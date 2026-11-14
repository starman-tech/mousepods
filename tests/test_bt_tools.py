# SPDX-License-Identifier: Apache-2.0
"""A missing bluetoothctl must be reported as such, not as "no AirPods connected".

Run:  python -m unittest tests.test_bt_tools -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import airpods_mouse_controller as m  # noqa: E402

MISSING = FileNotFoundError(2, "No such file or directory", "bluetoothctl")


class BtToolTests(unittest.TestCase):
    def test_missing_bluetoothctl_raises(self):
        with mock.patch.object(m.subprocess, "run", side_effect=MISSING):
            with self.assertRaises(m.BluetoothToolMissing) as cm:
                m.apple_audio_devices()
        self.assertIn("bluetoothctl", str(cm.exception))
        self.assertIn("bluez", str(cm.exception))
        self.assertEqual(cm.exception.errno, 112)

    def test_bt_info_missing_raises(self):
        with mock.patch.object(m.subprocess, "run", side_effect=MISSING):
            with self.assertRaises(m.BluetoothToolMissing):
                m.bt_info("AA:BB:CC:DD:EE:FF")

    def test_pick_propagates_missing(self):
        c = m.AAPClient("auto", auto_connect=True)
        with mock.patch.object(m.subprocess, "run", side_effect=MISSING):
            with self.assertRaises(m.BluetoothToolMissing):
                c._pick()

    def test_timeout_is_not_reported_as_missing(self):
        with mock.patch.object(m.subprocess, "run",
                               side_effect=m.subprocess.TimeoutExpired("bluetoothctl", 5)):
            self.assertEqual(m.apple_audio_devices(), [])

    def test_fixed_address_still_connects_without_bluetoothctl(self):
        # device_address set + auto_connect off never needed bluetoothctl: must reach the socket
        class Reached(Exception):
            pass
        c = m.AAPClient("AA:BB:CC:DD:EE:FF", auto_connect=False)
        with mock.patch.object(m, "WINDOWS", False), \
                mock.patch.object(m.subprocess, "run", side_effect=MISSING), \
                mock.patch.object(m.socket, "AF_BLUETOOTH", 31, create=True), \
                mock.patch.object(m.socket, "BTPROTO_L2CAP", 0, create=True), \
                mock.patch.object(m.socket, "socket", side_effect=Reached):
            with self.assertRaises(Reached):
                c.connect()
        self.assertEqual(c.pid, "0000")

    def test_bt_info_timeout_returns_empty(self):
        with mock.patch.object(m.subprocess, "run",
                               side_effect=m.subprocess.TimeoutExpired("bluetoothctl", 5)):
            self.assertEqual(m.bt_info("AA:BB:CC:DD:EE:FF"), "")

    def test_missing_is_an_oserror(self):
        self.assertTrue(issubclass(m.BluetoothToolMissing, OSError))


if __name__ == "__main__":
    unittest.main()
