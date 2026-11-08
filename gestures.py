#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Matteo Gaudioso (starman-tech) — https://github.com/starman-tech/mousepods
"""gestures.py — gestes de la tête et de la mâchoire à partir de l'IMU des AirPods.

Trois couches, indépendantes du Bluetooth (testables sur des enregistrements) :

  SensorBuffer     acquisition sur l'horodatage CAPTEUR (ns) : trous < 30 ms interpolés, trous plus
                   longs = nouveau segment (aucun geste ne peut chevaucher un trou). Statistiques :
                   fréquence réelle, trous, échantillons dupliqués, spectre (énergie > 25–50 Hz).
  GestureDetector  règles à seuils (pas d'apprentissage) -> événements {t, type, conf, peak, ...}
                     teeth_click / teeth_double   pic bref d'accélération passe-haut
                     nod / shake                  oscillation tangage / lacet (lobes alternés)
                     tilt_left / tilt_right       roulis maintenu
                     look_up / look_down          tangage maintenu 500 ms
                     jaw_clench                   EXPÉRIMENTAL (vibration musculaire faible et soutenue)
                     jaw_slide_left/right         EXPÉRIMENTAL, jamais émis (pas de signal plausible)
                   Rejet : marche (accélération verticale périodique 1,4–2,6 Hz), mastication
                   (impulsions répétées), grand mouvement de tête.
  GestureRouter    activation : un double claquement ouvre une fenêtre de 2 s ; hors fenêtre aucune
                   action n'est exécutée (les gestes restent visibles dans la liste d'événements).

GestureEngine assemble le tout pour le contrôleur : source préférée = IMU brute (rapports HID accel
dev 4 / gyro dev 5, ~201 Hz, horodatage CAPTEUR en µs, Controller.raw_imu_hooks), repli sur devmotion6
(Controller.gesture_hooks, horodatage capteur ns) quand l'IMU brute n'arrive plus depuis 0,3 s.
Enregistrement CSV étiqueté, calibration par l'utilisateur (10 répétitions -> seuils), notifications.
Seuils réglés sur captures/gestures_labeled.json et captures/cma_walk.json (tests/test_gestures_real.py).

Rejouer un enregistrement :
  python gestures.py --replay captures/mon_essai.csv        (CSV de ce module)
  python gestures.py --replay captures/airpods3_motion_capture.json --stats --spectrum
"""
import argparse
import csv
import json
import math
import os
import struct
import sys
import time
from collections import deque

import airpods_mouse_controller as m
from i18n import tr, tr_lazy

GRAVITY = 9.81
CSV_COLUMNS = ["t_sensor", "arrival", "qw", "qx", "qy", "qz", "gx", "gy", "gz", "ax", "ay", "az",
               "grx", "gry", "grz", "label", "yaw", "pitch", "roll", "src"]
GESTURES = ["teeth_click", "teeth_double", "nod", "shake", "tilt_left", "tilt_right", "look_up",
            "look_down", "jaw_clench", "jaw_slide_left", "jaw_slide_right"]
GESTURE_NAMES = {g: f"gest.name.{g}" for g in GESTURES}   # translation keys (tr)


def _gesture_name(g):
    return tr(GESTURE_NAMES[g]) if g in GESTURE_NAMES else g


# calibration : geste répété -> clé de seuil, événements acceptés
CALIB_KEYS = {"teeth_click": ("click_thresh", ("teeth_click", "teeth_double")),
              "nod": ("nod_deg", ("nod",)), "shake": ("shake_deg", ("shake",)),
              "tilt": ("tilt_deg", ("tilt_left", "tilt_right")),
              "look": ("look_deg", ("look_up", "look_down")),
              "jaw_clench": ("clench_rms", ("jaw_clench",))}


def gesture_cfg(over=None):
    """Section `gestures` complète : valeurs par défaut + surcharge (mapping fusionné clé par clé)."""
    c = json.loads(json.dumps(m.DEFAULTS["gestures"]))
    for k, v in (over or {}).items():
        if k == "mapping" and isinstance(v, dict):
            c["mapping"].update(v)
        else:
            c[k] = v
    return c


def vlen(v):
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def lerp(a, b, f):
    return tuple(x + (y - x) * f for x, y in zip(a, b))


# =========================================================================== acquisition
class Sample:
    __slots__ = ("t", "arrival", "q", "w", "a", "g", "r", "interp", "seg", "label")

    def __init__(self, t, arrival, q, w, a, g, r=None, interp=False, seg=0, label=""):
        self.t, self.arrival, self.q, self.w, self.a, self.g = t, arrival, q, w, a, g
        self.r, self.interp, self.seg, self.label = r, interp, seg, label


def _qlerp(q0, q1, f):
    if q0 is None or q1 is None:
        return q1 if f >= 0.5 else q0
    if sum(a * b for a, b in zip(q0, q1)) < 0:
        q1 = tuple(-c for c in q1)
    q = lerp(q0, q1, f)
    n = math.sqrt(sum(c * c for c in q)) or 1.0
    return tuple(c / n for c in q)


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


