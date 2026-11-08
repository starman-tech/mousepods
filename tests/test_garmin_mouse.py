# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Matteo Gaudioso (starman-tech) — https://github.com/starman-tech/mousepods
"""Montre Garmin : décodeur du service 16, calibration guidée, moteur d'inclinaison, rendu,
App sans fenêtre et GarminService (page web). Aucun Bluetooth : garmin-ble n'est pas nécessaire.

Lancer :  .venv/bin/python -m unittest tests.test_garmin_mouse -v
"""
import math
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import i18n  # noqa: E402
try:
    import numpy as np
    from garmin import garmin_mouse as gm
    from garmin import service as gs
except ImportError as e:  # numpy absent
    gm = None
    IMPORT_ERROR = str(e)


def tilt(deg_x=0.0, deg_y=0.0):
    """Gravity reading for a watch face-up (z = -1 g) tilted around y (-> x) and x (-> y)."""
    sx, sy = math.sin(math.radians(deg_x)), math.sin(math.radians(deg_y))
    return (sx, sy, -math.sqrt(max(0.0, 1 - sx * sx - sy * sy)))


def hold(c, a, n=40):
    for _ in range(n):
        c.feed(a)


def calibrate(right, left, up, down):
    c = gm.Calibrator()
    hold(c, tilt())
    for pose in (right, left, up, down):
        for k in range(1, 6):        # move there, then hold
            c.feed(tilt(pose[0] * k / 5, pose[1] * k / 5))
        hold(c, tilt(*pose))
    assert c.done, c.prompt
    return c.result


def engine(cal):
    e = gm.TiltEngine(dict(gm.DEFAULTS), cal)
    hold(e, tilt(), 30)
    return e


class FakePointer:
    def __init__(self):
        self.moves, self.clicks, self.closed = [], 0, False

    def move_rel(self, dx, dy):
        self.moves.append(dx)

    def move_abs(self, fx, fy):
        self.moves.append(fx)

    def click(self):
        self.clicks += 1

    def close(self):
        self.closed = True


