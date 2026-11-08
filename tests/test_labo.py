# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Matteo Gaudioso (starman-tech) — https://github.com/starman-tech/mousepods
"""Tests du Labo (labo.py) sur données synthétiques : statistiques, proposition de seuil, protocoles,
session guidée complète (claquements doux / moyens / forts, gauche / droite), mouvements, mots,
enregistrement étiqueté rejouable via l'interface web. Aucun matériel."""
import json
import math
import os
import random
import sys
import tempfile
import types as pytypes
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gestures as gz  # noqa: E402
import i18n  # noqa: E402
import labo  # noqa: E402

FS = 200.0
CAPTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "captures")


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


class TestStats(unittest.TestCase):
    def test_binom(self):
        self.assertAlmostEqual(labo.binom_sf(10, 10, 0.5), 0.5 ** 10)
        self.assertAlmostEqual(labo.binom_sf(0, 10, 0.5), 1.0)
        self.assertAlmostEqual(labo.binom_sf(5, 10, 0.5), 0.623046875)

    def test_separable_classes(self):
        rng = random.Random(1)
        X = [[1 + rng.gauss(0, 0.1), rng.gauss(0, 0.1), 0] for _ in range(12)] + \
            [[-1 + rng.gauss(0, 0.1), rng.gauss(0, 0.1), 0] for _ in range(12)]
        r = labo.loo_nearest_centroid(X, ["g"] * 12 + ["d"] * 12)
        self.assertEqual(r["accuracy"], 1.0)
        self.assertEqual(r["chance"], 0.5)
        self.assertLess(r["p_value"], 0.001)
        self.assertEqual(r["verdict"], i18n.tr("labo.verdict.separable"))

    def test_random_labels_are_chance(self):
        rng = random.Random(2)
        X = [[rng.gauss(0, 1), rng.gauss(0, 1), rng.gauss(0, 1)] for _ in range(40)]
        y = [rng.choice("ab") for _ in range(40)]
        r = labo.loo_nearest_centroid(X, y)
        self.assertGreater(r["p_value"], 0.05)
        self.assertLess(r["accuracy"], 0.75)

    def test_too_few(self):
        self.assertIn("error", labo.loo_nearest_centroid([[0, 0, 1]] * 3, ["a", "b", "a"]))

    def test_force_summary(self):
        cl = [{"group": "inc", "level": "doux", "peak": p} for p in (0.3, 0.4, 0.5)] + \
             [{"group": "inc", "level": "fort", "peak": p} for p in (2.0, 3.0)]
        f = labo.force_summary(cl)
        self.assertEqual(f["by_level"]["doux"]["median"], 0.4)
        self.assertEqual(f["by_group"]["inc"]["n"], 5)
        self.assertEqual(f["by_group_level"]["inc:fort"]["max"], 3.0)

    def test_propose_threshold(self):
        p = labo.propose_threshold([0.4, 0.45, 0.5, 0.55, 0.6], [0.15], 0.1)
        self.assertGreater(p["click_thresh"], 0.25)
        self.assertLessEqual(p["click_thresh"], 0.4)
        self.assertEqual(p["soft_detect"], 1.0)
        # bruit plus fort que les claquements doux : seuil limité par le bruit, honnêtement signalé
        q = labo.propose_threshold([0.3, 0.35, 0.4], [0.5], 0.2)
        self.assertAlmostEqual(q["click_thresh"], 0.6)
        self.assertEqual(q["reason"], i18n.tr("labo.reason.noise_limited"))
        self.assertEqual(q["soft_detect"], 0.0)
        self.assertIsNone(labo.propose_threshold([], [0.1])["click_thresh"])

    def test_detection_rates(self):
        cues = [{"phase": "hoche", "expect": ("nod",), "t0": 0, "t1": 3}, {"phase": "hoche", "expect": ("nod",), "t0": 3, "t1": 6},
                {"phase": "pause", "t0": 6, "t1": 7}]
        r = labo.detection_rates(cues, [{"type": "nod", "wall": 1.0}, {"type": "shake", "wall": 4.0}])
        self.assertEqual(r["hoche"]["hits"], 1)
        self.assertEqual(r["hoche"]["rate"], 0.5)
        self.assertEqual(r["hoche"]["other"], {"shake": 1})


