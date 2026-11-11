// KronosWsServer.cpp  --  WebSocket server DLL for the MQL5 EA.
//
// Why C++ (not C#): MQL5 #import can only load a NATIVE DLL directly.
// A C# assembly cannot be loaded by MT5 without an extra C++/CLI
// wrapper, so the server is written in C++ as a plain Win32 DLL.
// No third-party libraries: Winsock2 for sockets, Windows CryptoAPI
// (Crypt32) for the SHA-1 in the WebSocket handshake, std::thread
// for the accept / client threads. Single file, no dependencies.
//
// Build on the Windows host (pick the one that exists there):
//   MSVC (VS Developer Command Prompt):
//     cl /LD /EHsc KronosWsServer.cpp /link ws2_32.lib Crypt32.lib
//   MinGW-w64:
//     g++ -shared -o KronosWsServer.dll KronosWsServer.cpp -lws2_32 -lcrypt32 -static-libgcc -static-libstdc++
//
// Install: copy KronosWsServer.dll to the MT5 data folder
//   MQL5\Libraries\
// and tick "Allow DLL imports" in the EA's settings / MT5 options.
// The server listens ONLY on 127.0.0.1 (localhost), never on the LAN.
//
// Protocol (text frames, one JSON object per frame):
//   Python -> EA : {"signal":"LONG","change_pct":0.42,...,"bar_time":"..."} (from /api/ea_signal)
//   EA -> Python : {"type":"ea_status",...} / {"type":"ea_ack","bar_time":"..."} (optional)
// The DLL just moves strings: it queues every text frame it receives,
// the EA drains the queue with WsGetSignal(). It does NOT parse JSON.

#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <wincrypt.h>

#include <string>
#include <vector>
#include <queue>
#include <mutex>
#include <thread>
#include <atomic>
#include <algorithm>
#include <cstdint>
#include <cstring>

#pragma comment(lib, "ws2_32.lib")
#pragma comment(lib, "Crypt32.lib")

