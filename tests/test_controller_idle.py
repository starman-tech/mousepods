# SPDX-License-Identifier: Apache-2.0
"""Background cost of the controller: the motion engine's still-detection window without a rescan
per sample, no pointer event when the pointer is already in place, no render wake-ups while the
mouse is off.

Run:  python -m unittest tests.test_controller_idle -v
"""
import json
import logging
import os
import random
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import airpods_mouse_controller as m  # noqa: E402


def controller(pointer):
    d = tempfile.mkdtemp()
    path = os.path.join(d, "config.json")
    return m.Controller(m.load_config(path), pointer, cfg_path=path)


class WindowRangeTests(unittest.TestCase):
    def test_matches_a_full_rescan(self):
        rnd = random.Random(7)
        eng = m.MotionEngine(m.DEFAULTS["bias"])
        hist = []
        for i in range(2000):
            # repeated values and plateaus as well as noise, so ties are covered
            w = tuple(float(rnd.choice([rnd.randint(-3, 3), rnd.uniform(-50, 50)])) for _ in range(3))
            hist.append(w)
            last = hist[-40:]
            want = max(max(s[k] for s in last) - min(s[k] for s in last) for k in range(3))
            self.assertEqual(eng._window_range(w), want, i)

    def test_engine_output_on_a_real_capture_is_stable(self):
        logging.disable(logging.CRITICAL)
        try:
            ctl = controller(m.NullPointer())
            aap = m.AAPClient("AA:BB:CC:DD:EE:FF")
            aap.pid, aap.sock = "2013", None
            with open(os.path.join(ROOT, "captures", "airpods3_glide_raw.json")) as f:
                frames = [bytes.fromhex(x[2]) for x in json.load(f)]
            for p in frames:
                ctl.on_packet(p, aap)
        finally:
            logging.disable(logging.NOTSET)
        self.assertTrue(ctl.engine.bias_ok)            # the still detection ran and settled
        self.assertEqual(len(ctl.engine.win), 40)


class CountingPointer(m.NullPointer):
    def __init__(self):
        super().__init__()
        self.abs_moves = 0

    def move_abs(self, x, y):
        self.abs_moves += 1


class PointerRenderTests(unittest.TestCase):
    def test_pointer_in_place_is_repeated_only_four_times_a_second(self):
        ptr = CountingPointer()
        ctl = controller(ptr)
        ctl.cfg["mode"] = "pointeur"
        ctl.paused = False
        ctl.target = ctl.cur = (0.3, 0.6)
        t = 1000.0
        ctl._last_render = t
        for i in range(120):                            # one second at 120 Hz, head still
            t += 1 / 120
            ctl.render(t)
        self.assertLessEqual(ptr.abs_moves, 5)
        self.assertGreaterEqual(ptr.abs_moves, 4)

    def test_pointer_moving_sends_every_frame(self):
        ptr = CountingPointer()
        ctl = controller(ptr)
        ctl.cfg["mode"] = "pointeur"
        ctl.paused = False
        ctl.cur, ctl.target = (0.1, 0.1), (0.9, 0.9)
        t = 1000.0
        ctl._last_render = t
        for i in range(30):
            t += 1 / 120
            ctl.render(t)
        self.assertEqual(ptr.abs_moves, 30)


class GestureWatchTests(unittest.TestCase):
    def test_gestures_page_asks_for_live_stats(self):
        with open(os.path.join(ROOT, "ui", "index.html"), encoding="utf-8") as f:
            self.assertIn('fetch("/api/state"+(page==="gestures"?"?watch=gestures":""))', f.read())
        with open(os.path.join(ROOT, "mousepods_web.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('self.path in ("/api/state", "/api/state?watch=gestures")', src)
        self.assertIn("g.watch()", src)

    def test_off_gestures_cost_nearly_nothing_per_sample(self):
        import gestures
        eng = gestures.GestureEngine(lambda: {"enabled": False})
        for i in range(200):
            eng.on_sample(i * 10_000_000, i / 100, (0, 0, 0), (0.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, 0.0, 1.0), (0, 0, 0))
        self.assertEqual(eng.buf.stats()["samples"], 0)


class MainLoopTests(unittest.TestCase):
    def test_no_render_frames_while_inactive(self):
        with open(os.path.join(ROOT, "airpods_mouse_controller.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("next_frame = now + (frame if not ctl.inactive else 0.25)", src)


if __name__ == "__main__":
    unittest.main()