class TestProtocols(unittest.TestCase):
    def test_shapes(self):
        d = labo.build_protocol("dents")
        clicks = [s for s in d if s["kind"] == "click"]
        self.assertEqual(len(clicks), 3 * 3 * 8)
        self.assertEqual({(s["group"], s["level"]) for s in clicks},
                         {(g, lv) for g, _, _ in labo.TEETH_GROUPS for lv, _ in labo.FORCE_LEVELS})
        self.assertEqual(d[0]["phase"], "repos")
        for a, b in zip(d, d[1:]):
            self.assertAlmostEqual(a["t1"], b["t0"])
        mv = labo.build_protocol("mouvements")
        self.assertEqual(sum(s["kind"] == "move" for s in mv), 30)
        w = labo.build_protocol("mots", seed=1)
        words = [s["word"] for s in w if s["kind"] == "word"]
        self.assertEqual(len(words), 50)
        self.assertEqual(set(words), set(labo.DEFAULT_WORDS))
        with self.assertRaises(ValueError):
            labo.build_protocol("inconnu")


def sample(t, a=(0.0, 0.0, 0.0), r=(0.0, 0.0, 0.0), seg=0):
    return gz.Sample(t, t, (1.0, 0, 0, 0), tuple(r), tuple(a), (0.0, 0.0, 1000.0), tuple(r), False, seg)


class Driver:
    """Fait avancer l'horloge et alimente LiveTap échantillon par échantillon (200 Hz)."""

    def __init__(self, sess, tap, clock, seed=0):
        self.sess, self.tap, self.clock = sess, tap, clock
        self.rng = random.Random(seed)
        tap.listeners.append(self.listen)

    def listen(self, kind, obj):
        self.sess.on_tap(kind, obj)
        if kind == "hp":
            self.sess.tick()

    def run(self, until, acc=lambda t: None, rate=lambda t: None):
        while self.clock.t < until and not self.sess.done:
            t = self.clock.t
            a = acc(t) or (0.0, 0.0, 0.0)
            a = tuple(v + self.rng.gauss(0, 0.02) for v in a)
            self.tap.on_sample(sample(t, a, rate(t) or (0.0, 0.0, 0.0)), "raw")
            self.clock.t = t + 1 / FS


class TestSession(unittest.TestCase):
    DIR = {"inc": (0.2, 1.0, 0.0), "mol_g": (1.0, 0.2, 0.0), "mol_d": (-1.0, 0.2, 0.0)}
    AMP = {"doux": 0.5, "moyen": 1.2, "fort": 3.0}

    def test_teeth_session(self):
        clock = Clock()
        phases = []
        steps = labo.build_protocol("dents", reps=3)
        sess = labo.LaboSession("dents", steps, clock, phases.append, "x.json")
        tap = labo.LiveTap()
        drv = Driver(sess, tap, clock)
        t0 = sess.t0
        clicks = {}
        for s in steps:
            if s["kind"] == "click":
                tc = t0 + s["t0"] + 0.5
                clicks[round(tc * FS)] = (self.DIR[s["group"]], self.AMP[s["level"]])

        def acc(t):
            k = round(t * FS)
            for kk in (k, k - 1):           # pic de 10 ms
                if kk in clicks:
                    d, amp = clicks[kk]
                    return tuple(amp * x for x in d)
            return None
        drv.run(t0 + sess.total + 0.5, acc)
        self.assertTrue(sess.done)
        r = sess.result
        self.assertEqual(phases[0], "repos")
        self.assertIn("dents_mol_g_doux", phases)
        self.assertEqual(phases[-1], "")
        self.assertEqual(r["captured"], {"cues": 27, "with_impulse": 27})
        f = r["force"]["by_level"]
        self.assertLess(f["doux"]["median"], f["moyen"]["median"])
        self.assertLess(f["moyen"]["median"], f["fort"]["median"])
        sep = r["separability"]
        self.assertEqual(sep["mol_g_vs_mol_d"]["accuracy"], 1.0)
        self.assertLess(sep["mol_g_vs_mol_d"]["p_value"], 0.01)
        self.assertEqual(sep["inc_vs_mol"]["accuracy"], 1.0)
        p = r["proposal"]
        self.assertIsNotNone(p["click_thresh"])
        self.assertLessEqual(p["click_thresh"], f["doux"]["min"])
        self.assertGreater(p["click_thresh"], r["noise"]["p99"])
        self.assertEqual(r["at_proposal"]["fort"], 1.0)
        c = r["clicks"][0]
        for k in ("peak", "dur_ms", "axis", "vec", "gyro_mean", "gyro_max", "group", "level"):
            self.assertIn(k, c)
        d = sess.as_dict()
        self.assertTrue(d["done"])
        i18n.encode_json(d)                              # serializable for /api/labo

    def test_movement_session(self):
        clock = Clock()
        steps = labo.build_protocol("mouvements", reps=2)
        sess = labo.LaboSession("mouvements", steps, clock)
        tap = labo.LiveTap()
        drv = Driver(sess, tap, clock)
        nods = [sess.t0 + s["t0"] + 0.6 for s in steps if s["kind"] == "move" and s["phase"] == "hoche"]

        def rate(t):
            for tn in nods:
                if tn <= t < tn + 0.5:
                    return (0.0, -120 * math.sin(2 * math.pi * (t - tn) / 0.5), 0.0)
            return None
        drv.run(sess.t0 + sess.total + 0.5, rate=rate)
        rt = sess.result["rates"]
        self.assertEqual(rt["hoche"]["hits"], 2)
        self.assertEqual(rt["hoche"]["rate"], 1.0)
        self.assertEqual(rt["secoue"]["hits"], 0)
        self.assertEqual(set(rt), {ph for ph, _, _ in labo.MOVES})

    def test_cancel(self):
        clock = Clock()
        phases = []
        sess = labo.LaboSession("dents", labo.build_protocol("dents"), clock, phases.append)
        clock.t += 2
        sess.tick()
        sess.cancel()
        self.assertTrue(sess.done and sess.cancelled)
        self.assertEqual(phases[-1], "")
        self.assertTrue(sess.analyze()["cancelled"])

    def test_words_evaluation(self):
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("numpy absent")
        clock = Clock()
        sess = labo.LaboSession("mots", labo.build_protocol("mots", reps=6, words=["un", "deux"], seed=3), clock)
        rng = random.Random(4)
        for w in ("un", "deux") * 6:
            smp = []
            for i in range(100):
                t = i / 50.0
                bump = math.exp(-((t - 1.0) / 0.25) ** 2)
                a = (bump * 0.8 + rng.gauss(0, 0.02), rng.gauss(0, 0.02), rng.gauss(0, 0.02)) if w == "un" else \
                    (rng.gauss(0, 0.02), rng.gauss(0, 0.02), -bump * 0.8 + rng.gauss(0, 0.02))
                smp.append((t, None, (0.0, 0.0, 0.0), a))
            sess.word_samples.append((w, smp))
        ev = sess.evaluate_words(n_perm=60)
        self.assertTrue(ev["experimental"])
        self.assertGreaterEqual(ev["loo_accuracy"], 0.9)
        self.assertLess(ev["p_value"], 0.05)

    def test_word_buffer_follows_cues(self):
        clock = Clock()
        steps = labo.build_protocol("mots", reps=1, words=["a", "b"], seed=0)
        sess = labo.LaboSession("mots", steps, clock)
        while not sess.done:
            sess.on_dm6(int(clock.t * 1e9), None, (0, 0, 0), (0, 0, 0))
            clock.t += 0.02
            sess.tick()
        self.assertEqual(sorted(w for w, _ in sess.word_samples), ["a", "b"])
        self.assertTrue(all(90 <= len(s) <= 112 for _, s in sess.word_samples))   # 2,2 s à 50 Hz