namespace {

// ---- shared state -------------------------------------------------------
std::atomic<bool>     g_running{false};
SOCKET                g_listenSocket = INVALID_SOCKET;
int                   g_port = 8081;
std::thread           g_acceptThread;
std::vector<SOCKET>   g_clients;
std::mutex            g_clientsMutex;
std::queue<std::string> g_messages;   // text frames received, in order
std::mutex            g_messagesMutex;

void RemoveClient(SOCKET s)
{
    std::lock_guard<std::mutex> lock(g_clientsMutex);
    g_clients.erase(std::remove(g_clients.begin(), g_clients.end(), s), g_clients.end());
}

void AddClient(SOCKET s)
{
    std::lock_guard<std::mutex> lock(g_clientsMutex);
    g_clients.push_back(s);
}

// Receive exactly `len` bytes (or false on disconnect / error).
bool RecvExact(SOCKET s, char* buf, int len)
{
    int got = 0;
    while (got < len)
    {
        int n = recv(s, buf + got, len - got, 0);
        if (n <= 0) return false;
        got += n;
    }
    return true;
}

// Send the whole buffer (or false on error).
bool SendAll(SOCKET s, const char* buf, int len)
{
    int sent = 0;
    while (sent < len)
    {
        int n = send(s, buf + sent, len - sent, 0);
        if (n <= 0) return false;
        sent += n;
    }
    return true;
}

// WebSocket accept key: base64( SHA1(key + GUID) ), via CryptoAPI so
// we do not hand-roll SHA-1 / base64.
bool ComputeAcceptKey(const std::string& clientKey, std::string& acceptKey)
{
    static const char* kGuid = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
    std::string input = clientKey + kGuid;

    HCRYPTPROV hProv = 0;
    HCRYPTHASH hHash = 0;
    BYTE hash[20];
    DWORD hashLen = sizeof(hash);
    char b64[64];
    DWORD b64Len = sizeof(b64);
    bool ok = false;

    if (!CryptAcquireContextA(&hProv, NULL, NULL, PROV_RSA_FULL, CRYPT_VERIFYCONTEXT))
        return false;
    if (CryptCreateHash(hProv, CALG_SHA1, 0, 0, &hHash) &&
        CryptHashData(hHash, (const BYTE*)input.data(), (DWORD)input.size(), 0) &&
        CryptGetHashParam(hHash, HP_HASHVAL, hash, &hashLen, 0) &&
        CryptBinaryToStringA(hash, hashLen,
                             CRYPT_STRING_BASE64 | CRYPT_STRING_NOCRLF,
                             b64, &b64Len))
    {
        // b64Len counts the terminating NUL; assign as a C string so
        // the accept key never carries an embedded NUL into the 101.
        acceptKey = b64;
        ok = true;
    }
    if (hHash) CryptDestroyHash(hHash);
    if (hProv) CryptReleaseContext(hProv, 0);
    return ok;
}

std::string Trim(const std::string& s)
{
    size_t a = s.find_first_not_of(" \t\r\n");
    if (a == std::string::npos) return "";
    size_t b = s.find_last_not_of(" \t\r\n");
    return s.substr(a, b - a + 1);
}

// Minimal HTTP handshake: read headers until blank line, find
// Sec-WebSocket-Key, answer 101 Switching Protocols.
bool DoHandshake(SOCKET s)
{
    std::string headers;
    char buf[1024];
    while (headers.find("\r\n\r\n") == std::string::npos && headers.size() < 8192)
    {
        int n = recv(s, buf, sizeof(buf), 0);
        if (n <= 0) return false;
        headers.append(buf, n);
    }
    std::string key;
    const std::string needle = "Sec-WebSocket-Key:";
    size_t pos = headers.find(needle);
    if (pos == std::string::npos) return false;
    size_t eol = headers.find("\r\n", pos);
    key = Trim(headers.substr(pos + needle.size(),
                              eol == std::string::npos ? std::string::npos : eol - (pos + needle.size())));
    if (key.empty()) return false;

    std::string accept;
    if (!ComputeAcceptKey(key, accept)) return false;

    std::string resp =
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Accept: " + accept + "\r\n\r\n";
    return SendAll(s, resp.data(), (int)resp.size());
}

// Send one unmasked text frame (server -> client must NOT mask).
bool SendTextFrame(SOCKET s, const std::string& msg)
{
    std::string frame;
    frame.push_back(char(0x81));  // FIN + text opcode
    size_t len = msg.size();
    if (len < 126)
    {
        frame.push_back(char(len));
    }
    else if (len < 65536)
    {
        frame.push_back(char(126));
        frame.push_back(char((len >> 8) & 0xFF));
        frame.push_back(char(len & 0xFF));
    }
    else
    {
        frame.push_back(char(127));
        for (int i = 7; i >= 0; --i)
            frame.push_back(char((len >> (8 * i)) & 0xFF));
    }
    frame += msg;
    return SendAll(s, frame.data(), (int)frame.size());
}

// One client connection: handshake, then read frames until close/error.
void ClientThread(SOCKET s)
{
    if (!DoHandshake(s))
    {
        closesocket(s);
        return;
    }
    AddClient(s);

    std::string fragmented;  // accumulation for fragmented (FIN=0) messages
    while (g_running)
    {
        unsigned char hdr[2];
        if (!RecvExact(s, (char*)hdr, 2)) break;
        bool fin = (hdr[0] & 0x80) != 0;
        unsigned char opcode = hdr[0] & 0x0F;
        bool masked = (hdr[1] & 0x80) != 0;
        uint64_t len = hdr[1] & 0x7F;
        if (len == 126)
        {
            unsigned char ext[2];
            if (!RecvExact(s, (char*)ext, 2)) break;
            len = ((uint64_t)ext[0] << 8) | ext[1];
        }
        else if (len == 127)
        {
            unsigned char ext[8];
            if (!RecvExact(s, (char*)ext, 8)) break;
            len = 0;
            for (int i = 0; i < 8; ++i) len = (len << 8) | ext[i];
        }
        unsigned char mask[4] = {0, 0, 0, 0};
        if (masked && !RecvExact(s, (char*)mask, 4)) break;

        if (len > (uint64_t)16 * 1024 * 1024) break;  // sanity cap: 16 MB
        std::string payload;
        payload.resize((size_t)len);
        if (len > 0 && !RecvExact(s, &payload[0], (int)len)) break;
        if (masked)
            for (size_t i = 0; i < payload.size(); ++i)
                payload[i] ^= (char)mask[i % 4];

        if (opcode == 0x8) break;                    // close
        if (opcode == 0x9)                           // ping -> pong
        {
            std::string pong;
            pong.push_back(char(0x8A));
            pong.push_back(char(payload.size()));
            pong += payload;
            if (!SendAll(s, pong.data(), (int)pong.size())) break;
            continue;
        }
        if (opcode == 0xA) continue;                 // pong: ignore
        if (opcode != 0x1 && opcode != 0x0) continue; // only text / continuation

        fragmented += payload;
        if (!fin) continue;
        {
            std::lock_guard<std::mutex> lock(g_messagesMutex);
            g_messages.push(fragmented);
        }
        fragmented.clear();
    }

    RemoveClient(s);
    closesocket(s);
}

void AcceptThread()
{
    while (g_running)
    {
        SOCKET s = accept(g_listenSocket, NULL, NULL);
        if (s == INVALID_SOCKET)
        {
            if (g_running) Sleep(10);
            continue;
        }
        std::thread(ClientThread, s).detach();
    }
}

}  // namespace