class SensorBuffer:
    """Tampon circulaire indexé sur l'horodatage capteur (pas sur l'heure d'arrivée Bluetooth)."""

    def __init__(self, max_gap_s=0.03, keep_s=8.0, max_periods=2.5):
        self.max_gap = max_gap_s
        self.max_periods = max_periods      # flux lent (50 Hz) : un échantillon perdu reste interpolable
        self.keep = keep_s
        self.buf = deque()
        self.seg = 0
        self.seg_t0 = None
        self.last = None
        self.dts = deque(maxlen=400)
        self.dup_flags = deque(maxlen=1000)
        self.real_t = deque(maxlen=2000)
        self.gaps = deque(maxlen=50)        # (t, durée s) des trous > max_gap
        self.n_real = self.n_interp = self.n_dup = self.n_dropped = self.n_gaps = 0
        self.n_short_gaps = 0

    def period(self):
        return _median(self.dts) if len(self.dts) >= 5 else 0.005

    def _new_segment(self, t, gap):
        self.seg += 1
        self.seg_t0 = t
        if gap is not None:
            self.n_gaps += 1
            self.gaps.append((t, gap))

    def push(self, ts_ns, arrival, q, w, a, g, r=None, label=""):
        """Ajoute un échantillon réel ; renvoie la liste des échantillons produits (interpolés + réel)."""
        t = ts_ns / 1e9
        out = []
        last = self.last
        if last is None:
            self.seg_t0 = t
        else:
            dt = t - last.t
            if dt <= 0:
                if dt > -1.0:               # horodatage répété ou en désordre : ignoré
                    self.n_dropped += 1
                    return out
                self._new_segment(t, None)  # horloge capteur réinitialisée
            else:
                dup = w == last.w and a == last.a and g == last.g
                self.dup_flags.append(dup)
                self.n_dup += dup
                if dt > max(self.max_gap, self.max_periods * self.period()):
                    self._new_segment(t, dt)
                else:
                    p = self.period()
                    self.dts.append(dt)     # médiane : robuste aux trous courts occasionnels
                    if dt > 1.5 * p:        # trou court : interpolation linéaire au pas nominal
                        self.n_short_gaps += 1
                        k = max(int(round(dt / p)) - 1, 1)
                        for i in range(1, k + 1):
                            f = i / (k + 1)
                            rr = lerp(last.r, r, f) if (last.r is not None and r is not None) else r
                            out.append(Sample(last.t + dt * f, None, _qlerp(last.q, q, f), lerp(last.w, w, f),
                                              lerp(last.a, a, f), lerp(last.g, g, f), rr, True, self.seg, label))
                        self.n_interp += k
        s = Sample(t, arrival, q, tuple(w), tuple(a), tuple(g), None if r is None else tuple(r), False,
                   self.seg, label)
        out.append(s)
        self.last = s
        self.n_real += 1
        self.real_t.append(t)
        self.buf.extend(out)
        while self.buf and self.buf[0].t < t - self.keep:
            self.buf.popleft()
        return out

    def window(self, seconds, same_segment=True):
        """Échantillons des `seconds` dernières secondes (du segment courant par défaut)."""
        if not self.buf:
            return []
        t_end = self.buf[-1].t
        out = []
        for s in reversed(self.buf):
            if s.t < t_end - seconds or (same_segment and s.seg != self.seg):
                break
            out.append(s)
        out.reverse()
        return out

    def stats(self):
        rt = [x for x in self.real_t if self.real_t and x > self.real_t[-1] - 2.0]
        span = rt[-1] - rt[0] if len(rt) > 1 else 0.0
        p = self.period()
        return {
            "rate_hz": round((len(rt) - 1) / span, 1) if span > 0 else 0.0,
            "nominal_hz": round(1.0 / p, 1) if p > 0 else 0.0,
            "samples": self.n_real, "interpolated": self.n_interp,
            "interp_ratio": round(self.n_interp / max(self.n_real + self.n_interp, 1), 4),
            "short_gaps": self.n_short_gaps, "gaps": self.n_gaps,
            "gaps_ms": [round(d * 1000) for _, d in list(self.gaps)[-10:]],
            "max_gap_ms": round(max((d for _, d in self.gaps), default=0.0) * 1000),
            "dup_ratio": round(sum(self.dup_flags) / max(len(self.dup_flags), 1), 4),
            "dropped": self.n_dropped, "segment": self.seg,
            "segment_s": round(self.last.t - self.seg_t0, 2) if self.last and self.seg_t0 is not None else 0.0,
            "upsampled_suspect": len(self.dup_flags) > 50 and sum(self.dup_flags) / len(self.dup_flags) > 0.25,
        }

    def spectrum(self, kind="acc", seconds=1.28):
        """Spectre de puissance (somme des 3 axes, fenêtre de Hann) -> (fréquences Hz, puissances)."""
        win = self.window(seconds)
        if len(win) < 16:
            return [], []
        span = win[-1].t - win[0].t
        fs = (len(win) - 1) / span if span > 0 else 0.0
        if fs <= 0:
            return [], []
        attr = {"acc": "a", "gyro": "w"}[kind]
        chans = [[getattr(s, attr)[k] for s in win] for k in range(3)]
        return power_spectrum(chans, fs)

    def energy_above(self, fc, kind="acc", seconds=1.28):
        """Part de l'énergie (hors continu) au-dessus de fc Hz. Utile pour voir si 200 Hz est réel."""
        f, p = self.spectrum(kind, seconds)
        tot = sum(p[1:])
        return sum(pp for ff, pp in zip(f, p) if ff >= fc) / tot if tot > 0 else 0.0


def power_spectrum(chans, fs):
    """Puissance |FFT|² sommée sur les canaux, après retrait de la moyenne et fenêtre de Hann."""
    n = len(chans[0])
    hann = [0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1)) for i in range(n)]
    try:
        import numpy as np
        tot = None
        for c in chans:
            x = np.asarray(c, float)
            x = (x - x.mean()) * np.asarray(hann)
            pw = np.abs(np.fft.rfft(x)) ** 2
            tot = pw if tot is None else tot + pw
        return list(np.fft.rfftfreq(n, 1.0 / fs)), list(tot)
    except ImportError:                     # repli sans numpy (DFT naïve, fenêtres courtes)
        nf = n // 2 + 1
        tot = [0.0] * nf
        for c in chans:
            mu = sum(c) / n
            x = [(v - mu) * h for v, h in zip(c, hann)]
            for k in range(nf):
                re = sum(x[i] * math.cos(2 * math.pi * k * i / n) for i in range(n))
                im = sum(x[i] * math.sin(2 * math.pi * k * i / n) for i in range(n))
                tot[k] += re * re + im * im
        return [k * fs / n for k in range(nf)], tot


def band_fraction(x, fs, lo, hi, fmin=0.5, fmax=8.0, step=0.25):
    """Part de la puissance de x dans [lo, hi] Hz parmi [fmin, fmax] (DFT sur une grille de fréquences)."""
    n = len(x)
    if n < 8:
        return 0.0, 0.0
    mu = sum(x) / n
    y = [v - mu for v in x]
    rms = math.sqrt(sum(v * v for v in y) / n)
    band = tot = 0.0
    f = fmin
    while f <= fmax + 1e-9:
        w = 2 * math.pi * f / fs
        re = sum(v * math.cos(w * i) for i, v in enumerate(y))
        im = sum(v * math.sin(w * i) for i, v in enumerate(y))
        p = re * re + im * im
        tot += p
        if lo <= f <= hi:
            band += p
        f += step
    return (band / tot if tot > 0 else 0.0), rms


# =========================================================================== IMU brute (HID accel / gyro)
RAW_SCALE = 1e-5          # accéléromètre : 1e-5 g ; gyroscope : 1e-5 °/s
RAW_DEVS = {4: "a", 5: "w"}   # identifiants RemoteHID habituels (AirPods 3) : 4 = accel, 5 = gyro


def parse_raw_imu(pl):
    """Rapport d'entrée HID accel/gyro (29 o) -> (horodatage capteur ns, (x, y, z)).

    Disposition observée : [0] 00, [1] id de rapport, int32 X/Y/Z en [2:6] [6:10] [10:14] (×1e-5),
    [14:16] inconnu (≈ température ?), [19:21] compteur, [21:29] horodatage capteur en µs.
    Accéléro et gyro d'un même instant portent exactement le même horodatage."""
    if len(pl) < 29:
        return None
    x, y, z = struct.unpack_from("<iii", pl, 2)
    ts_us = int.from_bytes(pl[21:29], "little")
    return ts_us * 1000, (x * RAW_SCALE, y * RAW_SCALE, z * RAW_SCALE)


