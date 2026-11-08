#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Matteo Gaudioso (starman-tech) — https://github.com/starman-tech/mousepods
"""labo.py — page « Labo » : mesures en direct et sessions guidées (consignes à l'écran).

  LiveTap         détecteur PERMISSIF dédié (seuil bas + seuil adaptatif), alimenté échantillon par
                  échantillon par GestureEngine.sample_taps : courbes |acc passe-haut| et |gyro| à pleine
                  cadence, impulsions brèves (même sous le seuil réel) avec leurs caractéristiques.
  Protocoles      « dents » (incisives / molaires gauches / droites × doux / moyen / fort × 8),
                  « mouvements » (oui, non, inclinaisons, regard haut/bas × 5), « mots » (5 mots × 10,
                  EXPÉRIMENTAL, via silent_speech.py). Chaque consigne = une phase étiquetée.
  LaboSession     déroule le protocole, étiquette l'enregistrement brut ([t, phase, hex] dans
                  captures/private/, rejouable par gestures.load_recording), attribue les événements aux
                  consignes et calcule le bilan.
  Analyse (pure)  plages de force, séparabilité (centroïde le plus proche, leave-one-out, p-valeur
                  binomiale contre le hasard), taux de détection, proposition de seuil.
Aucune dépendance Bluetooth : tout se teste sur des données synthétiques (tests/test_labo.py).
"""
import math
import random
import threading
from collections import deque

import gestures as gz
from i18n import tr, tr_lazy

# (id, name, instruction) — names and instructions are translation keys (tr)
TEETH_GROUPS = (("inc", "labo.group.inc", "labo.cue.inc"),
                ("mol_g", "labo.group.mol_g", "labo.cue.mol_g"),
                ("mol_d", "labo.group.mol_d", "labo.cue.mol_d"))
FORCE_LEVELS = (("doux", "labo.level.doux"), ("moyen", "labo.level.moyen"), ("fort", "labo.level.fort"))
MOVES = (("hoche", ("nod",), "labo.cue.hoche"), ("secoue", ("shake",), "labo.cue.secoue"),
         ("penche_g", ("tilt_left",), "labo.cue.penche_g"),
         ("penche_d", ("tilt_right",), "labo.cue.penche_d"),
         ("haut", ("look_up",), "labo.cue.haut"),
         ("bas", ("look_down",), "labo.cue.bas"))
DEFAULT_WORDS = ("oui", "non", "clic", "menu", "stop")
CLICK_TYPES = ("teeth_click", "teeth_double")
# détecteur du Labo : seuil plancher bas, le seuil adaptatif (k × bruit) protège du bruit de fond
LAB_CFG = {"click_thresh": 0.15, "click_noise_k": 3.0, "require_activation": False, "enabled": True}


# =========================================================================== analyse (fonctions pures)
def percentile(xs, p):
    s = sorted(xs)
    if not s:
        return None
    k = (len(s) - 1) * p
    i = int(math.floor(k))
    j = min(i + 1, len(s) - 1)
    return s[i] + (s[j] - s[i]) * (k - i)


def binom_sf(k, n, p):
    """P(X >= k) pour X ~ Binomiale(n, p) (p-valeur unilatérale « mieux que le hasard »)."""
    if k <= 0:
        return 1.0
    if p <= 0:
        return 0.0
    if p >= 1:
        return 1.0
    return min(1.0, sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1)))


def click_vector(f):
    """Caractéristiques de séparabilité d'un claquement : direction du pic (vecteur unitaire, signe
    compris). La force varie avec le niveau demandé, la direction dépend (peut-être) de la dent."""
    v = f.get("vec") or (0.0, 0.0, 0.0)
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def loo_nearest_centroid(X, y):
    """Précision leave-one-out d'un classifieur au centroïde le plus proche + hasard + p-valeur.
    Hasard = fréquence de la classe majoritaire (ce que ferait « toujours répondre la même chose »)."""
    n = len(y)
    classes = sorted(set(y))
    if n < 4 or len(classes) < 2:
        return {"n": n, "error": tr_lazy("labo.error.too_few_examples")}
    ok = 0
    for i in range(n):
        cents = {}
        for c in classes:
            pts = [X[j] for j in range(n) if j != i and y[j] == c]
            if pts:
                cents[c] = [sum(p[k] for p in pts) / len(pts) for k in range(len(X[i]))]
        pred = min(cents, key=lambda c: sum((a - b) ** 2 for a, b in zip(X[i], cents[c])))
        ok += pred == y[i]
    chance = max(y.count(c) for c in classes) / n
    p = binom_sf(ok, n, chance)
    return {"n": n, "classes": {c: y.count(c) for c in classes}, "accuracy": round(ok / n, 3),
            "chance": round(chance, 3), "p_value": round(p, 4),
            "verdict": tr_lazy("labo.verdict.separable" if p < 0.01 else "labo.verdict.weak" if p < 0.05 else
                          "labo.verdict.chance")}


