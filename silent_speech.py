# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Matteo Gaudioso (starman-tech) — https://github.com/starman-tech/mousepods
"""silent_speech.py — EXPÉRIMENTAL : reconnaissance de quelques mots articulés sans voix.

Hypothèse (non vérifiée sur les AirPods) : articuler un mot fait bouger la mâchoire et la peau du
conduit auditif ; l'IMU de l'écouteur en capte peut-être une trace reproductible. Ce module sert à le
VÉRIFIER honnêtement avant d'y croire :

  * caractéristiques par segment : accélération utilisateur (3), vitesse de rotation (3),
    variation d'attitude depuis le début du segment (3, degrés) ;
    sous-échantillonnage ~50 Hz, coupe des bords inactifs, centrage, mise à l'échelle (z) ;
  * reconnaissance : 1 plus proche voisin, distance DTW (bande de Sakoe–Chiba) ;
    rejet si la distance est trop grande ou si la marge avec le 2ᵉ mot est trop faible ;
  * évaluation : précision « leave-one-out » + test de permutation des étiquettes (500×) -> p-valeur.
    p > 0,05 = rien ne prouve que le signal contient le mot (le hasard fait aussi bien).

Entraînement depuis l'interface : maintenir le bouton, articuler le mot, relâcher (N prises par mot).
"""
import json
import math
import os
import random

import numpy as np

from i18n import tr, tr_lazy

FRAME_HZ = 50.0
# planchers d'écart-type par groupe (bruit capteur) : évite d'amplifier un canal immobile
STD_FLOOR = np.array([0.03] * 3 + [0.8] * 3 + [0.3] * 3)


def _qmul(a, b):
    return (a[0] * b[0] - a[1] * b[1] - a[2] * b[2] - a[3] * b[3],
            a[0] * b[1] + a[1] * b[0] + a[2] * b[3] - a[3] * b[2],
            a[0] * b[2] - a[1] * b[3] + a[2] * b[0] + a[3] * b[1],
            a[0] * b[3] + a[1] * b[2] - a[2] * b[1] + a[3] * b[0])


def rotvec_deg(q0, q):
    """Vecteur rotation (degrés) de q0 vers q."""
    if q0 is None or q is None:
        return (0.0, 0.0, 0.0)
    d = _qmul((q0[0], -q0[1], -q0[2], -q0[3]), q)
    if d[0] < 0:
        d = tuple(-c for c in d)
    s = math.sqrt(d[1] ** 2 + d[2] ** 2 + d[3] ** 2)
    if s < 1e-9:
        return (0.0, 0.0, 0.0)
    ang = 2 * math.degrees(math.atan2(s, d[0]))
    return (d[1] / s * ang, d[2] / s * ang, d[3] / s * ang)


def features(samples):
    """samples : [(t_s, q|None, w °/s, a m/s²)] -> matrice (T, 9) brute."""
    if not samples:
        return np.zeros((0, 9)), 0.0
    q0 = samples[0][1]
    X = np.array([tuple(a) + tuple(w) + rotvec_deg(q0, q) for _, q, w, a in samples], float)
    span = samples[-1][0] - samples[0][0]
    fs = (len(samples) - 1) / span if span > 0 else FRAME_HZ
    return X, fs


def preprocess(X, fs, frame_hz=FRAME_HZ, min_frames=8):
    """Sous-échantillonne, coupe les bords inactifs, centre et met à l'échelle chaque canal."""
    X = np.asarray(X, float)
    if len(X) == 0:
        return X
    k = max(int(round(fs / frame_hz)), 1)
    n = len(X) // k
    if n >= min_frames:
        X = X[:n * k].reshape(n, k, X.shape[1]).mean(axis=1)
    # activité : écart à la médiane (accélération + rotation), normalisé par le plancher de bruit
    med = np.median(X, axis=0)
    act = np.abs((X[:, :6] - med[:6]) / STD_FLOOR[:6]).max(axis=1)
    thr = max(0.25 * act.max(), 2.0)
    idx = np.where(act > thr)[0]
    if len(idx):
        a, b = max(idx[0] - 2, 0), min(idx[-1] + 3, len(X))
        if b - a >= min_frames:
            X = X[a:b]
    X = X - X.mean(axis=0)
    sd = np.maximum(X.std(axis=0), STD_FLOOR)
    return X / sd


def dtw(A, B, band=0.15):
    """Distance DTW (euclidienne image à image), bande de Sakoe–Chiba, normalisée par n + m."""
    n, m = len(A), len(B)
    if n == 0 or m == 0:
        return float("inf")
    w = max(int(band * max(n, m)), abs(n - m) + 1)
    C = np.sqrt(((A[:, None, :] - B[None, :, :]) ** 2).sum(axis=2))
    D = np.full((n + 1, m + 1), np.inf)
    D[0, 0] = 0.0
    for i in range(1, n + 1):
        lo, hi = max(1, i - w), min(m, i + w)
        prev, cur = D[i - 1], D[i]
        for j in range(lo, hi + 1):
            cur[j] = C[i - 1, j - 1] + min(prev[j], prev[j - 1], cur[j - 1])
    return float(D[n, m] / (n + m))


