# SPDX-License-Identifier: Apache-2.0
"""Status text shown in the UI when connecting to the AirPods fails.

Run:  python -m unittest tests.test_connect_status -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import airpods_mouse_controller as m  # noqa: E402
import i18n  # noqa: E402


class ConnectStatusTests(unittest.TestCase):
    def setUp(self):
        i18n.set_lang("fr")

    def tearDown(self):
        i18n.set_lang("en")

    def test_no_airpods_is_an_oserror_112_in_english(self):
        c = m.AAPClient("auto", auto_connect=False)
        with mock.patch.object(m, "apple_audio_devices", return_value=[]), \
                mock.patch.object(m, "have_bluetoothctl", return_value=True):
            with self.assertRaises(m.NoAirPodsConnected) as cm:
                c._pick()
        self.assertEqual(cm.exception.errno, 112)
        self.assertEqual(cm.exception.strerror, "no AirPods connected")

    def test_no_airpods_status_is_fully_translated(self):
        text = str(m.connect_failure_status(m.NoAirPodsConnected()))
        self.assertEqual(text, i18n.tr("srv.status.no_airpods", "fr"))
        i18n.set_lang("en")
        self.assertNotIn("aucun", str(m.connect_failure_status(m.NoAirPodsConnected())))

    def test_missing_bluetoothctl_status_is_translated(self):
        text = str(m.connect_failure_status(OSError(112, m.BLUETOOTHCTL_MISSING)))
        self.assertEqual(text, i18n.tr("srv.status.bluetoothctl_missing", "fr"))

    def test_other_errors_keep_their_detail(self):
        text = str(m.connect_failure_status(OSError(111, "Connection refused")))
        self.assertIn("Connection refused", text)
        self.assertTrue(text.startswith("Recherche des AirPods"))


if __name__ == "__main__":
    unittest.main()
