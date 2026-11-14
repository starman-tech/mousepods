# SPDX-License-Identifier: Apache-2.0
"""AirPods AAP channel on Windows, through the native Bluetooth stack.

Windows gives user programs no L2CAP socket, so the channel (PSM 0x1001, receive MTU 2582) is opened by
the small kernel driver in windows/aap-poc (Microsoft's bthecho sample, modified upstream in MousePods
PR #17). The driver exposes one device interface per paired AirPods; opening it opens the channel, each
ReadFile returns one L2CAP packet and each WriteFile sends one.

The controller's main loop is built around a socket (select / recv / send). To leave it untouched, the
driver handle is bridged to a pair of connected loopback UDP sockets: datagrams keep packet boundaries,
exactly like the SOCK_SEQPACKET socket on Linux, and an empty datagram means "channel closed".

    AirPods ── L2CAP ── driver ── ReadFile/WriteFile ── bridge threads ── UDP 127.0.0.1 ── controller

EXPERIMENTAL: written against the driver's proof of concept, not yet run on Windows hardware by us.
Everything except `Win32Channel` and `interface_paths` is plain Python and is tested on Linux.
"""
from __future__ import annotations

import collections
import logging
import re
import socket
import threading
import uuid

log = logging.getLogger("airpods-mouse")

# device interface published by windows/aap-poc (common/inc/public.h)
INTERFACE_GUID = "6d0f5a3e-2b1c-4f7e-9a41-0c3e8b7d5a21"
READ_SIZE = 4096            # >= the 2582-byte receive MTU
PENDING_READS = 4           # overlapped reads kept posted, so no packet waits for Python
# \\?\bthenum#{74ec2172-...}_vid&0001004c_pid&2014#8&2b1e3c1f&0&001122334455_c00000000#{6d0f5a3e-...}
_PID = re.compile(r"pid&([0-9a-f]{4})", re.I)
_ADDR = re.compile(r"&([0-9a-f]{12})_", re.I)


def parse_interface_path(path):
    """(Bluetooth address 'AA:BB:CC:DD:EE:FF' or None, product id 'xxxx' or '0000') of an interface path."""
    a = _ADDR.search(path or "")
    p = _PID.search(path or "")
    addr = ":".join(a.group(1)[i:i + 2] for i in range(0, 12, 2)).upper() if a else None
    return addr, (p.group(1).lower() if p else "0000")


def pick_interface(paths, want="auto"):
    """The interface for the wanted address ('auto' = the first one), or OSError like the Linux side."""
    if not paths:
        raise OSError(112, "no AirPods interface: driver installed, AirPods paired and connected?")
    if want in (None, "auto"):
        return paths[0]
    for p in paths:
        if parse_interface_path(p)[0] == want.upper():
            return p
    raise OSError(112, f"AirPods {want} not found among {[parse_interface_path(p)[0] for p in paths]}")