// ---- exported API (MQL5 #import, __stdcall) ------------------------------
extern "C" {

// Start the server on 127.0.0.1:port. Returns 1 ok, 0 already running, -1 error.
__declspec(dllexport) int __stdcall WsStart(int port)
{
    if (g_running) return 0;
    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) return -1;

    SOCKET ls = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (ls == INVALID_SOCKET) { WSACleanup(); return -1; }
    BOOL reuse = TRUE;
    setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, (const char*)&reuse, sizeof(reuse));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);  // 127.0.0.1 only
    addr.sin_port = htons((u_short)port);
    if (bind(ls, (sockaddr*)&addr, sizeof(addr)) == SOCKET_ERROR ||
        listen(ls, 8) == SOCKET_ERROR)
    {
        closesocket(ls);
        WSACleanup();
        return -1;
    }
    g_listenSocket = ls;
    g_port = port;
    g_running = true;
    g_acceptThread = std::thread(AcceptThread);
    return 1;
}

// Stop the server. Safe to call twice.
__declspec(dllexport) void __stdcall WsStop()
{
    if (!g_running) return;
    g_running = false;
    if (g_listenSocket != INVALID_SOCKET)
    {
        closesocket(g_listenSocket);
        g_listenSocket = INVALID_SOCKET;
    }
    {
        std::lock_guard<std::mutex> lock(g_clientsMutex);
        for (SOCKET s : g_clients) { shutdown(s, SD_BOTH); closesocket(s); }
        g_clients.clear();
    }
    if (g_acceptThread.joinable()) g_acceptThread.join();
    WSACleanup();
}

__declspec(dllexport) int __stdcall WsIsRunning()
{
    return g_running ? 1 : 0;
}

__declspec(dllexport) int __stdcall WsClientCount()
{
    std::lock_guard<std::mutex> lock(g_clientsMutex);
    return (int)g_clients.size();
}

// 1 if an unread message is queued, else 0.
__declspec(dllexport) int __stdcall WsHasSignal()
{
    std::lock_guard<std::mutex> lock(g_messagesMutex);
    return g_messages.empty() ? 0 : 1;
}

// Pop the oldest queued text frame into `buffer` (UTF-8, NOT
// null-terminated by us; the return value is the byte count).
// Returns 0 = queue empty, -1 = buffer too small (message stays
// queued, call again with a bigger buffer), -2 = server not running.
__declspec(dllexport) int __stdcall WsGetSignal(char* buffer, int bufferSize)
{
    if (!g_running) return -2;
    std::lock_guard<std::mutex> lock(g_messagesMutex);
    if (g_messages.empty()) return 0;
    const std::string& msg = g_messages.front();
    if ((int)msg.size() > bufferSize) return -1;
    memcpy(buffer, msg.data(), msg.size());
    int n = (int)msg.size();
    g_messages.pop();
    return n;
}

// Broadcast a text frame to every connected client.
// Returns the number of clients it was sent to.
__declspec(dllexport) int __stdcall WsSendText(const char* msg, int len)
{
    if (!g_running || msg == nullptr || len <= 0) return 0;
    std::string text(msg, len);
    std::lock_guard<std::mutex> lock(g_clientsMutex);
    int sent = 0;
    for (SOCKET s : g_clients)
        if (SendTextFrame(s, text)) ++sent;
    return sent;
}

}  // extern "C"

// NOTE: do NOT call WsStop() from DllMain -- joining a thread while
// the loader lock is held can deadlock MT5 on unload. The EA's
// OnDeinit() calls WsStop() explicitly, which is the clean shutdown.
BOOL APIENTRY DllMain(HMODULE, DWORD, LPVOID)
{
    return TRUE;
}
