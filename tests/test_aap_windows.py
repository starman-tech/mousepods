# SPDX-License-Identifier: Apache-2.0
"""Windows transport: everything above the Win32 calls, run on any platform with a fake driver handle."""
import os
import queue
import select
import struct
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import aap_rate_probe  # noqa: E402
import aap_windows as aw  # noqa: E402
import airpods_mouse_controller as m  # noqa: E402

PATH = (r"\\?\bthenum#{74ec2172-0bad-4d01-8f77-997b2be0722a}_vid&0001004c_pid&2014"
        r"#8&2b1e3c1f&0&001122334455_c00000000#{6d0f5a3e-2b1c-4f7e-9a41-0c3e8b7d5a21}")
PATH3 = PATH.replace("pid&2014", "pid&2013").replace("001122334455", "aabbccddeeff")


class FakeChannel:
    """Stands for the driver handle: read() blocks on a queue, cancel() makes it fail like CancelIoEx."""

    def __init__(self, path=None):
        self.path, self.rx, self.sent, self.closed = path, queue.Queue(), [], False

    def read(self):
        pkt = self.rx.get()
        if pkt is None:
            raise OSError(995, "operation aborted")
        return pkt

    def write(self, pkt):
        self.sent.append(bytes(pkt))

    def cancel(self):
        self.rx.put(None)

    def close(self):
        self.closed = True


def pause(seconds=0.01):
    """A short wait that survives time.sleep being patched. select() with no sockets fails on Windows."""
    threading.Event().wait(seconds)


def recv(sock, timeout=2.0):
    r, _, _ = select.select([sock], [], [], timeout)
    if not r:
        raise AssertionError("nothing received")
    return sock.recv(8192)


def motion_block(ts_ns):
    return b"\x01" + struct.pack("<Q", ts_ns) + bytes(49)


def wx_packet(blocks):
    body = b"".join(bytes([0x3A, 2 + 2 + len(b), 0x08, m.SVC_DEVMOTION6, 0x1A, len(b)]) + b for b in blocks)
    return m.WX_PREFIX + struct.pack("<H", len(body)) + body


class InterfacePath(unittest.TestCase):
    def test_address_and_model(self):
        self.assertEqual(aw.parse_interface_path(PATH), ("00:11:22:33:44:55", "2014"))

    def test_unknown_shape_is_not_fatal(self):
        self.assertEqual(aw.parse_interface_path(r"\\?\something#else"), (None, "0000"))

    def test_pick(self):
        self.assertEqual(aw.pick_interface([PATH, PATH3]), PATH)
        self.assertEqual(aw.pick_interface([PATH, PATH3], "aa:bb:cc:dd:ee:ff"), PATH3)
        with self.assertRaises(OSError):
            aw.pick_interface([])
        with self.assertRaises(OSError):
            aw.pick_interface([PATH], "11:22:33:44:55:66")


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.ch = FakeChannel()
        self.sock, self.addr, self.pid = aw.connect("auto", lambda p: self.ch, [PATH])
        self.addCleanup(self.sock.close)

    def test_identity(self):
        self.assertEqual((self.addr, self.pid), ("00:11:22:33:44:55", "2014"))

    def test_packets_keep_their_boundaries_and_order(self):
        pkts = [bytes([i]) * n for i, n in enumerate((1, 58, 700, 2582, 3))]
        for p in pkts:
            self.ch.rx.put(p)
        self.assertEqual([recv(self.sock) for _ in pkts], pkts)

    def test_send_reaches_the_driver(self):
        self.sock.send(m.PKT_HANDSHAKE)
        self.sock.send(m.PKT_FEATURES)
        for _ in range(200):
            if len(self.ch.sent) == 2:
                break
            pause()
        self.assertEqual(self.ch.sent, [m.PKT_HANDSHAKE, m.PKT_FEATURES])

    def test_channel_loss_reads_as_empty_packet(self):
        self.ch.cancel()
        self.assertEqual(recv(self.sock), b"")           # the controller raises "channel closed" on this

    def test_shutdown_is_the_reconnect_button(self):
        self.sock.shutdown(2)
        self.assertEqual(recv(self.sock), b"")

    def test_close_releases_the_driver(self):
        self.sock.close()
        self.assertTrue(self.ch.closed)
        self.sock.close()                                # twice is harmless


