// SPDX-License-Identifier: Apache-2.0
// MousePods AAP proof of concept for Windows: talks to AirPods through the
// modified BTHECHO client driver (L2CAP PSM 0x1001) and prints the motion stream.
// Packet sequence taken from airpods_mouse_controller.py.

#include <windows.h>
#include <initguid.h>
#include <cfgmgr32.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <vector>
#include "public.h"

#pragma comment(lib, "cfgmgr32.lib")

static const unsigned char PKT_HANDSHAKE[] = {0x00,0x00,0x04,0x00,0x01,0x00,0x02,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00};
static const unsigned char PKT_FEATURES[] = {0x04,0x00,0x04,0x00,0x4d,0x00,0xd7,0x00,0x00,0x00,0x00,0x00,0x00,0x00};
static const unsigned char PKT_NOTIFICATIONS[] = {0x04,0x00,0x04,0x00,0x0f,0x00,0xff,0xff,0xff,0xff};
static const unsigned char WX_PREFIX[] = {0x04,0x00,0x04,0x00,0x17,0x00,0x00,0x00,0x10,0x00};
static const unsigned char SVC_ACTIVITY = 0x0E, SVC_DEVMOTION6 = 0x10;

static HANDLE g_dev = INVALID_HANDLE_VALUE;
static unsigned char g_seq = 0;

static void hexline(const char *tag, const unsigned char *p, DWORD n, DWORD max)
{
    printf("%s %4lu B:", tag, n);
    for (DWORD i = 0; i < n && i < max; i++) printf(" %02x", p[i]);
    printf(n > max ? " ...\n" : "\n");
}

static bool send(const unsigned char *p, DWORD n)
{
    OVERLAPPED ov = {};
    ov.hEvent = CreateEvent(NULL, TRUE, FALSE, NULL);
    DWORD done = 0;
    BOOL ok = WriteFile(g_dev, p, n, NULL, &ov);
    if (!ok && GetLastError() == ERROR_IO_PENDING) ok = GetOverlappedResult(g_dev, &ov, &done, TRUE);
    CloseHandle(ov.hEvent);
    if (!ok) { printf("WriteFile failed: %lu\n", GetLastError()); return false; }
    hexline("tx", p, n, 24);
    return true;
}

static void stream(unsigned char svc, unsigned int interval_us)
{
    g_seq = (g_seq + 1) & 0x7F;
    unsigned char body[15] = {0x08, g_seq, 0x42, 0x0B, 0x08, svc, 0x10, 0x02, 0x1A, 0x05, 0x01};
    memcpy(body + 11, &interval_us, 4);
    unsigned char pkt[sizeof(WX_PREFIX) + 2 + sizeof(body)];
    memcpy(pkt, WX_PREFIX, sizeof(WX_PREFIX));
    unsigned short len = sizeof(body);
    memcpy(pkt + sizeof(WX_PREFIX), &len, 2);
    memcpy(pkt + sizeof(WX_PREFIX) + 2, body, sizeof(body));
    send(pkt, sizeof(pkt));
}

static PWSTR device_path()
{
    ULONG size = 0;
    if (CM_Get_Device_Interface_List_SizeW(&size, (LPGUID)&BTHECHOSAMPLE_DEVICE_INTERFACE, NULL,
            CM_GET_DEVICE_INTERFACE_LIST_PRESENT) != CR_SUCCESS || size <= 1) return NULL;
    PWSTR list = (PWSTR)calloc(size, sizeof(WCHAR));
    if (CM_Get_Device_Interface_ListW((LPGUID)&BTHECHOSAMPLE_DEVICE_INTERFACE, NULL, list, size,
            CM_GET_DEVICE_INTERFACE_LIST_PRESENT) != CR_SUCCESS || !*list) { free(list); return NULL; }
    return list;
}

struct Reader {
    OVERLAPPED ov = {};
    unsigned char buf[4096];
    void post() {
        ResetEvent(ov.hEvent);
        if (!ReadFile(g_dev, buf, sizeof(buf), NULL, &ov) && GetLastError() != ERROR_IO_PENDING)
            printf("ReadFile failed: %lu\n", GetLastError());
    }
};

int main(int argc, char **argv)
{
    int seconds = argc > 1 ? atoi(argv[1]) : 10;
    PWSTR path = device_path();
    if (!path) { printf("No MousePods AAP device interface. Driver installed and AirPods connected?\n"); return 1; }
    wprintf(L"Interface: %ls\n", path);
    g_dev = CreateFileW(path, GENERIC_READ | GENERIC_WRITE, 0, NULL, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, NULL);
    free(path);
    if (g_dev == INVALID_HANDLE_VALUE) { printf("CreateFile (L2CAP open) failed: %lu\n", GetLastError()); return 1; }
    printf("L2CAP channel to PSM 0x1001 open\n");

    const int NR = 8;
    Reader readers[NR];
    HANDLE events[NR];
    for (int i = 0; i < NR; i++) { readers[i].ov.hEvent = events[i] = CreateEvent(NULL, TRUE, FALSE, NULL); readers[i].post(); }

    send(PKT_HANDSHAKE, sizeof(PKT_HANDSHAKE)); Sleep(300);
    send(PKT_FEATURES, sizeof(PKT_FEATURES)); Sleep(150);
    send(PKT_NOTIFICATIONS, sizeof(PKT_NOTIFICATIONS)); Sleep(150);

    ULONGLONG t0 = GetTickCount64();
    bool started = false;
    unsigned long total = 0, wx = 0, wx_big = 0;
    while (GetTickCount64() - t0 < (ULONGLONG)seconds * 1000 + 1500) {
        if (!started && GetTickCount64() - t0 > 1500) {
            printf("-- requesting motion stream (activity, devmotion6 @ 10 ms)\n");
            stream(SVC_ACTIVITY, 40000); Sleep(250);
            stream(SVC_DEVMOTION6, 10000);
            stream(SVC_ACTIVITY, 0);
            started = true;
        }
        DWORD w = WaitForMultipleObjects(NR, events, FALSE, 200);
        if (w >= WAIT_OBJECT_0 + NR) continue;
        Reader &r = readers[w - WAIT_OBJECT_0];
        DWORD n = 0;
        if (!GetOverlappedResult(g_dev, &r.ov, &n, FALSE)) {
            printf("read error %lu, channel closed?\n", GetLastError());
            break;
        }
        total++;
        bool is_wx = n >= 12 && memcmp(r.buf, WX_PREFIX, sizeof(WX_PREFIX)) == 0;
        if (is_wx) { wx++; if (n >= 70) wx_big++; }
        if (!is_wx || wx <= 3) hexline(is_wx ? "rx WX" : "rx   ", r.buf, n, 32);
        r.post();
    }
    double secs = (GetTickCount64() - t0) / 1000.0;
    printf("== %lu packets, %lu SensorDataWX (%lu with sensor blocks), %.1f WX/s\n", total, wx, wx_big, wx / secs);
    stream(SVC_DEVMOTION6, 0);
    CancelIoEx(g_dev, NULL);
    CloseHandle(g_dev);
    return wx_big ? 0 : 2;
}