class RawImuFuser:
    """Apparie accéléro (avec gravité) et gyro par horodatage capteur, estime la gravité (passe-bas)
    et produit des lignes {ts_ns, arrival, q, w °/s, a m/s² sans gravité, g ~1000/g}."""

    def __init__(self, grav_tau=1.0, stale_ns=20_000_000):
        self.pend = {}
        self.g = None
        self.last_w = (0.0, 0.0, 0.0)
        self.last_ts = None
        self.tau = grav_tau
        self.stale = stale_ns
        self.n_accel_only = 0

    def push(self, kind, pl, arrival):
        p = parse_raw_imu(pl)
        if p is None or kind not in ("a", "w"):
            return []
        ts, v = p
        self.pend.setdefault(ts, {"arrival": arrival})[kind] = v
        newest = max(self.pend)
        out = []
        for t in sorted(self.pend):
            e = self.pend[t]
            if ("a" in e and "w" in e) or newest - t > self.stale:
                del self.pend[t]
                if "a" not in e:            # gyro seul (accéléro absent) : pas de ligne
                    self.last_w = e["w"]
                    continue
                if "w" in e:
                    self.last_w = e["w"]
                else:
                    self.n_accel_only += 1
                out.append(self._emit(t, e["arrival"], e["a"], self.last_w))
            else:
                break
        return out

    def _emit(self, ts, arrival, a_g, w):
        dt = (ts - self.last_ts) / 1e9 if self.last_ts else 0.0
        self.last_ts = ts
        if self.g is None or dt <= 0 or dt > 1.0:
            self.g = a_g
        else:
            # filtre complémentaire : la gravité (repère corps) tourne avec le gyroscope (dv/dt = -ω×v),
            # puis est recalée lentement sur l'accéléromètre -> pas de fausse accélération en tournant la tête
            wr = tuple(math.radians(x) for x in w)
            g = self.g
            cx = (wr[1] * g[2] - wr[2] * g[1], wr[2] * g[0] - wr[0] * g[2], wr[0] * g[1] - wr[1] * g[0])
            gp = tuple(gi - ci * dt for gi, ci in zip(g, cx))
            al = dt / (self.tau + dt)
            self.g = tuple(gi + al * (a - gi) for gi, a in zip(gp, a_g))
        user = tuple((a - g) * GRAVITY for a, g in zip(a_g, self.g))
        return {"ts_ns": ts, "arrival": arrival, "q": None, "w": tuple(w), "a": user,
                "g": tuple(g * 1000.0 for g in self.g), "r": None, "label": ""}