@unittest.skipIf(gm is None, "numpy absent")
class TestEngine(unittest.TestCase):
    def test_decoder_reference_packet(self):
        s = gm.decode16(bytes.fromhex("4f71000000010f00fd1ff002e0ff021f"))
        self.assertEqual(len(s), 3)
        self.assertLess(abs(s[0][2] + 1.0), 0.05)

    def test_calibration_walks_all_poses_and_measures_ranges(self):
        r = calibrate((20, 0), (-25, 0), (0, 15), (0, -18))["range_deg"]
        self.assertLess(abs(r["right"] - 20), 1.5)
        self.assertLess(abs(r["left"] - 25), 1.5)
        self.assertLess(abs(r["up"] - 15), 1.5)
        self.assertLess(abs(r["down"] - 18), 1.5)

    def test_calibrated_directions_map_to_screen(self):
        e = engine(calibrate((20, 0), (-20, 0), (0, 15), (0, -15)))
        hold(e, tilt(15, 0))
        self.assertGreater(e.velocity()[0], 0)        # right
        hold(e, tilt(-15, 0))
        self.assertLess(e.velocity()[0], 0)           # left
        hold(e, tilt(0, 12))
        self.assertLess(e.velocity()[1], 0)           # up = screen y negative
        hold(e, tilt(0, -12))
        self.assertGreater(e.velocity()[1], 0)

    def test_calibration_handles_any_watch_orientation(self):
        # watch rotated on the wrist: "right" physically tilts the watch's y axis, "up" its -x axis
        e = engine(calibrate((0, 20), (0, -20), (-15, 0), (15, 0)))
        hold(e, tilt(0, 15))
        vx, vy = e.velocity()
        self.assertGreater(vx, 0)
        self.assertLess(abs(vy), 1)
        hold(e, tilt(-12, 0))
        self.assertLess(e.velocity()[1], 0)

    def test_wrong_side_is_rejected(self):
        c = gm.Calibrator()
        hold(c, tilt())
        hold(c, tilt(20, 0))                       # right ok
        hold(c, tilt(20, 0))                       # "left" done on the right side: refused
        self.assertEqual(c.i, 2)
        self.assertEqual(c.error, i18n.tr("garmin.calib.wrong_side"))

    def test_neutral_only_recalibration_keeps_axes(self):
        cal = calibrate((20, 0), (-20, 0), (0, 15), (0, -15))
        c = gm.Calibrator(gm.NEUTRAL_ONLY, cal)
        hold(c, tilt(5, 3))
        self.assertTrue(c.done)
        self.assertEqual(c.result["range_deg"], cal["range_deg"])
        self.assertGreater(np.dot(c.result["ex"], cal["ex"]), 0.99)

    def test_neutral_deadzone_and_pause(self):
        e = engine(calibrate((20, 0), (-20, 0), (0, 15), (0, -15)))
        hold(e, tilt(3, 2))
        self.assertEqual(e.velocity(), (0.0, 0.0))
        hold(e, tilt(70, 0), 10)
        self.assertTrue(e.paused)
        self.assertEqual(e.velocity(), (0.0, 0.0))

    def test_resting_offset_is_absorbed_but_real_tilt_moves(self):
        e = engine(calibrate((20, 0), (-20, 0), (0, 15), (0, -15)))
        hold(e, tilt(5, 3), 25 * 6)
        self.assertEqual(e.velocity(), (0.0, 0.0))
        hold(e, tilt(18, 3))
        self.assertGreater(e.velocity()[0], 300)

    def test_double_tap_clicks_and_single_does_not(self):
        e = engine(calibrate((20, 0), (-20, 0), (0, 15), (0, -15)))
        e.events.clear()
        e.feed((0.0, 0.0, -1.8))
        hold(e, tilt(), 4)
        self.assertFalse(any(ev[0] == "click" for ev in e.events))
        e.feed((0.0, 0.0, -1.8))
        self.assertTrue(any(ev[0] == "click" for ev in e.events))

    def test_renderer_is_smooth(self):
        e = engine(calibrate((20, 0), (-20, 0), (0, 15), (0, -15)))
        p = FakePointer()
        r = gm.Renderer(e, p, dict(gm.DEFAULTS))
        t = 0.0
        for i in range(120):
            if i % 5 == 0:
                e.feed(tilt(16, 0))
            r.step(1 / 120, t)
            t += 1 / 120
        d = [b - a for a, b in zip(p.moves, p.moves[1:])]
        self.assertLess(max(abs(x) for x in d), 0.15 * max(p.moves))


def guided(app, poses=((20, 0), (-20, 0), (0, 15), (0, -15))):
    """Feed the 5-pose calibration to an App, as the BLE thread would."""
    for _ in range(40):
        app.on_sample(tilt())
    for pose in poses:
        for k in range(1, 6):
            app.on_sample(tilt(pose[0] * k / 5, pose[1] * k / 5))
        for _ in range(40):
            app.on_sample(tilt(*pose))