class TestWebRecording(unittest.TestCase):
    """Session lancée par l'interface : trames brutes étiquetées [t, phase, hex] dans captures/private/,
    rejouables par gestures.load_recording."""

    def test_labeled_recording_replays(self):
        src = os.path.join(CAPTURES, "gestures_labeled.json")
        if not os.path.exists(src):
            self.skipTest("gestures_labeled.json absent")
        import mousepods_web as mw
        with open(src) as f:
            frames = json.load(f)[:1500]
        ctl = pytypes.SimpleNamespace(gesture_hooks=[], gestures=pytypes.SimpleNamespace(sample_taps=[]),
                                      cfg={"gestures": {}})
        with tempfile.TemporaryDirectory() as d:
            old = mw.HERE
            mw.HERE = d
            try:
                ui = mw.WebUI(ctl, os.path.join(d, "config.json"), port=0)
                self.assertTrue(ctl.gestures.sample_taps)              # LiveTap branché
                info = ui.labo_start({"protocol": "dents", "reps": 2})
                self.assertTrue(info["path"].startswith(os.path.join(d, "captures", "private")))
                for _, _, hx in frames:
                    ui.feed_packet(bytes.fromhex(hx))
                ui.labo_cancel()
                ui.feed_packet(bytes.fromhex(frames[-1][2]))          # après « until » -> fichier écrit
                with open(info["path"]) as f:
                    rows = json.load(f)
                self.assertTrue(all(len(r) == 3 and isinstance(r[2], str) for r in rows))
                self.assertEqual(rows[0][1], "repos")
                rec = gz.load_recording(info["path"], "raw")
                self.assertGreater(len(rec), 100)
                self.assertEqual(rec[0]["label"], "repos")
                st = ui.labo_state()
                self.assertTrue(st["session"]["cancelled"])
                i18n.encode_json(st)
            finally:
                mw.HERE = old


if __name__ == "__main__":
    unittest.main()