class Recognizer:
    def __init__(self, band=0.15, max_dist=1.2, min_margin=0.08):
        self.band, self.max_dist, self.min_margin = band, max_dist, min_margin
        self.templates = []          # [(mot, matrice prétraitée)]

    def add(self, word, X):
        self.templates.append((word, np.asarray(X, float)))

    def words(self):
        out = {}
        for w, _ in self.templates:
            out[w] = out.get(w, 0) + 1
        return out

    def forget(self, word):
        self.templates = [t for t in self.templates if t[0] != word]

    def predict(self, X):
        """-> (mot | None, distance, marge, raison)."""
        best = {}
        for w, T in self.templates:
            d = dtw(X, T, self.band)
            best[w] = min(best.get(w, float("inf")), d)
        if not best:
            return None, None, None, tr_lazy("speech.reason.no_model")
        ranked = sorted(best.items(), key=lambda kv: kv[1])
        w1, d1 = ranked[0]
        d2 = ranked[1][1] if len(ranked) > 1 else float("inf")
        margin = (d2 - d1) / d2 if d2 not in (0.0, float("inf")) else 1.0
        if d1 > self.max_dist:
            return None, d1, margin, tr_lazy("speech.reason.too_far")
        if margin < self.min_margin:
            return None, d1, margin, tr_lazy("speech.reason.ambiguous")
        return w1, d1, margin, "ok"

    def distance_matrix(self):
        n = len(self.templates)
        D = np.zeros((n, n))
        for i in range(n):
            for j in range(i + 1, n):
                D[i, j] = D[j, i] = dtw(self.templates[i][1], self.templates[j][1], self.band)
        return D

    def evaluate(self, n_perm=500, seed=0):
        """Précision leave-one-out (1-NN DTW) et p-valeur du test de permutation des étiquettes."""
        labels = [w for w, _ in self.templates]
        n = len(labels)
        if n < 4 or len(set(labels)) < 2:
            return {"n": n, "error": tr("speech.error.too_few_takes")}
        D = self.distance_matrix()
        np.fill_diagonal(D, np.inf)
        nn = D.argmin(axis=1)

        def acc(lab):
            return float(np.mean([lab[i] == lab[nn[i]] for i in range(n)]))

        a = acc(labels)
        rng = random.Random(seed)
        perm = list(labels)
        ge = 0
        for _ in range(n_perm):
            rng.shuffle(perm)
            ge += acc(perm) >= a
        p = (ge + 1) / (n_perm + 1)
        conf = {}
        for i in range(n):
            conf.setdefault(labels[i], {}).setdefault(labels[nn[i]], 0)
            conf[labels[i]][labels[nn[i]]] += 1
        counts = {w: labels.count(w) for w in set(labels)}
        chance = sum((c / n) * ((c - 1) / (n - 1)) for c in counts.values())
        return {"n": n, "words": counts, "loo_accuracy": round(a, 3), "chance": round(chance, 3),
                "p_value": round(p, 4), "n_perm": n_perm, "confusion": conf,
                "verdict": tr_lazy("speech.verdict.likely_real" if p < 0.01 else
                              "speech.verdict.weak" if p < 0.05 else "speech.verdict.chance")}

    # ---- persistance
    def save(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"band": self.band, "templates": [[w, X.round(4).tolist()] for w, X in self.templates]}, f)

    def load(self, path):
        if os.path.exists(path):
            with open(path) as f:
                d = json.load(f)
            self.band = d.get("band", self.band)
            self.templates = [(w, np.asarray(X, float)) for w, X in d.get("templates", [])]


class SpeechSession:
    """Enregistrement « bouton maintenu » depuis l'interface, branché sur Controller.gesture_hooks."""

    def __init__(self, path):
        self.path = path
        self.rec = Recognizer()
        try:
            self.rec.load(path)
        except (OSError, ValueError):
            pass
        self.buf = None
        self.word = None
        self.last = None
        self.evaluation = None

    def on_sample(self, ts_ns, now, att, w, g, a, rates):
        if self.buf is None:
            return
        q = None
        if att is not None and len(att) == 3:
            x, y, z = (c / 32768.0 for c in att)
            n2 = x * x + y * y + z * z
            q = (math.sqrt(max(0.0, 1 - n2)), x, y, z) if 0.01 < n2 < 1.01 else None
        self.buf.append((ts_ns / 1e9, q, tuple(w), tuple(a)))
        if len(self.buf) > 2000:            # 10 s maximum
            self.buf = self.buf[-2000:]

    def action(self, body):
        act = body.get("action")
        if act == "start":
            self.buf, self.word = [], (str(body.get("word", "")).strip() or None)
        elif act == "stop" and self.buf is not None:
            samples, self.buf = self.buf, None
            X, fs = features(samples)
            if len(X) < 20:
                self.last = {"error": tr("speech.error.too_short")}
                return self.last
            P = preprocess(X, fs)
            if self.word:
                self.rec.add(self.word, P)
                self.rec.save(self.path)
                self.last = {"trained": self.word, "frames": len(P)}
            else:
                w, d, mg, why = self.rec.predict(P)
                self.last = {"word": w, "dist": None if d is None else round(d, 3),
                             "margin": None if mg is None else round(mg, 3), "reason": why}
        elif act == "evaluate":
            self.evaluation = self.rec.evaluate(int(body.get("n_perm", 500)))
        elif act == "forget":
            self.rec.forget(str(body.get("word", "")))
            self.rec.save(self.path)
        elif act == "clear":
            self.rec.templates = []
            self.rec.save(self.path)
            self.evaluation = None
        return self.last

    def as_dict(self):
        return {"recording": self.buf is not None, "word": self.word, "words": self.rec.words(),
                "last": self.last, "evaluation": self.evaluation}