@unittest.skipIf(gm is None, "numpy absent")
class TestHeadlessApp(unittest.TestCase):
    """App works without Tk: the web page only uses state(), tick(), recalibrate(), toggle()."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.calib = Path(self.tmp.name) / "calib.json"
        self.said = []
        self.app = gm.App("00:00:00:00:00:00", dict(gm.DEFAULTS), True,
                          notifier=lambda cfg, text: self.said.append(text), calib_path=self.calib)
        self.app.ptr = FakePointer()

    def tearDown(self):
        self.tmp.cleanup()

    def test_state_walks_the_guided_calibration(self):
        st = self.app.state()
        self.assertTrue(st["calibrating"])
        self.assertEqual((st["calib"]["step"], st["calib"]["steps"]), (1, 5))
        for _ in range(40):
            self.app.on_sample(tilt())
        st = self.app.state()
        self.assertEqual(st["calib"]["step"], 2)
        for _ in range(10):
            self.app.on_sample(tilt(20, 0))
        self.assertGreater(self.app.state()["calib"]["tilt_deg"][0], 10)   # live dot follows the wrist
        guided(self.app)
        st = self.app.state()
        self.assertFalse(st["calibrating"])
        self.assertTrue(self.calib.exists())
        self.assertTrue(any(s.startswith(i18n.tr("garmin.msg.calibrated").split("{")[0]) for s in self.said))
        self.assertIn("norm", st)

    def test_tick_moves_pointer_and_pause_stops_it(self):
        guided(self.app)
        for _ in range(30):
            self.app.on_sample(tilt())
        t = 0.0
        for i in range(60):
            if i % 5 == 0:
                self.app.on_sample(tilt(18, 0))
            self.app.tick(1 / 120, t)
            t += 1 / 120
        self.assertTrue(self.app.ptr.moves)
        n = len(self.app.ptr.moves)
        self.app.toggle()
        self.assertFalse(self.app.state()["enabled"])
        for i in range(60):
            self.app.on_sample(tilt(18, 0))
            self.app.tick(1 / 120, t)
            t += 1 / 120
        self.assertEqual(len(self.app.ptr.moves), n)

    def test_recenter_keeps_axes_and_full_restarts_5_steps(self):
        guided(self.app)
        self.app.recalibrate(False)
        self.app.tick(0.01, 0.0)
        self.assertEqual(self.app.state()["calib"]["steps"], 1)
        for _ in range(40):
            self.app.on_sample(tilt(2, 1))
        self.assertFalse(self.app.state()["calibrating"])
        self.app.recalibrate(True)
        self.app.tick(0.01, 0.0)
        self.assertEqual(self.app.state()["calib"]["steps"], 5)


class FakeApp:
    def __init__(self, address, cfg, full, notifier=None, calib_path=None):
        self.address, self.cfg, self.full = address, cfg, full
        self.enabled, self.status, self.alive, self.cmds = True, "Connexion à la montre…", True, []

    def run_thread(self):
        return self

    @property
    def running(self):
        return self.alive

    def close(self, timeout=0):
        self.alive = False

    def recalibrate(self, full):
        self.cmds.append(full)

    def toggle(self):
        self.enabled = not self.enabled

    def state(self):
        return {"status": self.status, "running": self.alive}


@unittest.skipIf(gm is None, "numpy absent")
class TestService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.svc = gs.GarminService(d / "garmin_mouse.json", d / "garmin_calib.json", app_factory=FakeApp,
                                    notifier=False)
        self._missing = gs.missing_modules
        gs.missing_modules = lambda: []          # pretend garmin-ble is installed

    def tearDown(self):
        gs.missing_modules = self._missing
        self.tmp.cleanup()

    def test_sanitize_clamps_and_drops_unknown(self):
        u = gs.sanitize({"max_speed": 99999, "deadzone": "0.3", "mode": "pointeur", "invert_x": 1,
                         "address": "AA:BB:CC:DD:EE:FF", "bogus": 1, "tap_g": "x"})
        self.assertEqual(u, {"max_speed": 5000.0, "deadzone": 0.3, "mode": "pointeur", "invert_x": True})
        self.assertEqual(gs.sanitize({"mode": "nope"}), {})

    def test_settings_are_persisted_and_shared_with_the_engine(self):
        self.svc.connect("aa:bb:cc:dd:ee:ff")
        self.svc.set({"max_speed": 1500, "mode": "pointeur"})
        self.assertEqual(self.svc.app.cfg["max_speed"], 1500.0)   # same dict: live
        again = gs.GarminService(self.svc.cfg_path, self.svc.calib_path, app_factory=FakeApp, notifier=False)
        self.assertEqual(again.cfg["mode"], "pointeur")
        self.assertEqual(again.cfg["address"], "AA:BB:CC:DD:EE:FF")

    def test_connect_validates_address_and_uses_full_calibration_first_time(self):
        with self.assertRaises(ValueError):
            self.svc.connect()
        with self.assertRaises(ValueError):
            self.svc.connect("not-an-address")
        self.svc.connect("AA:BB:CC:DD:EE:FF")
        self.assertTrue(self.svc.app.full)               # no garmin_calib.json yet
        self.svc.recenter()
        self.svc.recalibrate_all()
        self.assertEqual(self.svc.app.cmds, [False, True])
        self.assertFalse(self.svc.pause())
        self.svc.disconnect()
        self.assertFalse(self.svc.app.running)
        with self.assertRaises(RuntimeError):
            self.svc.recenter()

    def test_missing_module_is_reported_not_raised(self):
        gs.missing_modules = lambda: ["garmin_ble"]
        st = self.svc.state()
        self.assertFalse(st["available"])
        self.assertEqual(st["missing"], ["garmin_ble"])
        with self.assertRaises(RuntimeError):
            self.svc.connect("AA:BB:CC:DD:EE:FF")

    def test_parse_paired(self):
        rows = gs.parse_paired("Device 11:22:33:44:55:66 Some Headset\nDevice AA:BB:CC:DD:EE:FF quatix 7\n")
        self.assertEqual(rows[0], {"address": "AA:BB:CC:DD:EE:FF", "name": "quatix 7", "garmin": True})
        self.assertEqual(len(rows), 2)


@unittest.skipIf(gm is None, "numpy absent")
class TestWebEndpoint(unittest.TestCase):
    """/api/garmin through the real HTTP server, without AirPods nor watch."""

    def test_get_and_post(self):
        import json
        import urllib.request
        import airpods_mouse_controller as m
        import mousepods_web as w
        tmp = tempfile.TemporaryDirectory()
        cfgp = os.path.join(tmp.name, "config.json")
        with open(cfgp, "w") as f:
            json.dump({}, f)
        ctl = m.Controller(m.load_config(cfgp), m.NullPointer(), diag=True, cfg_path=cfgp)
        d = Path(tmp.name)
        orig = gs.GarminService.__init__.__defaults__
        gs.GarminService.__init__.__defaults__ = (d / "g.json", d / "c.json", FakeApp, False)
        try:
            ui = w.WebUI(ctl, cfgp, port=0)
            ui.start()
            port = ui.server.server_address[1]
            base = f"http://127.0.0.1:{port}/api/garmin"
            st = json.load(urllib.request.urlopen(base))
            self.assertIn("available", st)
            self.assertIn("max_speed", st["config"])
            req = urllib.request.Request(base, data=json.dumps({"action": "set", "values": {"deadzone": 0.3}}).encode(),
                                         headers={"Content-Type": "application/json"})
            self.assertTrue(json.load(urllib.request.urlopen(req))["ok"])
            self.assertEqual(json.load(urllib.request.urlopen(base))["config"]["deadzone"], 0.3)
            ui.stop()
        finally:
            gs.GarminService.__init__.__defaults__ = orig
            tmp.cleanup()



@unittest.skipIf(gm is None, "numpy absent")
class TestPointerRelease(unittest.TestCase):
    def test_pointer_mode_releases_cursor_when_wrist_is_still(self):
        class P:
            def __init__(self):
                self.n = 0

            def move_abs(self, x, y):
                self.n += 1

            def click(self):
                pass
        cfg = dict(gm.DEFAULTS, mode="pointeur")
        e = gm.TiltEngine(cfg, calibrate((20, 0), (-20, 0), (0, 15), (0, -15)))
        hold(e, tilt(), 30)
        p = P()
        r = gm.Renderer(e, p, cfg)
        t = 0.0
        for _ in range(240):                      # 2 s perfectly still
            r.step(1 / 120, t); t += 1 / 120
        still = p.n
        assert still < 50                          # cursor released after the first 0.3 s
        for i in range(120):                       # now tilt: cursor is driven again
            if i % 5 == 0:
                e.feed(tilt(10, 0))
            r.step(1 / 120, t); t += 1 / 120
        assert p.n - still > 30



if __name__ == "__main__":
    unittest.main()