def force_summary(clicks):
    """clicks : [{"group", "level", "peak", ...}] -> plages de force par (groupe, niveau) et par groupe."""
    out = {"by_level": {}, "by_group": {}, "by_group_level": {}}
    for key, sel in (("by_level", lambda c: c["level"]), ("by_group", lambda c: c["group"]),
                     ("by_group_level", lambda c: f'{c["group"]}:{c["level"]}')):
        groups = {}
        for c in clicks:
            groups.setdefault(sel(c), []).append(c["peak"])
        out[key] = {k: {"n": len(v), "min": round(min(v), 3), "p10": round(percentile(v, 0.1), 3),
                        "median": round(percentile(v, 0.5), 3), "max": round(max(v), 3)}
                    for k, v in groups.items()}
    return out


def separability(clicks):
    """Molaires gauches vs droites, incisives vs molaires (direction du pic)."""
    res = {}
    lr = [c for c in clicks if c["group"] in ("mol_g", "mol_d")]
    res["mol_g_vs_mol_d"] = loo_nearest_centroid([click_vector(c) for c in lr], [c["group"] for c in lr])
    res["inc_vs_mol"] = loo_nearest_centroid([click_vector(c) for c in clicks],
                                             ["inc" if c["group"] == "inc" else "mol" for c in clicks])
    return res


def propose_threshold(soft_peaks, noise_peaks, noise_p99=None, lo=0.2, hi=3.0):
    """Seuil proposé entre le bruit et les claquements DOUX mesurés.

    soft_peaks : pics des claquements doux ; noise_peaks : pics des impulsions pendant le repos (aucun
    claquement demandé) ; noise_p99 : centile 99 de |acc passe-haut| au repos.
    Seuil = max(1,2 × plus forte impulsion de repos, 1,5 × p99 du repos, 0,25) puis, si possible, abaissé
    jusqu'à 0,8 × p10 des doux (sans jamais passer sous le plancher de bruit)."""
    floor = 1.2 * max(noise_peaks) if noise_peaks else 0.0
    if noise_p99:
        floor = max(floor, 1.5 * noise_p99)
    floor = max(floor, 0.25)
    if not soft_peaks:
        return {"click_thresh": None, "floor": round(floor, 3), "reason": tr_lazy("labo.reason.no_soft")}
    target = 0.8 * percentile(soft_peaks, 0.1)
    thr = min(max(target, floor, lo), hi)
    det = sum(p >= thr for p in soft_peaks) / len(soft_peaks)
    reason = tr_lazy("labo.reason.above_noise" if target >= floor else "labo.reason.noise_limited")
    return {"click_thresh": round(thr, 3), "floor": round(floor, 3), "soft_detect": round(det, 3),
            "soft_p10": round(percentile(soft_peaks, 0.1), 3), "n_soft": len(soft_peaks), "reason": reason}


def detection_rates(cues, events):
    """cues : [{"phase", "expect": types, "t0", "t1"}] ; events : [{"type", "wall"}] -> taux par phase.
    Un geste est compté pour la consigne active à son instant (pas de double comptage)."""
    out = {}
    for cu in cues:
        if not cu.get("expect"):
            continue
        evs = [e for e in events if cu["t0"] <= e["wall"] < cu["t1"]]
        d = out.setdefault(cu["phase"], {"cues": 0, "hits": 0, "other": {}})
        d["cues"] += 1
        d["hits"] += any(e["type"] in cu["expect"] for e in evs)
        for e in evs:
            if e["type"] not in cu["expect"]:
                d["other"][e["type"]] = d["other"].get(e["type"], 0) + 1
    for d in out.values():
        d["rate"] = round(d["hits"] / d["cues"], 3) if d["cues"] else 0.0
    return out