# =========================================================================== détection
class GestureDetector:
    """Règles à seuils. `feed(sample)` -> liste d'événements. `sample.r` = (lacet, tangage, roulis) °/s
    calibrés (gauche = lacet négatif, haut = tangage positif, droite = roulis positif)."""

    def __init__(self, cfg=None):
        self.c = gesture_cfg(cfg)
        self.reset()
        self.n_rejected = {"walk": 0, "chew": 0, "motion": 0}

    def reset(self, seg=None, context=True):
        """Remise à zéro de l'état des gestes : appelé à chaque nouveau segment (trou capteur).
        Le contexte de rejet (marche, mastication, grand mouvement) survit aux trous ; il n'est
        effacé que si context=True (création, changement de source)."""
        self.seg = seg
        self.prev_t = None
        self.lp = None                  # passe-bas de l'accélération (pour le passe-haut)
        self.hp_mag = 0.0
        self.spike = None               # pic en cours {t0, peak, t, vec, hi}
        self.seq = []                   # claquements candidats [(t, peak, vec)]
        self.rhist = deque()            # (t, |vitesse de rotation|) sur 1,5 s
        self.last_click_t = -1e9
        self.last_click_peak = 0.0
        self.last_impulse_t = -1e9
        self.last_spike = None          # dernière impulsion brève (même sous le seuil) -> page Labo
        self.noise_buf = deque()        # (t, |acc passe-haut|) tête quasi immobile -> bruit de fond
        self.noise_next = -1e9
        self.last_spike_end = -1e9
        if context or not hasattr(self, "noise"):
            self.noise = None
        if context or not hasattr(self, "impulses"):
            self.impulses = deque()     # instants des impulsions (tous niveaux) -> mastication
            self.chew_until = self.walk_until = self.big_until = -1e9
            self.walk_buf = deque()
            self.walk_next = -1e9
            self.walk_dec = 0.0
        self.lobes = [[], [], []]       # par axe : (t0, t1, signe, angle, pic)
        self.last_lobe = [(-1e9, 0.0)] * 3   # dernier aller rapide par axe (fin, angle)
        self.run = [None, None, None]   # lobe en cours par axe
        self.energy = deque()           # (t, e_lacet, e_tangage, e_roulis)
        self.ang = [0.0, 0.0, 0.0]
        self.base = [0.0, 0.0, 0.0]
        self.hold = {}                  # geste maintenu -> t début
        self.latched = set()
        self.last_event_t = -1e9
        self.clench_t0 = None
        self.clench_win = deque()
        self.clench_latched = False

    # ---- états de rejet (exposés à l'interface)
    def flags(self, t=None):
        t = self.prev_t if t is None else t
        if t is None:
            return {"walking": False, "chewing": False, "big_motion": False}
        return {"walking": t < self.walk_until, "chewing": t < self.chew_until, "big_motion": t < self.big_until}

    def _ev(self, t, typ, conf, peak, **kw):
        e = {"t": round(t, 4), "type": typ, "conf": round(max(0.0, min(1.0, conf)), 2), "peak": round(peak, 3)}
        e.update(kw)
        return e

    def feed(self, s):
        c = self.c
        if s.seg != self.seg:
            self.reset(s.seg, context=False)
        t = s.t
        dt = t - self.prev_t if self.prev_t is not None else 0.0
        self.prev_t = t
        out = []
        r = s.r or (0.0, 0.0, 0.0)
        rmag = vlen(r)
        # --- passe-haut de l'accélération utilisateur
        if self.lp is None:
            self.lp = s.a
        else:
            tau = 1.0 / (2 * math.pi * c["click_hp_hz"])
            al = dt / (tau + dt) if dt > 0 else 1.0
            self.lp = tuple(lv + al * (av - lv) for lv, av in zip(self.lp, s.a))
        hp = tuple(av - lv for av, lv in zip(s.a, self.lp))
        hmag = vlen(hp)
        self.hp_mag = hmag
        # --- rejets
        self._walk(s, t, dt)
        if rmag > c["big_motion_dps"] or max(abs(x - b) for x, b in zip(self.ang, self.base)) > c["big_motion_deg"]:
            if t >= self.big_until:
                self.n_rejected["motion"] += 1
            self.big_until = t + c["big_motion_hold_s"]
        suppressed = t < self.walk_until or t < self.big_until
        # --- claquements
        out += self._clicks(t, hp, hmag, rmag, suppressed)
        # --- mâchoire serrée (expérimental)
        if c.get("clench_enabled"):
            out += self._clench(t, hmag, rmag, suppressed)
        # --- tête
        if dt > 0:
            out += self._head(t, dt, r, suppressed)
        return out

    # ---- marche : accélération verticale périodique 1,4–2,6 Hz
    def _walk(self, s, t, dt):
        c = self.c
        gl = vlen(s.g)
        av = sum(a * g for a, g in zip(s.a, s.g)) / gl if gl > 1e-6 else vlen(s.a)
        self.walk_dec += dt
        if self.walk_dec >= 0.04 or not self.walk_buf:      # ~25 Hz suffisent
            self.walk_dec = 0.0
            self.walk_buf.append((t, av))
        while self.walk_buf and self.walk_buf[0][0] < t - c["walk_window_s"]:
            self.walk_buf.popleft()
        if t >= self.walk_next and len(self.walk_buf) > 40:
            self.walk_next = t + 0.25
            span = self.walk_buf[-1][0] - self.walk_buf[0][0]
            if span > 0.8 * c["walk_window_s"]:
                fs = (len(self.walk_buf) - 1) / span
                frac, rms = band_fraction([v for _, v in self.walk_buf], fs, 1.4, 2.6)
                if rms > c["walk_rms"] and frac > c["walk_ratio"]:
                    if t >= self.walk_until:
                        self.n_rejected["walk"] += 1
                    self.walk_until = t + c["walk_hold_s"]

    # ---- claquement des dents
    def _gyro_mean(self, tc):
        """Vitesse de rotation moyenne (°/s) sur [tc - w, tc + w] : un claquement secoue brièvement le
        gyroscope (pic jusqu'à ~80 °/s) mais sa moyenne reste basse (< 16 °/s mesuré), contrairement
        à un geste de tête (> 30 °/s)."""
        w = self.c["click_gyro_win_s"]
        v = [x for tt, x in self.rhist if tc - w <= tt <= tc + w]
        return sum(v) / len(v) if v else 0.0

    def click_threshold(self, t=None):
        """Seuil effectif du claquement (m/s²) = max(click_thresh, k × bruit). Le bruit est le
        centile `click_noise_pct` de |acc passe-haut| sur `click_noise_win_s` (tête quasi immobile) :
        il monte quand on parle ou mâche, ce qui relève le seuil ; au calme, click_thresh s'applique
        (on peut donc le baisser pour des claquements doux sans exploser les faux positifs)."""
        c = self.c
        thr = c["click_thresh"]
        if c.get("click_adaptive", True) and self.noise is not None:
            thr = max(thr, c["click_noise_k"] * self.noise)
        return thr

    def _noise_update(self, t, hmag, rmag):
        c = self.c
        # bruit mesuré hors pic en cours et hors des click_echo_s qui suivent un claquement accepté
        # (le « ring » d'un claquement relèverait sinon le seuil du 2e claquement d'un double)
        if (rmag < c["click_noise_still_dps"] and self.spike is None
                and t - self.last_click_t > c.get("click_echo_s", 0.45)):
            self.noise_buf.append((t, hmag))
        win = c["click_noise_win_s"]
        while self.noise_buf and self.noise_buf[0][0] < t - win:
            self.noise_buf.popleft()
        if t >= self.noise_next:
            self.noise_next = t + 0.1
            n = len(self.noise_buf)
            if n >= 40 and self.noise_buf[-1][0] - self.noise_buf[0][0] > 0.5 * win:
                v = sorted(x for _, x in self.noise_buf)
                self.noise = v[min(int(c["click_noise_pct"] * n), n - 1)]

    def _clicks(self, t, hp, hmag, rmag, suppressed):
        c = self.c
        out = []
        self._noise_update(t, hmag, rmag)
        thr = self.click_threshold()
        lo = thr * c["click_low_frac"]
        # impulsion « mastication » : niveau absolu, indépendant du seuil de claquement (sinon baisser
        # click_thresh fait compter les rebonds d'ouverture de la mâchoire comme de la mastication)
        imp_min = c.get("chew_impulse_min", 0.3)
        lo = min(lo, imp_min)
        self.rhist.append((t, rmag))
        while self.rhist and self.rhist[0][0] < t - 1.5:
            self.rhist.popleft()
        sp = self.spike
        if sp is None:
            # période réfractaire comptée depuis le dernier CLAQUEMENT (une petite impulsion ne doit pas
            # masquer le 2e claquement d'un double qui la suit de près)
            if hmag > lo and t - self.last_click_t >= c["click_refractory_ms"] / 1000:
                self.spike = {"t0": t, "peak": hmag, "t": t, "vec": hp, "thr": thr, "gmax": rmag}
        else:
            if hmag > sp["peak"]:
                sp.update(peak=hmag, t=t, vec=hp)
            sp["gmax"] = max(sp["gmax"], rmag)
            dur = t - sp["t0"]
            # fin du pic relative à son amplitude : avec un seuil bas, le « ring » d'un claquement fort
            # reste longtemps au-dessus de lo/2 et la durée dépasserait click_max_ms (claquement perdu)
            if hmag < max(lo * 0.5, c.get("click_end_frac", 0.25) * sp["peak"]) or dur > 3 * c["click_max_ms"] / 1000:
                self.spike = None
                self.last_spike_end = t
                thr = sp["thr"]                 # seuil d'avant le pic (le pic ne relève pas son propre seuil)
                # écho : petite impulsion juste après un claquement (relâchement de la mâchoire) -> ignorée
                echo = (sp["t"] - self.last_click_t <= c.get("click_echo_s", 0.45)
                        and sp["peak"] < c.get("click_echo_frac", 0.3) * self.last_click_peak)
                if dur > c["click_max_ms"] / 1000 and sp["peak"] >= imp_min:
                    # secousse forte mais longue (pas, impact, trot) : annule la séquence en cours
                    self.seq = []
                    self.last_impulse_t = sp["t"]
                elif dur <= c["click_max_ms"] / 1000 and not echo and sp["peak"] >= min(thr, imp_min):
                    self.last_spike = {"t": sp["t"], "peak": sp["peak"], "dur": dur, "vec": sp["vec"],
                                       "gmax": sp["gmax"], "thr": thr}
                    # mastication : impulsions rapprochées (< chew_merge_s, ex. un double claquement)
                    # comptées pour une seule ; seules les impulsions >= chew_impulse_min comptent
                    if sp["peak"] >= imp_min:
                        if not self.impulses or sp["t"] - self.last_impulse_t > c["chew_merge_s"]:
                            self.impulses.append(sp["t"])
                    self.last_impulse_t = sp["t"]
                    while self.impulses and self.impulses[0] < t - c["chew_window_s"]:
                        self.impulses.popleft()
                    if len(self.impulses) >= c["chew_count"]:
                        if t >= self.chew_until:
                            self.n_rejected["chew"] += 1
                        self.chew_until = t + c["chew_hold_s"]
                        self.seq = []
                    elif sp["peak"] >= thr and not suppressed and t >= self.chew_until:
                        self.seq.append((sp["t"], sp["peak"], sp["vec"], dur, sp["gmax"], thr))
                        self.last_click_t = sp["t"]
                        self.last_click_peak = sp["peak"]
        # fin de séquence : silence assez long -> simple / double (rotation de tête vérifiée autour)
        if self.seq and t - self.last_impulse_t > c["click_quiet_s"]:
            seq, self.seq = self.seq, []
            if t < self.chew_until or suppressed:
                return out
            if any(self._gyro_mean(x[0]) > c["click_max_gyro_dps"] for x in seq):
                self.n_rejected["motion"] += 1
                return out
            if len(seq) == 1:
                t1, pk, vec, dur, gmax, th = seq[0]
                out.append(self._ev(t1, "teeth_click", pk / (2 * th), pk, **self._side(vec),
                                    **self._click_info(t1, vec, dur, gmax, th)))
            elif len(seq) == 2:
                (t1, p1, v1, d1, g1, th1), (t2, p2, v2, d2, g2, th2) = seq
                if c["double_min_s"] <= t2 - t1 <= c["double_max_s"]:
                    th = max(th1, th2)
                    out.append(self._ev(t2, "teeth_double", min(p1, p2) / (2 * th), min(p1, p2),
                                        interval=round(t2 - t1, 3), **self._side(v1),
                                        **self._click_info(t1, v1, max(d1, d2), max(g1, g2), th)))
        return out

    def _click_info(self, tc, vec, dur, gmax, thr):
        """Caractéristiques d'un claquement (page Labo) : vecteur du pic, durée, gyroscope, seuil."""
        return {"vec": [round(v, 3) for v in vec], "dur_ms": round(dur * 1000, 1),
                "gyro_mean": round(self._gyro_mean(tc), 1), "gyro_max": round(gmax, 1),
                "thr": round(thr, 3)}
        return out

    def _side(self, vec):
        """Axe dominant du pic (brut) + côté si `click_side_axis` a été réglé par l'utilisateur."""
        k = max(range(3), key=lambda i: abs(vec[i]))
        axis = ("+" if vec[k] >= 0 else "-") + "xyz"[k]
        spec = (self.c.get("click_side_axis") or "").strip().lower()
        side = None
        if spec and spec[-1] in "xyz":
            j = "xyz".index(spec[-1])
            sgn = -1 if spec.startswith("-") else 1
            if abs(vec[j]) > 0.5 * abs(vec[k]):
                side = "gauche" if vec[j] * sgn > 0 else "droite"
        return {"axis": axis, "side": side}

    # ---- mâchoire serrée (expérimental) : vibration faible, soutenue, tête immobile
    def _clench(self, t, hmag, rmag, suppressed):
        c = self.c
        self.clench_win.append((t, hmag * hmag))
        while self.clench_win and self.clench_win[0][0] < t - 0.1:
            self.clench_win.popleft()
        rms = math.sqrt(sum(v for _, v in self.clench_win) / len(self.clench_win))
        ok = (c["clench_rms"] <= rms < c["click_thresh"] * c["click_low_frac"] and rmag < 20.0
              and not suppressed and t >= self.chew_until)
        if not ok:
            self.clench_t0 = None
            if rms < c["clench_rms"] * 0.7:
                self.clench_latched = False
            return []
        if self.clench_t0 is None:
            self.clench_t0 = t
        if not self.clench_latched and t - self.clench_t0 >= c["clench_min_s"]:
            self.clench_latched = True
            return [self._ev(t, "jaw_clench", rms / (2 * c["clench_rms"]), rms, experimental=True)]
        return []

    @staticmethod
    def jaw_slide(*_):
        """EXPÉRIMENTAL — non implémenté : aucun signal plausible identifié (glissement latéral de la
        mâchoire quasi invisible pour une IMU dans le conduit auditif). Ne renvoie jamais rien."""
        return None

    # ---- tête : oscillations (oui / non) et positions maintenues
    def _head(self, t, dt, r, suppressed):
        c = self.c
        out = []
        self.energy.append((t, r[0] * r[0] * dt, r[1] * r[1] * dt, r[2] * r[2] * dt))
        while self.energy and self.energy[0][0] < t - 2.0:
            self.energy.popleft()
        for k in range(3):
            self.ang[k] += r[k] * dt
        still = max(abs(x) for x in r) < 15.0
        for k, thr in ((1, c["look_deg"]), (2, c["tilt_deg"])):
            # position neutre : suit vite les petites dérives, lentement (base_tau_s) les grands écarts,
            # pour qu'une position maintenue reste visible ~1 s sans figer la référence indéfiniment
            if still:
                dev = abs(self.ang[k] - self.base[k])
                # écart dû à un grand mouvement sur un autre axe (intégration non commutative) : oublié vite
                cross = any(t - self.last_lobe[j][0] < 1.0 and self.last_lobe[j][1] > c["cross_axis_ratio"] * dev
                            for j in range(3) if j != k)
                tau = 0.3 if cross else (1.0 if dev < 0.5 * thr else c["base_tau_s"])
                self.base[k] += (self.ang[k] - self.base[k]) * min(dt / tau, 1.0)
        self.base[0] = self.ang[0]          # le lacet n'a pas de position maintenue
        # lobes des oscillations
        for k in (0, 1, 2):
            fin = self._lobe(k, t, dt, r[k])
            if fin and k < 2 and not suppressed:
                ev = self._oscillation(k, t)
                if ev:
                    out.append(ev)
        # positions maintenues
        rel_p = self.ang[1] - self.base[1]
        rel_r = self.ang[2] - self.base[2]
        holds = (("look_up", rel_p, c["look_deg"], c["look_hold_s"], abs(rel_p) > abs(rel_r), 1),
                 ("look_down", -rel_p, c["look_deg"], c["look_hold_s"], abs(rel_p) > abs(rel_r), 1),
                 ("tilt_right", rel_r, c["tilt_deg"], c["tilt_hold_s"], abs(rel_r) > abs(rel_p), 2),
                 ("tilt_left", -rel_r, c["tilt_deg"], c["tilt_hold_s"], abs(rel_r) > abs(rel_p), 2))
        # position maintenue refusée juste après un grand aller rapide sur un AUTRE axe : un « non »
        # fait aussi pencher (roulis couplé), une inclinaison fait dériver le tangage intégré, etc.
        rel = {1: abs(rel_p), 2: abs(rel_r)}
        coupled = {k: any(t - self.last_lobe[j][0] < 1.0 and self.last_lobe[j][1] > c["cross_axis_ratio"] * rel[k]
                          for j in range(3) if j != k) for k in (1, 2)}
        for name, v, thr, need, dominant, k in holds:
            if coupled[k]:
                dominant = False
            if name in self.latched:
                if v < 0.5 * thr:
                    self.latched.discard(name)
                continue
            if v >= thr and dominant and abs(r[k]) < c["hold_max_dps"] and not suppressed:
                t0 = self.hold.setdefault(name, t)
                if t - t0 >= need:
                    self.latched.add(name)
                    self.hold.pop(name, None)
                    out.append(self._ev(t, name, v / (2 * thr), v))
            elif v < thr or not dominant or suppressed:
                self.hold.pop(name, None)
        return out

    def _lobe(self, k, t, dt, rate):
        """Suit les lobes (signe constant) sur l'axe k ; renvoie True quand un lobe valable se termine."""
        c = self.c
        lo = 0.25 * c["lobe_dps"]
        run = self.run[k]
        sgn = 1 if rate > 0 else -1
        if run is not None and (abs(rate) < lo or sgn != run[2]):
            self.run[k] = None
            if run[4] >= c["lobe_dps"]:
                self.lobes[k].append((run[0], t, run[2], abs(run[3]), run[4]))
                self.last_lobe[k] = (t, abs(run[3]))
                self.lobes[k] = [lb for lb in self.lobes[k] if lb[1] > t - c["osc_max_s"] - 0.5][-6:]
                done = True
            else:
                done = False
            run = None
            if abs(rate) >= lo:
                self.run[k] = [t, t, sgn, rate * dt, abs(rate)]
            return done
        if run is None:
            if abs(rate) >= lo:
                self.run[k] = [t, t, sgn, rate * dt, abs(rate)]
            return False
        run[1] = t
        run[3] += rate * dt
        run[4] = max(run[4], abs(rate))
        return False

    def _oscillation(self, k, t):
        c = self.c
        name, need, min_deg = (("shake", c["shake_lobes"], c["shake_deg"]) if k == 0
                               else ("nod", c["nod_lobes"], c["nod_deg"]))
        lbs = self.lobes[k]
        if len(lbs) < need or t - self.last_event_t < c["refractory_s"]:
            return None
        seq = lbs[-need:]
        for a, b in zip(seq, seq[1:]):
            if a[2] == b[2] or b[0] - a[1] > c["lobe_gap_s"]:
                return None
        if any(lb[3] < min_deg for lb in seq) or seq[-1][1] - seq[0][0] > c["osc_max_s"]:
            return None
        t0 = seq[0][0]
        e = [sum(x[i + 1] for x in self.energy if x[0] >= t0) for i in range(3)]
        others = max(e[j] for j in range(3) if j != k)
        if e[k] < c["dominance"] * others:
            return None
        self.lobes = [[], [], []]
        self.last_event_t = t
        amp = min(lb[3] for lb in seq)
        conf = min(amp / (2 * min_deg), 1.0) * min(e[k] / (2 * c["dominance"] * max(others, 1e-9)), 1.0)
        first = "bas" if (k == 1 and seq[0][2] < 0) else ("haut" if k == 1 else ("gauche" if seq[0][2] < 0 else "droite"))
        return self._ev(t, name, conf, amp, lobes=need, start=first)