# --------------------------------------------------------------------------- Win32 (only loaded on Windows)
def _win32():
    """kernel32 / cfgmgr32 prototypes. Built on first use so the module imports on any platform."""
    if _win32.cache:
        return _win32.cache
    import ctypes
    from ctypes import wintypes as w

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", w.DWORD), ("OffsetHigh", w.DWORD), ("hEvent", w.HANDLE)]

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD), ("Data4", ctypes.c_ubyte * 8)]

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    cm = ctypes.WinDLL("cfgmgr32", use_last_error=True)
    lpov = ctypes.POINTER(OVERLAPPED)
    k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
    k.CreateFileW.restype = w.HANDLE
    k.CreateEventW.argtypes = [ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR]
    k.CreateEventW.restype = w.HANDLE
    k.ResetEvent.argtypes = [w.HANDLE]
    k.ResetEvent.restype = w.BOOL
    k.ReadFile.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.c_void_p, lpov]
    k.ReadFile.restype = w.BOOL
    k.WriteFile.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.c_void_p, lpov]
    k.WriteFile.restype = w.BOOL
    k.GetOverlappedResult.argtypes = [w.HANDLE, lpov, ctypes.POINTER(w.DWORD), w.BOOL]
    k.GetOverlappedResult.restype = w.BOOL
    k.CancelIoEx.argtypes = [w.HANDLE, lpov]
    k.CancelIoEx.restype = w.BOOL
    k.CloseHandle.argtypes = [w.HANDLE]
    k.CloseHandle.restype = w.BOOL
    lpguid = ctypes.POINTER(GUID)
    cm.CM_Get_Device_Interface_List_SizeW.argtypes = [ctypes.POINTER(w.ULONG), lpguid, w.LPCWSTR, w.ULONG]
    cm.CM_Get_Device_Interface_List_SizeW.restype = w.DWORD
    cm.CM_Get_Device_Interface_ListW.argtypes = [lpguid, w.LPCWSTR, ctypes.c_void_p, w.ULONG, w.ULONG]
    cm.CM_Get_Device_Interface_ListW.restype = w.DWORD
    _win32.cache = (ctypes, w, k, cm, OVERLAPPED, GUID)
    return _win32.cache


_win32.cache = None
GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
OPEN_EXISTING, FILE_FLAG_OVERLAPPED = 3, 0x40000000
ERROR_IO_PENDING = 997


def interface_paths(guid=INTERFACE_GUID):
    """Device interface paths of the present AirPods (one per paired AirPods the driver is bound to)."""
    ctypes, w, _k, cm, _ov, GUID = _win32()
    g = GUID.from_buffer_copy(uuid.UUID(guid).bytes_le)
    size = w.ULONG(0)
    if cm.CM_Get_Device_Interface_List_SizeW(ctypes.byref(size), ctypes.byref(g), None, 0) or size.value <= 1:
        return []
    buf = ctypes.create_unicode_buffer(size.value)
    if cm.CM_Get_Device_Interface_ListW(ctypes.byref(g), None, buf, size.value, 0):
        return []
    return [p for p in buf[:size.value].split("\0") if p]


class Win32Channel:
    """The driver handle: read() blocks for one L2CAP packet, write() sends one. Overlapped I/O, so a
    blocked read never holds a write back. Reads are consumed strictly in the order they were posted."""

    def __init__(self, path):
        ctypes, w, k, _cm, OVERLAPPED, _g = _win32()
        self._ct, self._w, self._k, self._OV = ctypes, w, k, OVERLAPPED
        h = k.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
        if h is None or h == w.HANDLE(-1).value:
            err = ctypes.get_last_error()
            raise OSError(err, f"cannot open the AAP channel (Windows error {err}): "
                               "AirPods connected? another program already using them?")
        self.h = h
        self._wov = OVERLAPPED()
        self._wov.hEvent = k.CreateEventW(None, True, False, None)
        self._reads = collections.deque()
        for _ in range(PENDING_READS):
            ov = OVERLAPPED()
            ov.hEvent = k.CreateEventW(None, True, False, None)
            rd = (ov, ctypes.create_string_buffer(READ_SIZE))
            self._post(rd)
            self._reads.append(rd)

    def _post(self, rd):
        ov, buf = rd
        self._k.ResetEvent(ov.hEvent)
        if not self._k.ReadFile(self.h, buf, READ_SIZE, None, self._ct.byref(ov)):
            err = self._ct.get_last_error()
            if err != ERROR_IO_PENDING:
                raise OSError(err, f"ReadFile failed (Windows error {err})")

    def read(self):
        rd = self._reads.popleft()                     # oldest posted read = next packet
        n = self._w.DWORD(0)
        if not self._k.GetOverlappedResult(self.h, self._ct.byref(rd[0]), self._ct.byref(n), True):
            err = self._ct.get_last_error()
            raise OSError(err, f"AAP channel closed (Windows error {err})")
        data = rd[1].raw[:n.value]
        self._post(rd)
        self._reads.append(rd)
        return data

    def write(self, pkt):
        n = self._w.DWORD(0)
        self._k.ResetEvent(self._wov.hEvent)
        ok = self._k.WriteFile(self.h, bytes(pkt), len(pkt), None, self._ct.byref(self._wov))
        if not ok and self._ct.get_last_error() == ERROR_IO_PENDING:
            ok = self._k.GetOverlappedResult(self.h, self._ct.byref(self._wov), self._ct.byref(n), True)
        if not ok:
            err = self._ct.get_last_error()
            raise OSError(err, f"WriteFile failed (Windows error {err})")

    def cancel(self):
        """Wake the blocked read (it fails with 'operation aborted')."""
        if self.h:
            self._k.CancelIoEx(self.h, None)

    def close(self):
        if self.h:
            self._k.CancelIoEx(self.h, None)
            self._k.CloseHandle(self.h)
            self.h = None