# =========================================================================== protocoles
def build_protocol(name, reps=None, words=None, seed=None):
    """-> liste d'étapes {phase, title, sub, dur, kind, ...}. Chaque claquement / geste / mot a sa
    consigne (repérage précis ; les impulsions sont attribuées à la consigne active)."""
    steps = [{"phase": "repos", "title": tr_lazy("labo.cue.rest"), "sub": tr_lazy("labo.cue.rest_sub"), "dur": 6.0,
              "kind": "rest"}]
    rng = random.Random(seed)
    if name == "dents":
        n = int(reps or 8)
        for g, gname, instr in TEETH_GROUPS:
            for lv, lvname in FORCE_LEVELS:
                title = tr_lazy("labo.cue.pair", a=tr_lazy(gname), b=tr_lazy(lvname))
                steps.append({"phase": "pause", "title": title, "sub": tr_lazy("labo.cue.teeth_intro", cue=tr_lazy(instr), n=n),
                              "dur": 3.0, "kind": "intro"})
                for i in range(n):
                    steps.append({"phase": f"dents_{g}_{lv}", "title": title,
                                  "sub": tr_lazy("labo.cue.click_now", i=i + 1, n=n), "dur": 1.6, "kind": "click",
                                  "group": g, "level": lv, "rep": i + 1, "expect": CLICK_TYPES})
    elif name == "mouvements":
        n = int(reps or 5)
        for ph, exp, instr in MOVES:
            title = tr_lazy(instr)
            steps.append({"phase": "pause", "title": title, "sub": tr_lazy("labo.cue.n_times", n=n), "dur": 3.0,
                          "kind": "intro"})
            for i in range(n):
                steps.append({"phase": ph, "title": title, "sub": tr_lazy("labo.cue.move_now", i=i + 1, n=n), "dur": 3.0,
                              "kind": "move", "expect": exp, "rep": i + 1})
                steps.append({"phase": "pause", "title": tr_lazy("labo.cue.back_center"), "sub": tr_lazy("labo.cue.still"),
                              "dur": 1.0, "kind": "rest"})
    elif name == "mots":
        n = int(reps or 10)
        ws = [w.strip() for w in (words or DEFAULT_WORDS) if w and w.strip()][:8] or list(DEFAULT_WORDS)
        order = [w for w in ws for _ in range(n)]
        rng.shuffle(order)
        steps.append({"phase": "pause", "title": tr_lazy("labo.cue.words_intro"), "sub": tr_lazy("labo.cue.words_intro_sub"),
                      "dur": 3.0, "kind": "intro"})
        for i, w in enumerate(order):
            steps.append({"phase": f"mot_{w}", "title": w.upper(),
                          "sub": tr_lazy("labo.cue.word_now", i=i + 1, n=len(order)), "dur": 2.2, "kind": "word", "word": w})
            steps.append({"phase": "pause", "title": "…", "sub": tr_lazy("labo.cue.mouth_closed"), "dur": 0.8,
                          "kind": "rest"})
    else:
        raise ValueError(tr_lazy("labo.error.unknown_protocol", name=name))
    steps.append({"phase": "fin", "title": tr_lazy("labo.cue.done"), "sub": tr_lazy("labo.cue.analysing"), "dur": 1.0,
                  "kind": "rest"})
    t = 0.0
    for s in steps:
        s["t0"], t = t, t + s["dur"]
        s["t1"] = t
    return steps


