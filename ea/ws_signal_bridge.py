#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ws_signal_bridge.py -- Python side of the WebSocket EA link (runs on the host).

Role: WebSocket CLIENT. The EA is the server (C++ DLL inside MT5).
This bridge:
  1. watches MT5 for a newly CLOSED H1 bar (cheap: 2 bars via MetaTrader5),
  2. calls the local Flask 8077 /api/ea_signal -- Python there runs the
     gold model and parses the forecast into a flat signal JSON,
  3. pushes that JSON over WebSocket to the EA, which trades on it.

Pure standard library for the WebSocket client (socket/base64/hashlib):
no `websockets` / `websocket-client` pip install needed.

Run with the export venv (it has MetaTrader5 + requests-free stdlib only):
    D:\\Kronos\\.venv-gold\\Scripts\\python.exe D:\\Kronos\\ea\\ws_signal_bridge.py
    D:\\Kronos\\.venv-gold\\Scripts\\python.exe D:\\Kronos\\ea\\ws_signal_bridge.py --once   (one bar, for testing)

Keep this file in D:\\Kronos\\ea\\ -- NOT in D:\\Kronos\\webui\\ (Flask
reloader watches that folder).
"""

import argparse
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import time
import urllib.request
import urllib.error

WS_HOST = "127.0.0.1"
WS_PORT = 8081
EA_SIGNAL_URL = "http://127.0.0.1:8077/api/ea_signal"
SYMBOL = "XAUUSDm"
TIMEFRAME = "H1"
PRED_LEN = 12
HORIZON = 12
THRESHOLD_PCT = 0.10
POLL_SECONDS = 20

MT5_PATH = r"D:\mt5\terminal64.exe"


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Minimal WebSocket client (RFC 6455, text frames only, stdlib only)
# ---------------------------------------------------------------------------
class WsClient:
    def __init__(self, host, port, path="/"):
        self.host, self.port, self.path = host, port, path
        self.sock = None

    def connect(self, timeout=10):
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WS handshake: connection closed")
            resp += chunk
        if b"101" not in resp.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"WS handshake failed: {resp[:200]!r}")
        log(f"WS connected to {self.host}:{self.port}")

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("WS connection closed by server")
            buf += chunk
        return buf

    def send_text(self, text):
        payload = text.encode("utf-8")
        header = bytearray([0x81])  # FIN + text, masked (clients MUST mask)
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv_text(self, timeout=5):
        """One text frame, or None on timeout. Answers pings with pongs."""
        self.sock.settimeout(timeout)
        try:
            hdr = self._recv_exact(2)
        except socket.timeout:
            return None
        fin = hdr[0] & 0x80
        opcode = hdr[0] & 0x0F
        length = hdr[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        payload = self._recv_exact(length) if length else b""
        if opcode == 0x9:  # ping -> pong
            self.sock.sendall(bytes([0x8A, len(payload)]) + payload)
            return self.recv_text(timeout)
        if opcode == 0x8:
            raise ConnectionError("WS closed by server")
        if opcode != 0x1:
            return self.recv_text(timeout)
        return payload.decode("utf-8", "replace")

    def close(self):
        try:
            if self.sock:
                self.sock.sendall(bytes([0x88, 0x00]))
                self.sock.close()
        except OSError:
            pass
        self.sock = None


# ---------------------------------------------------------------------------
# MT5: last CLOSED bar time (cheap, no model involved)
# ---------------------------------------------------------------------------
def last_closed_bar_time(mt5, symbol, timeframe):
    tf = getattr(mt5, "TIMEFRAME_" + timeframe, None)
    if tf is None:
        raise ValueError(f"bad timeframe {timeframe}")
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, 2)
    if rates is None or len(rates) < 2:
        return None
    return int(rates[1]["time"])  # [1] = last closed bar; [0] is still forming


def call_ea_signal():
    body = json.dumps({
        "symbol": SYMBOL, "timeframe": TIMEFRAME,
        "pred_len": PRED_LEN, "horizon": HORIZON,
        "threshold_pct": THRESHOLD_PCT,
    }).encode()
    req = urllib.request.Request(EA_SIGNAL_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"ea_signal HTTP {e.code}: {e.read().decode()[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"ea_signal unreachable (is 8077 up?): {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="push one signal for the latest closed bar, then exit")
    ap.add_argument("--ws-port", type=int, default=WS_PORT)
    args = ap.parse_args()

    import MetaTrader5 as mt5
    if not mt5.initialize(path=MT5_PATH) and not mt5.initialize():
        log(f"MT5 initialize failed: {mt5.last_error()}")
        return 1
    log(f"MT5 connected, account={mt5.account_info().login if mt5.account_info() else '?'}")

    last_pushed = None
    ws = None
    try:
        while True:
            # --- (re)connect WS to the EA server ---
            if ws is None:
                try:
                    ws = WsClient(WS_HOST, args.ws_port)
                    ws.connect()
                except (OSError, ConnectionError) as e:
                    log(f"WS connect failed (is the EA attached with the DLL running?): {e} -- retry in 10s")
                    ws = None
                    time.sleep(10)
                    continue

            # --- new closed bar? ---
            try:
                bar_time = last_closed_bar_time(mt5, SYMBOL, TIMEFRAME)
            except Exception as e:
                log(f"MT5 bar check failed: {e}")
                time.sleep(POLL_SECONDS)
                continue
            if bar_time is None:
                time.sleep(POLL_SECONDS)
                continue
            if bar_time == last_pushed:
                if args.once:
                    log("latest closed bar already pushed, nothing to do")
                    return 0
                time.sleep(POLL_SECONDS)
                continue

            log(f"new closed {TIMEFRAME} bar {time.strftime('%Y-%m-%d %H:%M', time.gmtime(bar_time))} -- asking 8077 for a signal...")
            try:
                signal = call_ea_signal()
            except RuntimeError as e:
                log(str(e))
                time.sleep(POLL_SECONDS)
                continue
            log(f"signal: {signal.get('signal')} change={signal.get('change_pct')}% "
                f"last={signal.get('last_close')} pred={signal.get('pred_close')} "
                f"up_ratio={signal.get('up_ratio')} atr={signal.get('atr')}")
            try:
                ws.send_text(json.dumps(signal, ensure_ascii=False))
                log("pushed to EA via WebSocket")
            except (OSError, ConnectionError) as e:
                log(f"WS send failed, will reconnect: {e}")
                ws = None
                time.sleep(5)
                continue
            last_pushed = bar_time
            # read any EA ack/status waiting (non-blocking-ish, 2s)
            try:
                reply = ws.recv_text(timeout=2)
                if reply:
                    log(f"EA replied: {reply[:300]}")
            except (OSError, ConnectionError) as e:
                log(f"WS lost while reading reply: {e}")
                ws = None
            if args.once:
                return 0
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        log("stopped by user")
        return 0
    finally:
        if ws:
            ws.close()
        mt5.shutdown()


if __name__ == "__main__":
    sys.exit(main())
