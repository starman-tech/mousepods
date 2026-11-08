# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Matteo Gaudioso (starman-tech) — https://github.com/starman-tech/mousepods
"""Tests : acquisition (trous, interpolation, doublons, spectre), détecteurs de gestes, activation,
rejets (marche, mastication), enregistrement CSV + rejeu, calibration, rejeu des captures réelles."""
import contextlib
import io
import math
import os
import random
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import airpods_mouse_controller as m  # noqa: E402
import gestures as gz  # noqa: E402
import i18n  # noqa: E402

CAPTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "captures")
FS = 200.0


class Synth:
    """Générateur de signaux synthétiques à 200 Hz (horodatage capteur en ns)."""

    def __init__(self, seed=1):
        self.t = 1.0
        self.rows = []
        self.rng = random.Random(seed)

    def add(self, secs, rate=lambda t: (0, 0, 0), acc=lambda t: (0, 0, 0), skip=None):
        n = int(round(secs * FS))
        for i in range(n):
            tl = i / FS
            a = tuple(v + self.rng.gauss(0, 0.03) for v in acc(tl))
            r = tuple(rate(tl))
            if not (skip and skip(tl)):
                self.rows.append({"ts_ns": int(round(self.t * 1e9)), "arrival": self.t, "q": (1.0, 0, 0, 0),
                                  "w": r, "a": a, "g": (0.0, 0.0, 1000.0), "r": r, "label": ""})
            self.t += 1 / FS
        return self

    def still(self, secs):
        return self.add(secs)

    def click(self, amp=3.0):
        # pic bref (10 ms) sur x, typique d'un choc des dents transmis par l'os
        return self.add(0.01, acc=lambda t: (amp, 0.3 * amp, 0.0))

    def events(self, **cfg):
        ev, st, rej, _ = gz.replay(self.rows, {"require_activation": False, **cfg})
        return ev, st, rej


def types(evs):
    return [e["type"] for e in evs]