# =========================================================================== mesure en direct
class LiveTap:
    """Détecteur permissif dédié au Labo + tampon pleine cadence. Appelé dans la boucle principale."""

    def __init__(self, cfg_get=None, keep_s=8.0):
        self.cfg_get = cfg_get or (lambda: {})
        self.keep = keep_s
        self.pts = deque()              # (t capteur, |acc passe-haut|, |gyro| °/s)
        self.spikes = deque(maxlen=200)  # impulsions brèves (même sous le seuil réel)
        self.events = deque(maxlen=200)
        self.offset = None              # heure d'arrivée (monotone) - heure capteur
        self.det = None
        self.src = None
        self.listeners = []             # callbacks(kind "event"|"spike"|"hp", objet)
        self.lock = threading.Lock()

    def _cfg(self):
        c = dict(self.cfg_get() or {})
        c.pop("mapping", None)
        c.update(LAB_CFG)
        return c

    def wall(self, t):
        return t + (self.offset or 0.0)

    def on_sample(self, s, src):
        if src != self.src or self.det is None:
            self.src = src
            self.det = gz.GestureDetector(self._cfg())
            with self.lock:
                self.pts.clear()
        if s.arrival is not None:
            off = s.arrival - s.t
            # retard Bluetooth variable : on garde l'arrivée la plus précoce récente (≈ latence minimale)
            self.offset = off if self.offset is None or off < self.offset or off - self.offset > 0.5 else (
                self.offset + 0.001 * (off - self.offset))
        prev = self.det.last_spike
        evs = self.det.feed(s)
        gm = gz.vlen(s.w)
        with self.lock:
            self.pts.append((s.t, self.det.hp_mag, gm))
            while self.pts and self.pts[0][0] < s.t - self.keep:
                self.pts.popleft()
        sp = self.det.last_spike
        if sp is not None and sp is not prev:
            k = max(range(3), key=lambda i: abs(sp["vec"][i]))
            d = {"t": round(sp["t"], 4), "wall": self.wall(sp["t"]), "peak": round(sp["peak"], 3),
                 "dur_ms": round(sp["dur"] * 1000, 1), "vec": [round(v, 3) for v in sp["vec"]],
                 "axis": ("+" if sp["vec"][k] >= 0 else "-") + "xyz"[k], "gyro_max": round(sp["gmax"], 1),
                 "gyro_mean": round(self.det._gyro_mean(sp["t"]), 1), "thr": round(sp["thr"], 3),
                 "noise": None if self.det.noise is None else round(self.det.noise, 3)}
            self.spikes.append(d)
            for f in list(self.listeners):
                f("spike", d)
        for e in evs:
            e = dict(e, wall=self.wall(e["t"]), src=src)
            self.events.append(e)
            for f in list(self.listeners):
                f("event", e)
        hp = (self.wall(s.t), self.det.hp_mag)
        for f in list(self.listeners):
            f("hp", hp)

    def live(self, since=None, max_pts=1600):
        with self.lock:
            pts = list(self.pts)
        t_now = pts[-1][0] if pts else 0.0
        if since is not None:
            pts = [p for p in pts if p[0] > since]
        pts = pts[-max_pts:]
        det = self.det
        return {"t": round(t_now, 4), "pts": [[round(a, 4), round(b, 3), round(c, 1)] for a, b, c in pts],
                "spikes": [x for x in list(self.spikes) if x["t"] > t_now - self.keep],
                "events": [{k: v for k, v in e.items() if k != "wall"} for e in list(self.events)
                           if e["t"] > t_now - self.keep],
                "noise": None if det is None or det.noise is None else round(det.noise, 3),
                "thr": None if det is None else round(det.click_threshold(), 3),
                "src": self.src}