# --------------------------------------------------------------------------- handle <-> socket bridge
class BridgedSocket(socket.socket):
    """The socket handed to the controller. close() and shutdown() also end the bridge."""

    bridge = None

    def shutdown(self, how):                 # the web app's "reconnect" button calls sock.shutdown(2)
        if self.bridge:
            self.bridge.channel.cancel()

    def close(self):
        b, self.bridge = self.bridge, None
        if b:
            b.stop()
        super().close()


class Bridge:
    """Two threads between a channel (read / write / cancel / close) and a loopback UDP socket pair."""

    def __init__(self, channel):
        self.channel = channel
        self.app = BridgedSocket(socket.AF_INET, socket.SOCK_DGRAM)
        self.inner = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for s in (self.app, self.inner):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)   # seconds of sensor packets
            s.bind(("127.0.0.1", 0))
        self.app.connect(self.inner.getsockname())
        self.inner.connect(self.app.getsockname())
        self.inner.settimeout(0.2)
        self.app.bridge = self
        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self._rx, daemon=True, name="aap-rx"),
                         threading.Thread(target=self._tx, daemon=True, name="aap-tx")]
        for t in self._threads:
            t.start()

    def _rx(self):
        try:
            while not self._stop.is_set():
                pkt = self.channel.read()
                if pkt:
                    self.inner.send(pkt)
        except OSError as e:
            if not self._stop.is_set():
                log.debug("AAP channel read ended: %s", e)
        try:
            self.inner.send(b"")              # empty datagram = "channel closed" for the controller
        except OSError:
            pass

    def _tx(self):
        while not self._stop.is_set():
            try:
                pkt = self.inner.recv(8192)
            except socket.timeout:
                continue
            except OSError:
                return
            if pkt:
                try:
                    self.channel.write(pkt)
                except OSError as e:
                    log.warning("Cannot send AAP packet: %s", e)
                    self.channel.cancel()     # ends the read side, the controller reconnects
                    return

    def stop(self):
        self._stop.set()
        self.channel.cancel()
        me = threading.current_thread()
        for t in self._threads:
            if t is not me:
                t.join(1.0)
        self.channel.close()
        self.inner.close()


def connect(want="auto", channel_factory=None, paths=None):
    """(socket, address, product id) for the wanted AirPods. The socket behaves like the Linux L2CAP one."""
    path = pick_interface(interface_paths() if paths is None else paths, want)
    addr, pid = parse_interface_path(path)
    bridge = Bridge((channel_factory or Win32Channel)(path))
    return bridge.app, addr or (want if want != "auto" else "unknown"), pid


def raise_timer_resolution():
    """Windows' default 15.6 ms scheduler tick would quantise the 120 Hz render loop and the 25 Hz
    setpoint sender. Ask for 1 ms for the life of the process. No-op elsewhere."""
    try:
        import ctypes
        ctypes.WinDLL("winmm").timeBeginPeriod(1)
        return True
    except (AttributeError, OSError):
        return False