# =========================================================================== activation / actions
class GestureRouter:
    """Double claquement -> fenêtre d'action (window_s). Une seule action par fenêtre."""

    def __init__(self):
        self.armed_until = -1e9
        self.freeze_until = -1e9      # curseur gelé un instant après l'action (retour de la tête)

    def armed(self, t):
        return t < self.armed_until

    def route(self, ev, c):
        t, typ = ev["t"], ev["type"]
        action = c["mapping"].get(typ, "none") or "none"
        if c.get("require_activation", True):
            if typ == c.get("activation", "teeth_double"):
                self.armed_until = t + c["window_s"]
                ev["action"] = "activation"
                return None
            if not self.armed(t):
                ev["action"] = None
                ev["blocked"] = tr_lazy("gest.blocked.outside_window")
                return None
            if action == "none":
                ev["action"] = None
                return None
            self.armed_until = -1e9
        elif action == "none":
            ev["action"] = None
            return None
        ev["action"] = action
        self.freeze_until = t + float(c.get("freeze_after_s", 0.6))
        return action


# =========================================================================== moteur complet
class GestureEngine:
    """Branché sur Controller.gesture_hooks : hook(ts_ns, now, att, w, g, a, rates) (devmotion6)
    et Controller.raw_imu_hooks : hook(kind "a"|"w", payload HID, now) (IMU brute, source préférée).
    Tant que l'IMU brute arrive (< 0,3 s), devmotion6 est ignoré pour la détection."""

    @property
    def buf(self):
        return self.bufs[self.source]

    def __init__(self, cfg_get, command=None, notifier=None, save=None, profile_get=None):
        self.cfg_get = cfg_get
        self.command = command
        self.notifier = notifier
        self.save = save
        c = gesture_cfg(cfg_get())
        self.bufs = {"raw": SensorBuffer(c["max_interp_gap_s"]), "dm6": SensorBuffer(c["max_interp_gap_s"])}
        self.source = "dm6"             # "raw" = HID accel/gyro ~200 Hz (préféré) ; "dm6" = devmotion6
        self.raw_last = -1e9
        self.fuser = RawImuFuser()
        self.profile_get = profile_get
        self._prof = None
        self.raw_motion = m.MotionEngine(m.DEFAULTS["bias"])
        self.det = GestureDetector(c)
        self.router = GestureRouter()
        self.events = deque(maxlen=100)
        self.trace = deque(maxlen=900)     # (t, |acc passe-haut|, lacet, tangage, roulis) ~ 5 s
        self.rec = None
        self.calib = None
        self.n_events = 0
        self.error = None
        self._dec = 0
        self._src = self._src_copy = self._c = None
        self.sample_taps = []           # callbacks(sample, source) à pleine cadence (page Labo)

    def _cfg(self):
        """Section gestes complète, recalculée seulement si la configuration a changé (200 Hz)."""
        src = self.cfg_get()
        if src is not self._src or src != self._src_copy:
            self._src, self._src_copy = src, json.loads(json.dumps(src))
            self._c = gesture_cfg(src)
        return self._c

    # ---- enregistrement CSV étiqueté
    def start_record(self, path, label=""):
        self.stop_record()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        f = open(path, "w", newline="")
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        self.rec = {"path": path, "f": f, "w": w, "label": label, "rows": 0, "t0": time.monotonic()}
        return path

    def set_label(self, label):
        if self.rec:
            self.rec["label"] = label

    def stop_record(self):
        rec, self.rec = self.rec, None
        if rec:
            rec["f"].close()
            return {"path": rec["path"], "rows": rec["rows"]}
        return None

    # ---- calibration par l'utilisateur
    def start_calib(self, gesture, reps=None):
        c = gesture_cfg(self.cfg_get())
        if gesture not in CALIB_KEYS:
            raise ValueError(f"geste non calibrable : {gesture}")
        key, _ = CALIB_KEYS[gesture]
        self.calib = {"gesture": gesture, "key": key, "reps": int(reps or c["calib_reps"]), "peaks": [],
                      "old": c[key], "done": False, "result": None}
        self.det = GestureDetector(self._relaxed(c))

    def cancel_calib(self):
        self.calib = None
        self.det = GestureDetector(gesture_cfg(self.cfg_get()))

    def _relaxed(self, c):
        rc = dict(c)
        key = self.calib["key"]
        rc[key] = c[key] * 0.5                      # détection permissive pendant les répétitions
        rc["require_activation"] = False
        return rc

    def _calib_event(self, ev):
        cal = self.calib
        if cal["done"] or ev["type"] not in CALIB_KEYS[cal["gesture"]][1]:
            return
        cal["peaks"].append(ev["peak"])
        if len(cal["peaks"]) >= cal["reps"]:
            c = gesture_cfg(self.cfg_get())
            val = round(c["calib_frac"] * _median(cal["peaks"]), 3)
            cal["done"], cal["result"] = True, {cal["key"]: val}
            if self.save:
                self.save({cal["key"]: val})
            if self.notifier:
                self.notifier(tr("gest.calib_done", name=_gesture_name(cal["gesture"]), key=cal["key"], value=val))
            self.det = GestureDetector(gesture_cfg({**self.cfg_get(), cal["key"]: val}))

    # ---- flux
    def on_sample(self, ts_ns, now, att, w, g, a, rates):
        try:
            self._on_sample(ts_ns, now, att, w, g, a, rates)
        except Exception as e:  # noqa: BLE001  (les gestes ne doivent jamais arrêter la souris)
            if self.error is None:
                m.log.warning("Gestes : erreur %s", e)
            self.error = str(e)

    def _on_sample(self, ts_ns, now, att, w, g, a, rates):
        if now - self.raw_last < 0.3:
            return                          # IMU brute active : devmotion6 réservé à la souris
        q = m.quat_from_att(att) if att is not None and len(att) == 3 else att
        self._feed("dm6", ts_ns, now, q, w, a, g, rates)

    def on_raw(self, kind, pl, now):
        try:
            for row in self.fuser.push(kind, pl, now):
                self.raw_last = now
                p = self.profile_get() if self.profile_get else None
                if p is not None and p is not self._prof:
                    self._prof = p
                    self.raw_motion.set_profile(p)
                last = self.bufs["raw"].last
                dt = row["ts_ns"] / 1e9 - last.t if last else 0.005
                r = self.raw_motion.step(row["w"], row["g"], dt if 0 < dt < 0.15 else 0.005)
                self._feed("raw", row["ts_ns"], now, None, row["w"], row["a"], row["g"], r)
        except Exception as e:  # noqa: BLE001
            if self.error is None:
                m.log.warning("Gestes (IMU brute) : erreur %s", e)
            self.error = str(e)

    def _feed(self, src, ts_ns, now, q, w, a, g, rates):
        if src != self.source:              # changement de source : horloges différentes -> état remis à zéro
            self.source = src
            self.det.reset(None)
            self.trace.clear()
        if self.rec:
            lab = self.rec["label"]
            qq = q or (1.0, 0.0, 0.0, 0.0)
            self.rec["w"].writerow([f"{ts_ns / 1e9:.9f}", f"{now:.6f}", *[f"{v:.5f}" for v in qq],
                                    *[f"{v:.3f}" for v in w], *[f"{v:.4f}" for v in a],
                                    *[f"{v:.1f}" for v in g], lab, *[f"{v:.3f}" for v in rates], src])
            self.rec["rows"] += 1
        c = self._cfg()
        if not self.calib:
            self.det.c = c
        buf = self.bufs[src]
        buf.max_gap = c["max_interp_gap_s"]
        for s in buf.push(ts_ns, now, q, w, a, g, rates):
            self._dec += 1
            for tap in self.sample_taps:
                try:
                    tap(s, src)
                except Exception as e:  # noqa: BLE001  (le Labo ne doit jamais gêner les gestes)
                    self.error = f"labo : {e}"
            if not (c["enabled"] or self.calib):
                continue
            evs = self.det.feed(s)
            if self._dec % 3 == 0:
                self.trace.append((s.t, self.det.hp_mag, *(s.r or (0, 0, 0))))
            for ev in evs:
                ev["src"] = src
                self.handle(ev, c)

    def handle(self, ev, c):
        self.n_events += 1
        if self.calib:
            ev["action"] = None
            ev["calib"] = True
            self._calib_event(ev)
        else:
            action = self.router.route(ev, c)
            if ev.get("action") == "activation" and self.notifier:
                self.notifier(tr("gest.ready"))
            if action:
                if self.notifier:
                    self.notifier(tr("gest.action", name=_gesture_name(ev["type"]), action=action))
                if self.command:
                    self.command(action)
        self.events.append(ev)

    def cursor_frozen(self):
        """Vrai pendant la fenêtre d'action (après le double claquement) et juste après l'action :
        le curseur ne doit pas bouger pendant le hochement, sinon le clic tombe à côté."""
        if not self.buf.last:
            return False
        t = self.buf.last.t
        return t < self.router.armed_until or t < self.router.freeze_until

    def summary(self, n_trace=None):
        c = gesture_cfg(self.cfg_get())
        t = self.buf.last.t if self.buf.last else 0.0
        tr = list(self.trace)
        if tr:
            tr = [x for x in tr if x[0] > t - 4.0]
        return {
            "enabled": bool(c["enabled"]), "t": round(t, 3), "source": self.source,
            "armed": round(max(self.router.armed_until - t, 0.0), 2),
            "flags": self.det.flags(), "rejected": self.det.n_rejected,
            "events": list(self.events)[-12:],
            "trace": [[round(x[0] - t, 3), round(x[1], 2), round(x[2], 1), round(x[3], 1), round(x[4], 1)]
                      for x in (tr[-n_trace:] if n_trace else tr)],
            "calib": ({k: v for k, v in self.calib.items()} if self.calib else None),
            "rec": ({"path": self.rec["path"], "rows": self.rec["rows"], "label": self.rec["label"]}
                    if self.rec else None),
            "error": self.error,
        }


