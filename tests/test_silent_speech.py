# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Matteo Gaudioso (starman-tech) — https://github.com/starman-tech/mousepods
"""Tests (données synthétiques) : DTW, reconnaissance 1-NN, rejet, leave-one-out + test de permutation."""
import math
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import i18n  # noqa: E402
try:
    import numpy as np
    import silent_speech as ss
except ImportError:  # numpy absent : module expérimental non testé
    ss = None

FS = 200.0


def take(pattern, rng, warp=0.15, noise=0.02):
    """Une prise synthétique d'un « mot » : motif temporel déformé (vitesse, amplitude) + bruit."""
    dur = 0.8 * (1 + rng.uniform(-warp, warp))
    amp = 1 + rng.uniform(-0.2, 0.2)
    lead = int(rng.uniform(0.1, 0.25) * FS)
    n = int(dur * FS)
    out = []
    t0 = 0.0
    for i in range(lead + n + lead):
        u = (i - lead) / n
        a, w = pattern(u) if 0 <= u <= 1 else ((0, 0, 0), (0, 0, 0))
        a = tuple(amp * v + rng.gauss(0, noise) for v in a)
        w = tuple(amp * v + rng.gauss(0, noise * 20) for v in w)
        out.append((t0 + i / FS, None, w, a))
    return out


def bump(u, c, s=0.08):
    return math.exp(-((u - c) / s) ** 2)


WORDS = {
    "oui": lambda u: ((0.4 * bump(u, 0.3), 0, 0), (0, 0, 8 * bump(u, 0.6))),
    "non": lambda u: ((0, 0.4 * bump(u, 0.25) - 0.4 * bump(u, 0.7), 0), (0, 0, 0)),
    "stop": lambda u: ((0, 0, 0.3 * math.sin(2 * math.pi * 2 * u)), (6 * bump(u, 0.5, 0.2), 0, 0)),
}


@unittest.skipIf(ss is None, "numpy requis")
class TestSilentSpeech(unittest.TestCase):
    def build(self, n_takes=6, seed=3, labels=None):
        rng = random.Random(seed)
        rec = ss.Recognizer()
        for w, pat in WORDS.items():
            for _ in range(n_takes):
                X, fs = ss.features(take(pat, rng))
                rec.add(w, ss.preprocess(X, fs))
        return rec, rng

    def test_dtw_identity_and_warp(self):
        a = np.sin(np.linspace(0, 6, 40))[:, None]
        b = np.sin(np.linspace(0, 6, 52))[:, None]
        self.assertEqual(ss.dtw(a, a), 0.0)
        self.assertLess(ss.dtw(a, b), 0.05)
        self.assertGreater(ss.dtw(a, -a), 0.5)

    def test_preprocess_trims_and_scales(self):
        rng = random.Random(1)
        X, fs = ss.features(take(WORDS["oui"], rng))
        P = ss.preprocess(X, fs)
        self.assertEqual(P.shape[1], 9)
        self.assertLess(len(P), len(X) / 4 * 0.9)              # bords inactifs retirés (+ 50 Hz)
        self.assertTrue(np.allclose(P.mean(axis=0), 0, atol=1e-9))

    def test_loo_and_permutation_on_real_signal(self):
        rec, _ = self.build()
        ev = rec.evaluate(n_perm=200)
        self.assertGreaterEqual(ev["loo_accuracy"], 0.9, ev)
        self.assertLess(ev["p_value"], 0.01)
        self.assertEqual(ev["verdict"], i18n.tr("speech.verdict.likely_real"))

    def test_permutation_detects_no_signal(self):
        rng = random.Random(7)
        rec = ss.Recognizer()
        for i in range(18):                                     # bruit pur, étiquettes arbitraires
            X, fs = ss.features(take(lambda u: ((0, 0, 0), (0, 0, 0)), rng))
            rec.add(["oui", "non", "stop"][i % 3], ss.preprocess(X, fs))
        ev = rec.evaluate(n_perm=200)
        self.assertGreater(ev["p_value"], 0.01)

    def test_predict_and_reject(self):
        rec, rng = self.build()
        X, fs = ss.features(take(WORDS["non"], rng))
        w, d, mg, why = rec.predict(ss.preprocess(X, fs))
        self.assertEqual((w, why), ("non", "ok"))
        rec.max_dist = d * 0.5                                  # seuil strict -> rejet
        self.assertIsNone(rec.predict(ss.preprocess(X, fs))[0])

    def test_session_train_predict_save(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ss.json")
            s = ss.SpeechSession(path)
            rng = random.Random(5)
            for w in ("oui", "non"):
                for _ in range(4):
                    s.action({"action": "start", "word": w})
                    for t, q, wv, a in take(WORDS[w], rng):
                        s.on_sample(int(t * 1e9), t, None, wv, (0, 0, 1000), a, (0, 0, 0))
                    s.action({"action": "stop"})
            self.assertEqual(s.as_dict()["words"], {"oui": 4, "non": 4})
            s.action({"action": "start"})
            for t, q, wv, a in take(WORDS["oui"], rng):
                s.on_sample(int(t * 1e9), t, (0, 0, 0), wv, (0, 0, 1000), a, (0, 0, 0))
            self.assertEqual(s.action({"action": "stop"})["word"], "oui")
            s2 = ss.SpeechSession(path)
            self.assertEqual(s2.rec.words(), {"oui": 4, "non": 4})


if __name__ == "__main__":
    unittest.main()