# =========================================================================== session guidée
class LaboSession:
    """Déroulé d'un protocole. `clock` = horloge monotone (injectable pour les tests).
    on_phase(phase) : appelé à chaque changement d'étape (étiquette de l'enregistrement brut)."""

    def __init__(self, protocol, steps, clock, on_phase=None, path=None):
        self.protocol, self.steps, self.clock, self.on_phase = protocol, steps, clock, on_phase
        self.path = path
        self.t0 = clock()
        self.idx = -1
        self.done = False
        self.cancelled = False
        self.spikes, self.events = [], []
        self.rest_hp = []               # |acc passe-haut| pendant le repos (bruit de fond)
        self.word_buf = None            # échantillons du mot en cours (protocole « mots »)
        self.word_samples = []          # [(mot, [(t, q, w, a)])]
        self.result = None
        self.speech_eval = None
        self.lock = threading.Lock()
        self.tick()

    @property
    def total(self):
        return self.steps[-1]["t1"]

    def cues(self):
        return [dict(s, t0=self.t0 + s["t0"], t1=self.t0 + s["t1"]) for s in self.steps]

    def step_at(self, wall):
        x = wall - self.t0
        for i, s in enumerate(self.steps):
            if s["t0"] <= x < s["t1"]:
                return i
        return None

    def tick(self, now=None):
        now = self.clock() if now is None else now
        with self.lock:
            if self.done:
                return
            x = now - self.t0
            i = next((k for k, s in enumerate(self.steps) if s["t0"] <= x < s["t1"]), len(self.steps))
            if i != self.idx:
                prev = self.steps[self.idx] if 0 <= self.idx < len(self.steps) else None
                if prev and prev["kind"] == "word" and self.word_buf is not None:
                    self.word_samples.append((prev["word"], self.word_buf))
                self.word_buf = None
                self.idx = i
                if i >= len(self.steps):
                    self.done = True
                else:
                    if self.steps[i]["kind"] == "word":
                        self.word_buf = []
                    if self.on_phase:
                        self.on_phase(self.steps[i]["phase"])
            if self.done:
                if self.on_phase:
                    self.on_phase("")
                self.result = self.analyze()

    def cancel(self):
        with self.lock:
            self.done = self.cancelled = True
        if self.on_phase:
            self.on_phase("")

    # ---- flux (boucle principale)
    def on_tap(self, kind, obj):
        if self.done:
            return
        if kind == "spike":
            i = self.step_at(obj["wall"])
            if i is not None:
                self.spikes.append(dict(obj, step=i))
        elif kind == "event":
            i = self.step_at(obj["wall"])
            if i is not None:
                self.events.append(dict(obj, step=i))
        elif kind == "hp":
            wall, hp = obj
            i = self.step_at(wall)
            if i is not None and self.steps[i]["kind"] == "rest" and self.steps[i]["phase"] == "repos":
                self.rest_hp.append(hp)

    def on_dm6(self, ts_ns, q, w, a):
        buf = self.word_buf
        if buf is not None:
            buf.append((ts_ns / 1e9, q, tuple(w), tuple(a)))

    # ---- bilan
    def teeth_clicks(self):
        """Une impulsion par consigne de claquement : la plus forte (les échos sont ignorés)."""
        best = {}
        for sp in self.spikes:
            st = self.steps[sp["step"]]
            if st["kind"] != "click":
                continue
            if sp["step"] not in best or sp["peak"] > best[sp["step"]]["peak"]:
                best[sp["step"]] = dict(sp, group=st["group"], level=st["level"])
        return [best[k] for k in sorted(best)]

    def analyze(self):
        res = {"protocol": self.protocol, "path": self.path, "cancelled": self.cancelled}
        rest_spikes = [sp["peak"] for sp in self.spikes if self.steps[sp["step"]]["phase"] == "repos"]
        res["noise"] = {"n": len(self.rest_hp),
                        "p90": round(percentile(self.rest_hp, 0.9), 3) if self.rest_hp else None,
                        "p99": round(percentile(self.rest_hp, 0.99), 3) if self.rest_hp else None,
                        "rest_spikes": len(rest_spikes),
                        "rest_max": round(max(rest_spikes), 3) if rest_spikes else None}
        if self.protocol == "dents":
            clicks = self.teeth_clicks()
            n_cues = sum(1 for s in self.steps if s["kind"] == "click")
            res["clicks"] = clicks
            res["captured"] = {"cues": n_cues, "with_impulse": len(clicks)}
            res["force"] = force_summary(clicks) if clicks else None
            res["separability"] = separability(clicks) if clicks else None
            res["proposal"] = propose_threshold([c["peak"] for c in clicks if c["level"] == "doux"],
                                                rest_spikes, res["noise"]["p99"])
            ok = res["proposal"].get("click_thresh")
            if ok:
                res["at_proposal"] = {lv: round(sum(c["peak"] >= ok for c in clicks if c["level"] == lv) /
                                                max(1, sum(c["level"] == lv for c in clicks)), 3)
                                      for lv, _ in FORCE_LEVELS}
        elif self.protocol == "mouvements":
            evs = [dict(e, wall=e["wall"]) for e in self.events if e["type"] not in CLICK_TYPES]
            res["rates"] = detection_rates(self.cues(), evs)
        elif self.protocol == "mots":
            res["words"] = {}
            for w, smp in self.word_samples:
                res["words"][w] = res["words"].get(w, 0) + 1
            res["speech"] = self.speech_eval or {"status": tr_lazy("labo.status.waiting")}
        return res

    def evaluate_words(self, n_perm=500, save_path=None):
        """Entraînement + évaluation silent_speech (lent : à lancer dans un thread)."""
        try:
            import silent_speech as ss
        except Exception as e:  # noqa: BLE001  (numpy absent…)
            self.speech_eval = {"error": tr_lazy("labo.error.speech_unavailable", error=e)}
            return self.speech_eval
        rec = ss.Recognizer()
        short = 0
        for w, smp in self.word_samples:
            X, fs = ss.features(smp)
            if len(X) < 20:
                short += 1
                continue
            rec.add(w, ss.preprocess(X, fs))
        ev = rec.evaluate(n_perm)
        ev["too_short"] = short
        ev["experimental"] = True
        if save_path:
            rec.save(save_path)
            ev["model"] = save_path
        self.speech_eval = ev
        if self.result is not None:
            self.result["speech"] = ev
        return ev

    def as_dict(self, now=None):
        now = self.clock() if now is None else now
        st = self.steps[self.idx] if 0 <= self.idx < len(self.steps) else None
        out = {"protocol": self.protocol, "done": self.done, "cancelled": self.cancelled, "path": self.path,
               "elapsed": round(now - self.t0, 2), "total": round(self.total, 1), "result": self.result}
        if st and not self.done:
            nxt = self.steps[self.idx + 1] if self.idx + 1 < len(self.steps) else None
            out["step"] = {"i": self.idx, "n": len(self.steps), "title": st["title"], "sub": st["sub"],
                           "kind": st["kind"], "phase": st["phase"], "left": round(self.t0 + st["t1"] - now, 2),
                           "dur": st["dur"], "next": nxt["title"] if nxt else None}
            out["live_count"] = sum(1 for sp in self.spikes if sp["step"] == self.idx)
        return out