# =========================================================================== rejeu
def load_recording(path, source="auto", raw_devs=None):
    """-> liste de dicts {ts_ns, arrival, q, w, a, g, r, label}.

    path : CSV de ce module, capture décodée JSON (unités brutes) ou trames RemoteHID brutes
    [[t_arrivée, phase, hex], ...]. Pour ces dernières, source = "raw" (HID accel/gyro, horodatage
    capteur µs), "dm6" (devmotion6, horodatage capteur ns) ou "auto" (raw s'il est présent)."""
    rows = []
    if path.endswith(".csv"):
        with open(path, newline="") as f:
            for d in csv.DictReader(f):
                fl = lambda *ks: tuple(float(d[k]) for k in ks)  # noqa: E731
                r = fl("yaw", "pitch", "roll") if d.get("yaw") not in (None, "") else None
                rows.append({"ts_ns": int(round(float(d["t_sensor"]) * 1e9)),
                             "arrival": float(d["arrival"]) if d.get("arrival") else None,
                             "q": fl("qw", "qx", "qy", "qz"), "w": fl("gx", "gy", "gz"),
                             "a": fl("ax", "ay", "az"), "g": fl("grx", "gry", "grz"),
                             "r": r, "label": d.get("label", "")})
        return rows
    with open(path) as f:
        data = json.load(f)
    if data and isinstance(data[0], dict):              # capture décodée (unités brutes int16)
        k = GRAVITY / 1000.0
        for d in data:
            if not d.get("ts"):
                continue
            rows.append({"ts_ns": int(d["ts"]), "arrival": d.get("t"), "q": m.quat_from_att(d["att"]),
                         "w": tuple(v * 0.122 for v in d["gyr"]), "a": tuple(v * k for v in d["acc"]),
                         "g": tuple(float(v) for v in d["grav"]), "r": None, "label": d.get("ph", "")})
        return rows
    raw_devs = raw_devs or RAW_DEVS
    frames = [(t, ph, m.rhid_parse(bytes.fromhex(hx))) for t, ph, hx in data]
    if source == "auto":
        source = "raw" if any(k == 2 and dev in raw_devs for _, _, subs in frames for k, dev, _ in subs) else "dm6"
    fuser = RawImuFuser()
    for t, ph, subs in frames:
        for kind, dev, pl in subs:
            if kind != 2:
                continue
            if source == "raw" and dev in raw_devs:
                for row in fuser.push(raw_devs[dev], pl, t):
                    row["label"] = ph
                    rows.append(row)
            elif source == "dm6" and dev == 7 and len(pl) >= 59:
                blk = pl[1:]
                ts, w, g = m.parse_block(blk)
                att = struct.unpack_from("<hhh", blk, 20)
                rows.append({"ts_ns": ts, "arrival": t, "q": m.quat_from_att(att), "w": w,
                             "a": m.parse_accel(blk), "g": g, "r": None, "label": ph})
    return rows


