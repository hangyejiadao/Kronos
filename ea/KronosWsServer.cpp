// KronosWsServer.cpp —— 供 MQL5 EA 使用的 WebSocket 服务端 DLL。
//
// 为什么用 C++（而不用 C#）：MQL5 的 #import 只能直接加载原生 DLL。
// C# 程序集不额外包一层 C++/CLI 的话，MT5 加载不了，
// 所以服务端用 C++ 写成普通的 Win32 DLL。
// 不依赖第三方库：用 Winsock2 做 socket，用 Windows CryptoAPI
//（Crypt32）算 WebSocket 握手里的 SHA-1，用 std::thread 跑
// 接受连接/客户端线程。单文件，无依赖。
//
// 在 Windows 主机上编译（按主机上已有的工具选一种）：
//   MSVC (VS Developer Command Prompt):
//     cl /LD /EHsc KronosWsServer.cpp /link ws2_32.lib Crypt32.lib
//   MinGW-w64:
//     g++ -shared -o KronosWsServer.dll KronosWsServer.cpp -lws2_32 -lcrypt32 -static-libgcc -static-libstdc++
//
// 安装：把 KronosWsServer.dll 复制到 MT5 数据文件夹
//   MQL5\Libraries\
// 并在 EA 设置 / MT5 选项里勾选“允许 DLL 导入”。
// 服务端只监听 127.0.0.1（本机），绝不监听局域网。
//
// 协议（文本帧，每帧一个 JSON 对象）：
//   Python -> EA : {"signal":"LONG","change_pct":0.42,...,"bar_time":"..."} (from /api/ea_signal)
//   EA -> Python : {"type":"ea_status",...} / {"type":"ea_ack","bar_time":"..."} (optional)
// DLL 只负责传字符串：收到的每个文本帧都进队列，
// EA 用 WsGetSignal() 取出队列里的消息。DLL 不解析 JSON。

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

// ---- 共享状态 ------------------------------------------------------------
std::atomic<bool>     g_running{false};
SOCKET                g_listenSocket = INVALID_SOCKET;
int                   g_port = 8081;
std::thread           g_acceptThread;
std::vector<SOCKET>   g_clients;
std::mutex            g_clientsMutex;
std::queue<std::string> g_messages;   // 按接收顺序排列的文本帧
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

// 恰好接收 `len` 个字节（断开连接或出错时返回 false）。
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

// 发送整个缓冲区（出错时返回 false）。
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

// WebSocket 握手应答 key：base64(SHA1(key + GUID)），用 CryptoAPI 计算，
// 这样就不用自己手写 SHA-1 / base64。
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
        // b64Len 包含结尾的 NUL；按 C 字符串赋值，
        // 这样应答 key 就不会把内嵌的 NUL 带进 101 响应里。
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

// 最简 HTTP 握手：一直读请求头直到空行，找到
// Sec-WebSocket-Key，回复 101 Switching Protocols。
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

// 发送一个不掩码的文本帧（服务端发给客户端时必须不掩码）。
bool SendTextFrame(SOCKET s, const std::string& msg)
{
    std::string frame;
    frame.push_back(char(0x81));  // FIN + 文本帧操作码
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

// 处理一个客户端连接：先握手，然后一直读帧，直到关闭或出错。
void ClientThread(SOCKET s)
{
    if (!DoHandshake(s))
    {
        closesocket(s);
        return;
    }
    AddClient(s);

    std::string fragmented;  // 分片消息（FIN=0）的累积缓冲区
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

        if (len > (uint64_t)16 * 1024 * 1024) break;  // 合理性上限：16 MB
        std::string payload;
        payload.resize((size_t)len);
        if (len > 0 && !RecvExact(s, &payload[0], (int)len)) break;
        if (masked)
            for (size_t i = 0; i < payload.size(); ++i)
                payload[i] ^= (char)mask[i % 4];

        if (opcode == 0x8) break;                    // 关闭帧
        if (opcode == 0x9)                           // ping -> 回 pong
        {
            std::string pong;
            pong.push_back(char(0x8A));
            pong.push_back(char(payload.size()));
            pong += payload;
            if (!SendAll(s, pong.data(), (int)pong.size())) break;
            continue;
        }
        if (opcode == 0xA) continue;                 // pong：忽略
        if (opcode != 0x1 && opcode != 0x0) continue; // 只处理文本帧 / 续帧

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

}  // 命名空间结束

// ---- 对外导出的 API（MQL5 #import 调用，__stdcall）-------------------------
extern "C" {

// 在 127.0.0.1:port 上启动服务端。返回 1 表示成功，0 表示已在运行，-1 表示出错。
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
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);  // 仅限 127.0.0.1
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

// 停止服务端。重复调用是安全的。
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

// 队列里有未读消息时返回 1，否则返回 0。
__declspec(dllexport) int __stdcall WsHasSignal()
{
    std::lock_guard<std::mutex> lock(g_messagesMutex);
    return g_messages.empty() ? 0 : 1;
}

// 把队列里最旧的文本帧取出来放进 `buffer`（UTF-8，我们不会在末尾补
// 空字符；返回值是字节数）。
// 返回 0 = 队列为空，-1 = 缓冲区太小（消息留在队列里，换个更大的缓冲区再取），
// -2 = 服务端未运行。
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

// 向所有已连接的客户端广播一个文本帧。
// 返回成功发送到的客户端数量。
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

// 注意：不要在 DllMain 里调用 WsStop()——在持有加载器锁的情况下 join 线程
// 会导致 MT5 卸载 DLL 时死锁。EA 的
// OnDeinit() 会显式调用 WsStop()，那才是干净的关闭方式。
BOOL APIENTRY DllMain(HMODULE, DWORD, LPVOID)
{
    return TRUE;
}