class TestAcquisition(unittest.TestCase):
    def push_seq(self, buf, times, val=lambda i: (float(i), 0.0, 0.0)):
        out = []
        for i, t in enumerate(times):
            out += buf.push(int(round(t * 1e9)), t, None, val(i), (0, 0, 0), (0, 0, 1000.0), (0, 0, 0))
        return out

    def test_short_gap_is_interpolated(self):
        buf = gz.SensorBuffer(0.03)
        times = [i * 0.005 for i in range(50)] + [0.245 + 0.025 + i * 0.005 for i in range(20)]
        out = self.push_seq(buf, times)
        st = buf.stats()
        self.assertEqual(st["gaps"], 0)
        self.assertEqual(st["interpolated"], 4)
        self.assertEqual(len({s.seg for s in out}), 1)
        mids = [s for s in out if s.interp]
        self.assertTrue(all(out[49].w[0] < s.w[0] < out[54].w[0] for s in mids))   # interpolation linéaire
        self.assertAlmostEqual(st["nominal_hz"], 200, delta=2)

    def test_long_gap_starts_new_segment(self):
        buf = gz.SensorBuffer(0.03)
        times = [i * 0.005 for i in range(50)] + [0.245 + 0.130 + i * 0.005 for i in range(20)]
        out = self.push_seq(buf, times)
        st = buf.stats()
        self.assertEqual(st["gaps"], 1)
        self.assertEqual(st["max_gap_ms"], 130)
        self.assertEqual(st["interpolated"], 0)
        self.assertEqual(out[-1].seg, out[0].seg + 1)
        self.assertEqual(len(buf.window(10.0)), 20)                     # fenêtre limitée au segment

    def test_duplicates_and_out_of_order(self):
        buf = gz.SensorBuffer(0.03)
        times = [i * 0.005 for i in range(200)]
        self.push_seq(buf, times, val=lambda i: (float(i // 2), 0.0, 0.0))   # 100 Hz suréchantillonné à 200
        buf.push(int(0.5 * 1e9), 0, None, (0, 0, 0), (0, 0, 0), (0, 0, 1000.0))   # horodatage en arrière
        st = buf.stats()
        self.assertAlmostEqual(st["dup_ratio"], 0.5, delta=0.02)
        self.assertTrue(st["upsampled_suspect"])
        self.assertEqual(st["dropped"], 1)
        self.assertAlmostEqual(st["rate_hz"], 200, delta=2)

    def test_spectrum_energy_above(self):
        for f_sig, high in ((60.0, True), (5.0, False)):
            buf = gz.SensorBuffer(0.03)
            for i in range(400):
                t = i / FS
                buf.push(int(t * 1e9) + 10 ** 9, t, None, (0, 0, 0), (math.sin(2 * math.pi * f_sig * t), 0, 0),
                         (0, 0, 1000.0))
            frac = buf.energy_above(50.0)
            self.assertGreater(frac, 0.8) if high else self.assertLess(frac, 0.05)


class TestClicks(unittest.TestCase):
    def test_single_click(self):
        ev, _, _ = Synth().still(1).click().still(1).events()
        self.assertEqual(types(ev), ["teeth_click"])
        self.assertGreater(ev[0]["conf"], 0.5)
        self.assertEqual(ev[0]["axis"], "+x")
        self.assertIsNone(ev[0]["side"])            # côté inconnu tant que l'utilisateur ne l'a pas réglé

    def test_side_when_axis_configured(self):
        ev, _, _ = Synth().still(1).click().still(1).events(click_side_axis="-x")
        self.assertEqual(ev[0]["side"], "droite")

    def test_double_click(self):
        ev, _, _ = Synth().still(1).click().still(0.2).click().still(1).events()
        self.assertEqual(types(ev), ["teeth_double"])
        self.assertAlmostEqual(ev[0]["interval"], 0.21, delta=0.02)

    def test_too_slow_is_two_singles(self):
        ev, _, _ = Synth().still(1).click().still(0.9).click().still(1).events()
        self.assertEqual(types(ev), ["teeth_click", "teeth_click"])

    def test_small_bump_ignored(self):
        ev, _, _ = Synth().still(1).click(amp=0.5).still(1).events()
        self.assertEqual(ev, [])

    def test_chewing_rejected(self):
        s = Synth().still(1)
        for _ in range(8):
            s.click(amp=2.5).still(0.34)
        ev, _, rej = s.still(1).events()
        self.assertNotIn("teeth_double", types(ev))
        self.assertEqual(ev, [])
        self.assertGreaterEqual(rej["chew"], 1)

    def test_click_during_head_rotation_ignored(self):
        # vraie rotation de tête (0,3 s à 120 °/s) autour du pic : rejeté
        s = Synth().still(1).add(0.15, rate=lambda t: (120, 0, 0)).add(0.01, rate=lambda t: (120, 0, 0), acc=lambda t: (3, 1, 0))
        ev, _, _ = s.add(0.15, rate=lambda t: (120, 0, 0)).still(1).events()
        self.assertNotIn("teeth_click", types(ev))

    def test_click_gyro_jolt_accepted(self):
        # mesuré sur AirPods 3 : un claquement secoue le gyroscope ~10 ms (jusqu'à ~80 °/s) -> accepté
        ev, _, _ = Synth().still(1).add(0.01, rate=lambda t: (80, 0, 0), acc=lambda t: (3, 1, 0)).still(1).events()
        self.assertEqual(types(ev), ["teeth_click"])

    def test_no_double_across_gap(self):
        s = Synth().still(1).click().add(0.2, skip=lambda t: 0.05 < t < 0.15).click().still(1)
        ev, st, _ = s.events()
        self.assertNotIn("teeth_double", types(ev))
        self.assertEqual(st["gaps"], 1)


class TestHead(unittest.TestCase):
    def test_nod(self):
        ev, _, _ = Synth().still(1).add(0.5, rate=lambda t: (0, -120 * math.sin(2 * math.pi * t / 0.5), 0)).still(1).events()
        self.assertEqual(types(ev), ["nod"])
        self.assertEqual(ev[0]["start"], "bas")
        self.assertAlmostEqual(ev[0]["peak"], 120 * 0.25 * 2 / math.pi, delta=1.5)

    def test_shake(self):
        ev, _, _ = Synth().still(1).add(0.6, rate=lambda t: (150 * math.sin(2 * math.pi * 2.5 * t), 0, 0)).still(1).events()
        self.assertEqual(types(ev), ["shake"])

    def test_shake_needs_three_lobes(self):
        ev, _, _ = Synth().still(1).add(0.4, rate=lambda t: (150 * math.sin(2 * math.pi * 2.5 * t), 0, 0)).still(1).events()
        self.assertEqual(ev, [])

    def test_diagonal_motion_not_nod(self):
        f = lambda t: (110 * math.sin(2 * math.pi * t / 0.5), -120 * math.sin(2 * math.pi * t / 0.5), 0)  # noqa: E731
        ev, _, _ = Synth().still(1).add(0.5, rate=f).still(1).events()
        self.assertNotIn("nod", types(ev))

    def test_tilt_left_right(self):
        for sign, name in ((1, "tilt_right"), (-1, "tilt_left")):
            s = Synth().still(1).add(0.4, rate=lambda t: (0, 0, sign * 60)).still(0.5).add(0.4, rate=lambda t: (0, 0, -sign * 60)).still(1)
            self.assertEqual(types(s.events()[0]), [name])

    def test_look_up_held(self):
        s = Synth().still(1).add(0.4, rate=lambda t: (0, 60, 0)).still(0.8).add(0.4, rate=lambda t: (0, -60, 0)).still(1)
        ev = s.events()[0]
        self.assertEqual(types(ev), ["look_up"])
        self.assertGreater(ev[0]["peak"], 20)

    def test_look_down_not_held_long_enough(self):
        s = Synth().still(1).add(0.4, rate=lambda t: (0, -60, 0)).still(0.45).add(0.4, rate=lambda t: (0, 60, 0)).still(1)
        self.assertEqual(s.events()[0], [])

    def test_look_down_held(self):
        s = Synth().still(1).add(0.4, rate=lambda t: (0, -60, 0)).still(0.8).add(0.4, rate=lambda t: (0, 60, 0)).still(1)
        self.assertEqual(types(s.events()[0]), ["look_down"])


class TestRejection(unittest.TestCase):
    def test_walking_suppresses_clicks(self):
        walk = lambda t: (0, 0, 2.0 * math.sin(2 * math.pi * 1.8 * t))  # noqa: E731
        s = Synth().still(1).add(4, acc=walk)
        for _ in range(3):
            s.add(0.01, acc=lambda t: (3, 1, 0)).add(0.2, acc=walk).add(0.01, acc=lambda t: (3, 1, 0)).add(1.0, acc=walk)
        ev, _, rej = s.events()
        self.assertEqual(ev, [])
        self.assertGreaterEqual(rej["walk"], 1)

    def test_still_is_not_walking(self):
        _, _, rej = Synth().still(5).events()
        self.assertEqual(rej, {"walk": 0, "chew": 0, "motion": 0})

    def test_large_motion_suppresses(self):
        s = Synth().still(1).add(0.3, rate=lambda t: (500, 0, 0)).click().still(1)
        ev, _, rej = s.events()
        self.assertNotIn("teeth_click", types(ev))
        self.assertGreaterEqual(rej["motion"], 1)


class TestActivation(unittest.TestCase):
    def nod(self, s):
        return s.add(0.5, rate=lambda t: (0, -120 * math.sin(2 * math.pi * t / 0.5), 0))

    def test_nod_blocked_without_activation(self):
        s = self.nod(Synth().still(1)).still(1)
        ev, _, _, _ = gz.replay(s.rows, {})
        self.assertEqual(types(ev), ["nod"])
        self.assertIsNone(ev[0]["action"])
        self.assertEqual(ev[0]["blocked"], i18n.tr("gest.blocked.outside_window"))

    def test_double_click_opens_window(self):
        s = Synth().still(1).click().still(0.2).click().still(0.7)
        s = self.nod(s).still(1)
        ev, _, _, _ = gz.replay(s.rows, {})
        self.assertEqual(types(ev), ["teeth_double", "nod"])
        self.assertEqual(ev[0]["action"], "activation")
        self.assertEqual(ev[1]["action"], "left_click")

    def test_window_expires(self):
        s = Synth().still(1).click().still(0.2).click().still(2.5)
        s = self.nod(s).still(1)
        ev, _, _, _ = gz.replay(s.rows, {})
        self.assertIsNone(ev[1]["action"])

    def test_one_action_per_window(self):
        r = gz.GestureRouter()
        c = gz.gesture_cfg()
        r.route({"t": 0.0, "type": "teeth_double"}, c)
        self.assertEqual(r.route({"t": 0.5, "type": "nod"}, c), "left_click")
        self.assertIsNone(r.route({"t": 0.9, "type": "shake"}, c))


class TestEngine(unittest.TestCase):
    def feed(self, eng, rows):
        for r in rows:
            eng.on_sample(r["ts_ns"], r["arrival"], (0, 0, 0), r["w"], r["g"], r["a"], r["r"])

    def test_csv_record_and_replay(self):
        cmds = []
        eng = gz.GestureEngine(lambda: {"enabled": True}, command=cmds.append)
        s = Synth().still(1).click().still(0.2).click().still(0.7)
        s.add(0.5, rate=lambda t: (0, -120 * math.sin(2 * math.pi * t / 0.5), 0)).still(1)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "essai.csv")
            eng.start_record(path, "nod")
            self.feed(eng, s.rows)
            info = eng.stop_record()
            self.assertEqual(info["rows"], len(s.rows))
            self.assertEqual(cmds, ["left_click"])
            rows = gz.load_recording(path)
            self.assertEqual(rows[0]["label"], "nod")
            ev, _, _, _ = gz.replay(rows, {})
            self.assertEqual(types(ev), ["teeth_double", "nod"])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(gz.main(["--replay", path, "--config", "/nonexistent.json", "--stats"]), 0)
            self.assertIn("teeth_double", out.getvalue())
        summ = eng.summary()
        self.assertEqual([e["type"] for e in summ["events"]], ["teeth_double", "nod"])
        self.assertTrue(summ["trace"])

    def test_disabled_does_nothing(self):
        cmds = []
        eng = gz.GestureEngine(lambda: {"enabled": False}, command=cmds.append)
        self.feed(eng, Synth().still(1).click().still(0.2).click().still(1).rows)
        self.assertEqual(list(eng.events), [])
        self.assertGreater(eng.buf.stats()["samples"], 0)

    def test_user_calibration_sets_threshold(self):
        saved = {}
        eng = gz.GestureEngine(lambda: {"enabled": True}, save=saved.update)
        eng.start_calib("nod")
        s = Synth().still(1)
        for _ in range(10):        # hochements faibles (~10°) : sous le seuil par défaut mais détectés en calibration
            s.add(0.5, rate=lambda t: (0, -65 * math.sin(2 * math.pi * t / 0.5), 0)).still(0.6)
        self.feed(eng, s.rows)
        self.assertTrue(eng.calib["done"])
        amp = 65 * 0.25 * 2 / math.pi
        self.assertAlmostEqual(saved["nod_deg"], 0.6 * amp, delta=1.0)

    def test_controller_gesture_hook(self):
        cfg = m.load_config("/nonexistent.json")
        cfg["notifications"] = False
        ctl = m.Controller(cfg, m.NullPointer())
        got = []
        ctl.gesture_hooks.append(lambda *a: got.append(a))
        b = bytearray(58)
        struct.pack_into("<Q", b, 1, 123456789)
        struct.pack_into("<hhh", b, 20, 0, 0, 1000)
        struct.pack_into("<hhh", b, 38, 100, 0, 0)
        struct.pack_into("<hhh", b, 44, 0, 0, 1000)
        ctl.on_block(bytes(b), 5.0)
        ts, now, att, w, g, a, rates = got[0]
        self.assertEqual((ts, now, att), (123456789, 5.0, (0, 0, 1000)))
        self.assertAlmostEqual(a[0], 0.981, places=3)
        self.assertEqual(len(rates), 3)


def raw_payload(ts_us, xyz):
    """Rapport HID accel/gyro brut (29 o) au format observé sur AirPods 3."""
    b = bytearray(29)
    b[1] = 1
    struct.pack_into("<iii", b, 2, *[int(round(v / gz.RAW_SCALE)) for v in xyz])
    b[21:29] = int(ts_us).to_bytes(8, "little")
    return bytes(b)


class FakeAAP:
    def __init__(self):
        self.rhid_devs = {"devmotion6": 7, "accel": 4, "gyro": 5}
        self.rhid_active, self.imu_kinds, self.sock, self.sent = {}, {}, True, []

    def rhid_set_feature(self, dev, rid, value):
        self.sent.append((dev, rid, value))


class TestRawImu(unittest.TestCase):
    def test_parse_and_fuse(self):
        f = gz.RawImuFuser()
        out = []
        for i in range(400):
            ts = 10_000_000 + i * 4967
            out += f.push("w", raw_payload(ts, (0.1, 0.2, 0.3)), i * 0.005)
            out += f.push("a", raw_payload(ts, (0.0, 0.0, 1.0)), i * 0.005)
        self.assertEqual(len(out), 400)
        self.assertEqual(out[1]["ts_ns"] - out[0]["ts_ns"], 4967000)       # horodatage capteur (µs -> ns)
        self.assertEqual([round(v, 6) for v in out[-1]["w"]], [0.1, 0.2, 0.3])
        self.assertLess(gz.vlen(out[-1]["a"]), 0.3)                        # gravité retirée
        self.assertAlmostEqual(gz.vlen(out[-1]["g"]), 1000, delta=30)

    def test_accel_without_gyro_still_emitted(self):
        f = gz.RawImuFuser()
        out = []
        for i in range(20):
            out += f.push("a", raw_payload(i * 5000, (0, 0, 1.0)), 0)
        self.assertGreaterEqual(len(out), 15)
        self.assertGreater(f.n_accel_only, 0)

    def test_engine_prefers_raw_and_clicks(self):
        eng = gz.GestureEngine(lambda: {"enabled": True, "require_activation": False})
        t_us = 5_000_000
        clicks = {200, 600}
        for i in range(800):
            t_us += 4967
            acc = (0.0, 0.0, 1.0) if i not in clicks else (0.3, 0.1, 1.0)        # +0,3 g pendant 5 ms
            eng.on_raw("w", raw_payload(t_us, (0, 0, 0)), i * 0.005)
            eng.on_raw("a", raw_payload(t_us, acc), i * 0.005)
            # devmotion6 en parallèle : ignoré tant que l'IMU brute arrive
            eng.on_sample(999_000_000_000 + i * 20_000_000, i * 0.005, (0, 0, 0), (0, 0, 0), (0, 0, 1000), (0, 0, 0),
                          (0, 0, 0))
        self.assertEqual(eng.source, "raw")
        self.assertEqual([e["type"] for e in eng.events], ["teeth_click", "teeth_click"])
        self.assertTrue(all(e["src"] == "raw" for e in eng.events))
        self.assertAlmostEqual(eng.buf.stats()["nominal_hz"], 201.3, delta=1)

    def test_falls_back_to_devmotion6(self):
        eng = gz.GestureEngine(lambda: {"enabled": True})
        eng.on_raw("a", raw_payload(1000, (0, 0, 1)), 0.0)
        for i in range(10):
            eng.on_sample(10 ** 9 + i * 10 ** 7, 1.0 + i * 0.01, (0, 0, 0), (0, 0, 0), (0, 0, 1000), (0, 0, 0), (0, 0, 0))
        self.assertEqual(eng.source, "dm6")

    def test_controller_starts_raw_streams_only_when_enabled(self):
        cfg = m.load_config("/nonexistent.json")
        cfg["notifications"] = False
        ctl = m.Controller(cfg, m.NullPointer())
        aap = FakeAAP()
        ctl.raw_imu_hooks.append(lambda *a: None)
        ctl.sync_raw_imu(aap)
        self.assertEqual(aap.sent, [])                                  # gestes désactivés
        self.assertEqual(ctl.motion_interval_us(), cfg["sensor_interval_us"])
        cfg["gestures"]["enabled"] = True
        ctl.sync_raw_imu(aap)
        self.assertEqual(sorted(aap.sent), [(4, 1, 5000), (5, 1, 5000)])
        self.assertEqual(aap.rhid_active, {4: 5000, 5: 5000})          # relancés après réannonce
        self.assertEqual(ctl.motion_interval_us(), 20000)
        ctl.sync_raw_imu(aap)
        self.assertEqual(len(aap.sent), 2)                              # idempotent
        cfg["gestures"]["use_raw_imu"] = False
        ctl.sync_raw_imu(aap)
        self.assertEqual(aap.sent[-2:], [(4, 1, 0), (5, 1, 0)])
        self.assertEqual(aap.rhid_active, {})
        self.assertEqual(ctl.motion_interval_us(), cfg["sensor_interval_us"])

    def test_raw_reports_reach_hooks(self):
        cfg = m.load_config("/nonexistent.json")
        cfg["notifications"] = False
        ctl = m.Controller(cfg, m.NullPointer())
        aap = m.AAPClient(auto_connect=False)
        aap.imu_kinds = {4: "a"}
        got = []
        ctl.raw_imu_hooks.append(lambda kind, pl, now: got.append((kind, pl)))
        pl = raw_payload(123, (0, 0, 1))
        ctl.on_packet(m.rhid_frame(1, [m.rhid_sub(4, m.RHID_INPUT, pl)]), aap)
        self.assertEqual(got, [("a", pl)])


class TestRealCaptures(unittest.TestCase):
    def test_motion_capture_replay(self):
        rows = gz.load_recording(os.path.join(CAPTURES, "airpods3_motion_capture.json"))
        ev, st, _, buf = gz.replay(rows, {})
        self.assertGreater(len(rows), 3000)
        self.assertTrue(150 < st["nominal_hz"] < 220, st)
        self.assertLess(st["dup_ratio"], 0.1)
        self.assertLess(len(ev), 30)                    # pas d'avalanche de faux positifs
        self.assertFalse(any(e["action"] for e in ev))  # aucune action sans activation

    def test_rhid_raw_replay(self):
        rows = gz.load_recording(os.path.join(CAPTURES, "airpods3_glide_raw2.json"))
        ev, st, _, _ = gz.replay(rows, {})
        self.assertGreater(len(rows), 500)
        self.assertGreater(st["short_gaps"] + st["gaps"], 0)        # flux ~50 Hz avec pertes
        self.assertTrue(30 < st["rate_hz"] < 220, st)


if __name__ == "__main__":
    unittest.main()


class TestSoftClicks(unittest.TestCase):
    """Seuil bas (claquements doux) : le seuil ne doit plus modifier la durée mesurée, les rebonds ni
    la mastication ; le seuil adaptatif remonte dans le bruit."""

    @staticmethod
    def ringing(amp=3.0, f=40.0, tau=0.05):
        # claquement fort + « ring » amorti (reste > 0,1 m/s² ~150 ms)
        return lambda t: (amp * math.exp(-t / tau) * math.cos(2 * math.pi * f * t), 0.3 * amp * math.exp(-t / tau), 0.0)

    def test_strong_ringing_click_kept_at_low_threshold(self):
        for thr in (0.3, 0.6):
            ev, _, _ = Synth().still(1).add(0.2, acc=self.ringing()).still(1).events(click_thresh=thr)
            self.assertEqual(types(ev), ["teeth_click"], thr)

    def test_soft_click_needs_low_threshold(self):
        s = Synth().still(2).click(amp=0.45).still(1)
        self.assertEqual(s.events(click_thresh=0.6)[0], [])
        self.assertEqual(types(s.events(click_thresh=0.25)[0]), ["teeth_click"])

    def test_jaw_release_echo_ignored(self):
        s = Synth().still(1).click(amp=3.0).still(0.3).click(amp=0.5).still(1)
        ev, _, rej = s.events(click_thresh=0.3)
        self.assertEqual(types(ev), ["teeth_click"])
        self.assertEqual(rej["chew"], 0)

    def test_clicks_with_echoes_are_not_chewing(self):
        s = Synth().still(1)
        for _ in range(3):
            s.click(amp=2.0).still(0.3).click(amp=0.4).still(0.7)
        ev, _, rej = s.still(1).events(click_thresh=0.3)
        self.assertEqual(types(ev), ["teeth_click"] * 3)
        self.assertEqual(rej["chew"], 0)

    def test_small_pulse_does_not_mask_second_click(self):
        s = Synth().still(1).click(amp=1.0).still(0.06).click(amp=0.32).still(0.12).click(amp=1.0).still(1)
        self.assertEqual(types(s.events(click_thresh=0.6)[0]), ["teeth_double"])

    def test_long_jolt_cancels_pending_click(self):
        jolt = lambda t: (2.5 * math.sin(2 * math.pi * 6 * t), 0, 2.0)  # noqa: E731
        s = Synth().still(1).click(amp=1.5).still(0.15).add(0.3, acc=jolt).still(1)
        self.assertNotIn("teeth_click", types(s.events(click_thresh=0.6)[0]))

    def test_adaptive_threshold_rises_with_noise(self):
        rng = random.Random(3)
        talk = lambda t: (rng.gauss(0, 0.06), rng.gauss(0, 0.06), rng.gauss(0, 0.06))  # noqa: E731  (≈ parole)
        s = Synth().still(1).add(3, acc=talk).click(amp=0.45).add(1, acc=talk)
        ev, _, _ = s.events(click_thresh=0.25)
        self.assertNotIn("teeth_click", types(ev))                      # seuil relevé par le bruit
        strong = Synth().still(1).add(3, acc=talk).click(amp=3.0).add(1, acc=talk)
        self.assertIn("teeth_click", types(strong.events(click_thresh=0.25)[0]))   # un vrai claquement passe
        det = gz.GestureDetector({"click_thresh": 0.25})
        buf = gz.SensorBuffer()
        for r in s.rows[:800]:
            for smp in buf.push(r["ts_ns"], r["arrival"], r["q"], r["w"], r["a"], r["g"], r["r"]):
                det.feed(smp)
        self.assertGreater(det.click_threshold(), 0.35)

    def test_click_event_carries_features(self):
        ev, _, _ = Synth().still(1).click(amp=2.0).still(1).events()
        e = ev[0]
        for k in ("vec", "dur_ms", "gyro_mean", "gyro_max", "thr"):
            self.assertIn(k, e)
        self.assertGreater(e["vec"][0], 0)
        self.assertLessEqual(e["dur_ms"], 120)