def phase_report(events, rows, lag=0.0):
    """Événements par phase étiquetée -> {phase: {type: nombre}} (toutes les phases présentes).
    lag : l'événement est attribué à la phase active `lag` secondes plus tôt (temps de réaction :
    un geste commencé en fin de phase se termine souvent au début de la suivante)."""
    import bisect
    out = {}
    for r in rows:
        out.setdefault(r["label"], {})
    ts = [r["ts_ns"] / 1e9 for r in rows]
    for e in events:
        lab = e.get("label", "")
        if lag and ts:
            i = bisect.bisect_right(ts, e["t"] - lag) - 1
            lab = rows[max(i, 0)]["label"]
        d = out.setdefault(lab, {})
        d[e["type"]] = d.get(e["type"], 0) + 1
    return out


def replay(rows, cfg=None, profile=None, bias_cfg=None, verbose=False):
    """Rejoue des échantillons dans le détecteur complet. Renvoie (événements, statistiques capteur)."""
    c = gesture_cfg(cfg)
    c["enabled"] = True
    buf = SensorBuffer(c["max_interp_gap_s"], keep_s=1e9)
    det = GestureDetector(c)
    router = GestureRouter()
    eng = m.MotionEngine(bias_cfg or m.DEFAULTS["bias"], profile)
    events = []
    last_ts = None
    for row in rows:
        r = row["r"]
        if r is None:                                   # vitesses tête recalculées (profil donné ou défaut)
            dt = (row["ts_ns"] - last_ts) / 1e9 if last_ts else 0.01
            r = eng.step(row["w"], row["g"], dt if 0 < dt < 0.15 else 0.01)
        last_ts = row["ts_ns"]
        for s in buf.push(row["ts_ns"], row["arrival"], row["q"], row["w"], row["a"], row["g"], r, row["label"]):
            for ev in det.feed(s):
                router.route(ev, c)
                ev["label"] = s.label
                events.append(ev)
    return events, buf.stats(), det.n_rejected, buf