class ControllerOnWindows(unittest.TestCase):
    def test_client_connects_through_the_driver(self):
        ch = FakeChannel()
        with mock.patch.object(m, "WINDOWS", True), mock.patch.object(m.time, "sleep", lambda s: None), \
                mock.patch.object(aw, "interface_paths", lambda: [PATH3]), \
                mock.patch.object(aw, "Win32Channel", lambda p: ch):
            aap = m.AAPClient("auto")
            aap.connect()
            self.addCleanup(aap.close)
            self.assertEqual((aap.addr, aap.pid), ("AA:BB:CC:DD:EE:FF", "2013"))
            for _ in range(200):
                if len(ch.sent) >= 4:
                    break
                pause()
            self.assertEqual(ch.sent[:3], [m.PKT_HANDSHAKE, m.PKT_FEATURES, m.PKT_NOTIFICATIONS])
            self.assertEqual(ch.sent[3][:10], m.RHID_PREFIX)             # HostConnect
            ch.rx.put(wx_packet([motion_block(1_000_000)]))
            self.assertEqual(len(aap.process(recv(aap.sock))), 1)

    def test_no_driver_is_a_retryable_error(self):
        with mock.patch.object(m, "WINDOWS", True), mock.patch.object(aw, "interface_paths", lambda: []):
            with self.assertRaises(OSError):
                m.AAPClient("auto").connect()

    def test_command_socket_is_udp(self):
        with mock.patch.object(m, "WINDOWS", True), mock.patch.object(m, "CTRL_UDP", ("127.0.0.1", 0)):
            s = m.open_ctrl_socket()
            self.addCleanup(s.close)
            with mock.patch.object(m, "CTRL_UDP", s.getsockname()):
                self.assertEqual(m.send_command("recenter"), 0)
            self.assertEqual(recv(s), b"recenter")


class RateProbe(unittest.TestCase):
    def test_counts_samples_not_packets(self):
        # 200 Hz for one second, two samples per packet, one sample lost
        ts = [i * 5_000_000 for i in range(200) if i != 77]
        samples = [(t / 1e9, t) for t in ts]
        s = aap_rate_probe.summarize(samples, packets=100, per_packet={2: 99, 1: 1}, max_len=140)
        self.assertAlmostEqual(s["hz_device_clock"], 199.0, delta=0.5)
        self.assertAlmostEqual(s["median_dt_ms"], 5.0, places=3)
        self.assertEqual((s["gaps"], s["out_of_order"]), (0, 0))        # one missing sample is 2x, not a gap
        self.assertAlmostEqual(s["longest_gap_ms"], 10.0, places=3)

    def test_reordering_is_reported(self):
        ts = [0, 5, 10, 20, 15, 25, 30]
        s = aap_rate_probe.summarize([(t / 1e3, t * 1_000_000) for t in ts], 7, {1: 7}, 70)
        self.assertEqual(s["out_of_order"], 1)

    def test_measure_through_the_bridge(self):
        ch = FakeChannel()
        with mock.patch.object(m, "WINDOWS", True), mock.patch.object(m.time, "sleep", lambda s: None), \
                mock.patch.object(aw, "interface_paths", lambda: [PATH]), \
                mock.patch.object(aw, "Win32Channel", lambda p: ch):
            aap = m.AAPClient("auto")
            aap.connect()
            self.addCleanup(aap.close)
            for i in range(0, 60, 2):
                ch.rx.put(wx_packet([motion_block(i * 10_000_000), motion_block((i + 1) * 10_000_000)]))
            s = aap_rate_probe.measure(aap, 0.3, settle=0.0)
        self.assertEqual((s["samples"], s["packets"], s["samples_per_packet"]), (60, 30, {2: 30}))
        self.assertAlmostEqual(s["hz_device_clock"], 100.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()