def main(argv=None):
    ap = argparse.ArgumentParser(description="Rejeu des gestes sur un enregistrement")
    ap.add_argument("--replay", required=True, help="CSV (gestures.py) ou capture JSON")
    ap.add_argument("--config", default=m.DEFAULT_CONFIG_PATH, help="config.json (seuils + profil)")
    ap.add_argument("--profile", default=None, help="clé de profil (ex. 2013_head) ; défaut : premier *_head")
    ap.add_argument("--no-activation", action="store_true", help="ignorer la fenêtre d'activation")
    ap.add_argument("--source", default="auto", choices=("auto", "raw", "dm6"),
                    help="trames brutes : IMU HID brute (raw) ou devmotion6 (dm6)")
    ap.add_argument("--phases", action="store_true", help="tableau des gestes par phase étiquetée")
    ap.add_argument("--lag", type=float, default=0.5, help="décalage d'attribution aux phases (s)")
    ap.add_argument("--stats", action="store_true", help="statistiques capteur (fréquence, trous, doublons)")
    ap.add_argument("--spectrum", action="store_true", help="énergie au-dessus de 25 et 50 Hz")
    a = ap.parse_args(argv)
    cfg = m.load_config(a.config) if os.path.exists(a.config) else json.loads(json.dumps(m.DEFAULTS))
    gc = dict(cfg.get("gestures", {}))
    if a.no_activation:
        gc["require_activation"] = False
    profs = cfg.get("profiles", {})
    key = a.profile or next((k for k in profs if k.endswith("_head")), None)
    rows = load_recording(a.replay, a.source)
    events, st, rej, buf = replay(rows, gc, profs.get(key) if key else None, cfg.get("bias"))
    t0 = rows[0]["ts_ns"] / 1e9 if rows else 0.0
    print(f"{len(rows)} échantillons, profil {key or 'par défaut'}")
    for e in events:
        extra = " ".join(f"{k}={v}" for k, v in e.items() if k not in ("t", "type", "conf", "peak", "label"))
        print(f"  t={e['t'] - t0:8.3f}s  {e['type']:<14} conf={e['conf']:.2f} pic={e['peak']:<7} "
              f"[{e.get('label', '')}] {extra}")
    print(f"{len(events)} événements ; rejets : {rej}")
    if a.phases:
        for ph, d in phase_report(events, rows, a.lag).items():
            print(f"  {ph or '(sans étiquette)':<14} " + (", ".join(f"{k}={v}" for k, v in sorted(d.items())) or "—"))
    if a.stats or a.spectrum:
        print("capteur :", json.dumps(st, ensure_ascii=False))
    if a.spectrum:
        # spectre moyen sur des fenêtres successives de 1,28 s du même segment
        segs = {}
        for s in buf.buf:
            segs.setdefault(s.seg, []).append(s)
        for kind, attr in (("acc", "a"), ("gyro", "w")):
            fr25 = []
            fr50 = []
            for ss in segs.values():
                n = 256
                for i in range(0, len(ss) - n, n):
                    win = ss[i:i + n]
                    span = win[-1].t - win[0].t
                    f, p = power_spectrum([[getattr(x, attr)[k] for x in win] for k in range(3)], (n - 1) / span)
                    tot = sum(p[1:]) or 1.0
                    fr25.append(sum(pp for ff, pp in zip(f, p) if ff >= 25) / tot)
                    fr50.append(sum(pp for ff, pp in zip(f, p) if ff >= 50) / tot)
            if fr25:
                print(f"spectre {kind} : énergie >25 Hz = {100 * _median(fr25):.1f} %, "
                      f">50 Hz = {100 * _median(fr50):.1f} % (médiane sur {len(fr25)} fenêtres)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
