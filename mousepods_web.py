# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Matteo Gaudioso (starman-tech) — https://github.com/starman-tech/mousepods
"""Serveur de l'interface AirPods Mouse (http://127.0.0.1:8737).

Tourne dans le processus du contrôleur, dans un thread :
  GET  /                 application (ui/index.html)
  GET  /api/ping
  GET  /api/i18n         {"lang", "setting", "strings"} for the UI language
  GET  /api/state        état complet (appareil, batteries, capteurs, réglages, journal…)
  GET  /api/stream       télémétrie temps réel (Server-Sent Events, 30 Hz)
  POST /api/cmd          {"cmd": "recenter" | "toggle" | "mode" | "scroll" | ...}
  POST /api/config       {clé: valeur, ...}   (fusionné dans config.json, appliqué à chaud)
  POST /api/calib        {"action": "start" | "cancel", "source": "auto|head|case|hand"}
  POST /api/sensor       {"dev": 7, "action": "start|stop|get", "interval": 10000, "report": 1}
  POST /api/control      {"ident": 0x17, "values": [0, 0, 0, 0]}     (commande AAP 0x09)
  POST /api/raw          {"hex": "04000400..."}                       (avancé)
  GET  /api/gestures     gestes : événements, statistiques capteur, calibration, enregistrement
  POST /api/gestures     {"action": "enable|disable|set|mapping|calib_start|calib_cancel|rec_start|rec_label|
                          rec_stop|clear", ...}
  POST /api/speech       parole silencieuse (expérimental) : {"action": "start|stop|evaluate|forget|clear", "word"}
  GET  /api/labo?since=t Labo : courbes pleine cadence (|acc passe-haut|, |gyro|), impulsions, session guidée
  POST /api/labo         {"action": "start", "protocol": "dents|mouvements|mots", "reps", "words"} | {"action": "cancel"}
  GET  /api/opentrack    état de la sortie OpenTrack (réglages, paquets envoyés, pose)
  GET  /api/pose        pose de tête en direct pour la page /demo (cockpit 3D) ; garde la pose calculée tant que la page interroge
  GET  /api/version      version, channel (stable/test), release notes, "what's new" once per version
  POST /api/version      {"seen": "1.1.0"}
  GET  /api/calibration  calibration of a game profile: {"active", "step", "progress", "hint", "result"}
  POST /api/calibration  {"action": "start" | "cancel"} | {"action": "save_baseline", "yaw", "pitch", ...}
  GET  /calibrate        full-screen overlay for that calibration (ui/calibrate.html?roles=main,left,right)
  POST /api/opentrack    {"enabled": true, "host": "127.0.0.1", "port": 4242, ..., "joystick": {"enabled": true, ...}}
                         | {"action": "recenter"}       (page Simulateurs ; voir docs/SIMS.md)
  GET  /api/garmin       montre Garmin : module installé ?, connexion, calibration guidée, réglages
  GET  /api/garmin?paired=1  + appareils appairés (bluetoothctl devices Paired, lecture seule)
  POST /api/garmin       {"action": "connect", "address", "full"} | "disconnect" | "recenter" | "recalibrate"
                         | "pause" | {"action": "set", "values": {"max_speed": 2200, "mode": "pointeur", ...}}
Les actions qui touchent au Bluetooth sont mises en file et exécutées par la boucle principale.
"""
import base64
import json
import math
import os
import queue
import re
import struct
import threading
import time
from urllib.parse import parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import airpods_mouse_controller as m
import i18n
import paths
from i18n import tr

HERE = os.path.dirname(os.path.abspath(__file__))
UI_FILE = os.path.join(HERE, "ui", "index.html")
CALIBRATE_FILE = os.path.join(HERE, "ui", "calibrate.html")
DEMO_FILE = os.path.join(HERE, "ui", "cockpit.html")          # /demo : cockpit réaliste (WebGL)
DEMO_SIMPLE_FILE = os.path.join(HERE, "ui", "demo.html")      # /demo/simple : rendu canvas léger
# The website's cockpit demo may read the head pose and recenter, nothing else (CORS, Chrome's local network access)
SITE_ORIGINS = ("https://starman-tech.github.io",)
SITE_PATHS = ("/api/pose", "/api/i18n", "/api/cmd")
VR_FILE = os.path.join(HERE, "ui", "vr.html")                 # /vr : vidéos 360° / VR, la vue suit la tête
VENDOR = {"three.module.min.js": "text/javascript", "Sky.js": "text/javascript",
          "LICENSE-three.txt": "text/plain; charset=utf-8"}   # ui/vendor, liste fermée

MODELS = ["2013", "2014", "200e", "200f", "2002", "200a", "2019", "201b", "2024", "2027", "202d"]  # -> model.<pid>
INFO_LABEL_KEYS = ["info.name", "info.model", "info.manufacturer", "info.serial", "info.firmware", "info.firmware2",
                   "info.hardware", "info.updater", "info.serial_left", "info.serial_right",
                   "info.marketing_version"]
CONTROL_NAMES = {0x01: "mic_mode", 0x05: "button_send_mode", 0x0A: "ear_detection",
                 0x0C: "single_click_mode", 0x0D: "noise_control", 0x0E: "one_bud_anc",
                 0x14: "listening_mode_configs", 0x16: "long_press_action",
                 0x17: "double_click_interval", 0x18: "long_press_interval", 0x1A: "listening_modes",
                 0x1B: "one_bud_anc_mode", 0x1C: "tone_volume", 0x1D: "volume_swipe_interval",
                 0x1F: "call_management", 0x20: "volume_swipe", 0x23: "adaptive_volume",
                 0x24: "software_mute", 0x25: "conversation_detection", 0x28: "hearing_aid",
                 0x39: "raw_gestures"}                       # -> info.control.<id>
BATTERY_PART = {0x01: "single", 0x02: "right", 0x04: "left", 0x08: "case"}          # -> info.part.<id>
BATTERY_STATE = {0x00: "unknown", 0x01: "charging", 0x02: "battery", 0x04: "disconnected"}  # -> info.battery.<id>
EAR_STATE = {0: "in_ear", 1: "out_of_ear", 2: "in_case", 3: "absent"}             # -> info.ear.<id>
USAGE_NAMES = {(0x20, 0x30E): "Report Interval", (0x20, 0x31B): "Report Latency",
               (0x20, 0x31A): "Max FIFO Events", (0x20, 0x73): "info.usage.accel_3d",
               (0x20, 0x7A): "info.usage.gyro_3d",
               (0x20, 0x453): "info.usage.accel_x", (0x20, 0x454): "info.usage.accel_y",
               (0x20, 0x455): "info.usage.accel_z", (0x20, 0x457): "info.usage.ang_vel_x",
               (0x20, 0x458): "info.usage.ang_vel_y", (0x20, 0x459): "info.usage.ang_vel_z",
               (0x20, 0x434): "info.usage.timestamp", (0x20, 0x316): "Reporting State",
               (0x20, 0x319): "Power State"}                # "info.*" entries are translation keys
SERVICE_DESC = ("devmotion6", "accel", "gyro", "cma", "kadabra", "iedrelay", "neorelay", "mando")  # -> info.service.<name>
REPORT_KIND = {0x80: "input", 0x90: "output", 0xB0: "feature"}                     # -> info.report.<id>

def _usage_name(u):
    name = USAGE_NAMES.get(u)
    if name is None:
        return f"0x{u[0]:04x}:0x{u[1]:04x}"
    return tr(name) if name.startswith("info.") else name


def model_name(pid):
    return tr(f"model.{pid}") if pid in MODELS else f"Apple 0x{pid}"


def info_view(info):
    """Device info for the UI: stable `key` (compared by the script) + translated `label`."""
    out = []
    for i, v in enumerate(info):
        key = INFO_LABEL_KEYS[i] if i < len(INFO_LABEL_KEYS) else "info.field"
        out.append({"key": key, "label": tr(key, n=i + 1), "value": v})
    return out


def battery_view(battery):
    return [{"part": tr(f"info.part.{b['part']}") if b["part"] in BATTERY_PART.values() else b["part"],
             # 0xFF (255) means "unknown" (bud just put in the case, no reading yet): show no number
             "level": b["level"] if 0 <= b["level"] <= 100 else None, "state": b["state"],
             "state_label": tr(f"info.battery.{b['state']}") if b["state"] in BATTERY_STATE.values() else b["state"]}
            for b in battery]


def ear_view(ear):
    if ear is None:
        return None
    return [tr(f"info.ear.{e}") if e in EAR_STATE.values() else e for e in ear]


def control_view(c):
    name = CONTROL_NAMES.get(c["ident"])
    return {**c, "name": tr(f"info.control.{name}") if name else tr("info.unknown")}


def hid_reports(desc):
    """Décode un descripteur HID -> liste de rapports {kind, id, bits, usages}."""
    out = []
    i, page, rid, size, count, usages = 0, 0, 0, 0, 1, []
    while i < len(desc):
        b = desc[i]
        sz = [0, 1, 2, 4][b & 3]
        tag = b & 0xFC
        v = int.from_bytes(desc[i + 1:i + 1 + sz], "little")
        i += 1 + sz
        if tag == 0x04:
            page = v
        elif tag == 0x84:
            rid = v
        elif tag == 0x74:
            size = v
        elif tag == 0x94:
            count = v
        elif tag == 0x08:
            usages.append((v >> 16, v & 0xFFFF) if sz == 4 else (page, v))
        elif tag in (0x80, 0x90, 0xB0):
            kind = REPORT_KIND[tag]
            out.append({"kind": kind, "kind_label": tr(f"info.report.{kind}"), "id": rid, "bits": size * count,
                        "usages": [_usage_name(u) for u in usages]})
            usages = []
        elif tag == 0xC0:
            usages = []
    return out


def plist_info(xml):
    x = xml.decode(errors="replace")
    d = {k: v for k, v in re.findall(r"<key>(\w+)</key>\s*<(?:integer|string)>([^<]*)<", x)}
    rd = re.search(r"ReportDescriptor</key>\s*<data>([^<]*)<", x)
    desc = base64.b64decode(rd.group(1)) if rd else b""
    return d, desc


class DeviceState:
    """Tout ce qu'on apprend des paquets AAP (hors mouvement)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.info = []
        self.battery = []
        self.ear = None
        self.controls = {}
        self.log = []
        self.n_motion = 0
        self.counts = {}

    def feed(self, pkt, direction="rx"):
        with self.lock:
            op = pkt[4] if len(pkt) > 5 and pkt[:4] == m.HDR else (-1 if pkt[:2] != b"\x00\x00" else -2)
            self.counts[op] = self.counts.get(op, 0) + 1
            motion = pkt[:10] in (m.WX_PREFIX,) or (pkt[:10] == m.RHID_PREFIX and len(pkt) in range(70, 90))
            if motion:
                self.n_motion += 1
                if self.n_motion % 100 != 1:      # n'enregistrer qu'1 paquet de mouvement sur 100
                    return
            self.log.append({"t": time.time(), "dir": direction, "op": op, "len": len(pkt),
                             "hex": pkt[:96].hex(" ") + (" …" if len(pkt) > 96 else "")})
            del self.log[:-400]
            if direction != "rx" or pkt[:4] != m.HDR or len(pkt) < 7:
                return
            if op == 0x04 and len(pkt) >= 7:
                n = pkt[6]
                bat = []
                for k in range(n):
                    e = pkt[7 + 5 * k:12 + 5 * k]
                    if len(e) == 5:
                        bat.append({"part": BATTERY_PART.get(e[0], hex(e[0])), "level": e[2],
                                    "state": BATTERY_STATE.get(e[3], hex(e[3]))})   # ids, see battery_view
                self.battery = bat
            elif op == 0x06 and len(pkt) >= 8:
                self.ear = [EAR_STATE.get(pkt[6], str(pkt[6])), EAR_STATE.get(pkt[7], str(pkt[7]))]
            elif op == 0x1D:
                parts = [p.decode(errors="replace") for p in pkt[11:].split(b"\x00")]
                vals = [p for p in parts if p and all(32 <= ord(c) < 127 or ord(c) > 160 for c in p)]
                self.info = vals[:14]                       # labels: info_view
            elif op == 0x09 and len(pkt) >= 11:
                ident = pkt[6]
                self.controls[ident] = {"ident": ident, "values": list(pkt[7:11])}   # name: control_view


AIM_TEXT = {"aim_center": "calib.step.aim_center", "aim_tl": "calib.step.aim_tl",
            "aim_br": "calib.step.aim_br"}                  # translation keys
GLIDE_TEXT = {"flat": "calib.step.flat", "slide_right": "calib.step.slide_right",
              "slide_fwd": "calib.step.slide_fwd", "flat_back": "calib.step.flat_back"}
FLOWS = {"souris": m.CALIB_STEPS, "pointeur": m.CALIB_STEPS + ["aim_center", "aim_tl", "aim_br"],
         "glisse": ["flat", "slide_right", "flat_back", "slide_fwd", "flat_back"]}


class CalibSession:
    """Calibration guidée pilotée depuis l'interface.

    souris   : axes (immobile, gauche, haut, épaule/rotation droite)
    pointeur : axes + visée du centre et de deux coins -> amplitude exacte de l'écran
    glisse   : boîtier à plat, glisser à droite puis vers l'avant -> axes de la table
    """

    def __init__(self, ctl, cfg_path, source, flow=None):
        self.ctl, self.cfg_path = ctl, cfg_path
        self.flow = flow if flow in FLOWS else ctl.cfg["mode"]
        self.source = source if source != "auto" else ctl.source
        self.steps = FLOWS[self.flow]
        self.i = 0
        self.recent = []
        self.still_since = None
        self.bias = self.up = None
        self.acc = None
        self.res = {}
        self.progress = 0.0
        self.done = False
        self.message = None
        self.engine = None
        self.ang = [0.0, 0.0]
        self.aims = {}
        self.slide = None
        self.was_paused = ctl.paused
        ctl.paused = True

    @property
    def step(self):
        return self.steps[self.i] if self.i < len(self.steps) else "done"

    def text(self):
        if self.done:
            return tr("calib.done")
        st = self.step
        if st in AIM_TEXT:
            return tr(AIM_TEXT[st])
        if st in GLIDE_TEXT:
            return tr(GLIDE_TEXT[st])
        return tr(m.CALIB_TEXT[self.source][st])

    def _still(self, now, need=1.0, with_acc=False):
        ws = [r[1] for r in self.recent]
        if len(ws) < 5:
            return False
        rng = max((max(v[k] for v in ws) - min(v[k] for v in ws)) for k in range(3))
        ok = rng < 6.0
        if with_acc and self.up:
            ok = ok and max(m.vlen(m.reject(r[3], self.up)) for r in self.recent) < 0.35
        if ok:
            self.still_since = self.still_since or now
            self.progress = min((now - self.still_since) / need, 1.0)
            return self.progress >= 1.0
        self.still_since, self.progress = None, 0.0
        return False

    def hook(self, w, g, dt, a=(0.0, 0.0, 0.0)):
        if self.done:
            return
        now = time.monotonic()
        self.recent.append((now, w, g, a))
        self.recent = [r for r in self.recent if r[0] > now - 0.4]
        st = self.step
        if self.engine is not None:                     # visée des coins : angles intégrés
            y, p, _ = self.engine.step(w, g, dt)
            self.ang[0] += m.soft_deadzone(y, 0.6) * dt
            self.ang[1] += m.soft_deadzone(p, 0.6) * dt
        if st in ("still", "back", "flat", "flat_back"):
            if self._still(now, 1.0, with_acc=st.startswith("flat") and self.up is not None):
                if st in ("still", "flat") and self.bias is None:
                    ws = [r[1] for r in self.recent]
                    gs = [r[2] for r in self.recent]
                    self.bias = tuple(sum(v[k] for v in ws) / len(ws) for k in range(3))
                    self.up = m.vnorm(tuple(sum(v[k] for v in gs) / len(gs) for k in range(3)))
                self._next()
        elif st in AIM_TEXT:
            if self._still(now, 1.0):
                if st == "aim_center":
                    self.ang = [0.0, 0.0]
                else:
                    self.aims[st] = tuple(self.ang)
                if st == "aim_br":
                    rng = m.corner_range(self.aims.get("aim_tl", (0, 0)), self.aims["aim_br"])
                    if rng is None:
                        self.message = i18n.tr_lazy("calib.corners_mismatch")
                        self.i = self.steps.index("aim_center")
                        self.still_since, self.progress = None, 0.0
                        return
                    self.res["range"] = rng
                self._next()
        elif st in ("slide_right", "slide_fwd"):
            if self.slide is None:
                self.slide = {"v": (0.0, 0.0, 0.0), "peak": (0.0, 0.0, 0.0), "quiet": 0.0}
            sl = self.slide
            ah = m.reject(a, self.up)
            sl["v"] = tuple(v + c * dt for v, c in zip(sl["v"], ah))
            if m.vlen(sl["v"]) > m.vlen(sl["peak"]):
                sl["peak"] = sl["v"]
            pk = m.vlen(sl["peak"])
            self.progress = min(pk / 0.12, 1.0)
            moving = m.vlen(ah) > 0.35 or m.vlen(m.vsub(w, self.bias)) > 15
            sl["quiet"] = 0.0 if moving else sl["quiet"] + dt
            if pk >= 0.12 and sl["quiet"] > 0.2:
                self.res[st] = sl["peak"]
                self._next()
        else:
            if self.acc is None:
                self.acc = m.CalibAccumulator(self.bias, self.up)
            self.acc.add(w, dt)
            mag = abs(self.acc.yaw) if st == "yaw" else self.acc.horiz_deg()
            self.progress = min(mag / 20.0, 1.0)
            if mag >= 20.0:
                self.res[st] = self.acc.yaw if st == "yaw" else self.acc.horiz
                self._next()

    def _next(self):
        self.i += 1
        self.still_since, self.acc, self.slide, self.progress, self.message = None, None, None, 0.0, None
        if self.flow != "glisse" and self.step == "aim_center" and self.engine is None:
            prof = m.profile_from_calibration(self.up, self.res["yaw"], self.res["pitch"], self.res["roll"])
            self.res["axes"] = prof
            self.engine = m.MotionEngine(self.ctl.cfg["bias"], prof)
            self.engine.bias, self.engine.bias_ok = self.bias, True
        if self.i >= len(self.steps):
            self.finish()

    def finish(self):
        if self.flow == "glisse":
            prof = m.glide_profile(self.res["slide_right"], self.res["slide_fwd"])
            key = f"{self.ctl.pid}_glide"
        else:
            prof = self.res.get("axes") or m.profile_from_calibration(self.up, self.res["yaw"],
                                                                      self.res["pitch"], self.res["roll"])
            if "range" in self.res:
                prof["range"] = self.res["range"]
            key = f"{self.ctl.pid}_{self.source}"
        with open(self.cfg_path) as f:
            user = json.load(f)
        old = user.setdefault("profiles", {}).get(key) or {}
        if "range" not in prof and isinstance(old.get("range"), dict) and self.flow == "souris":
            prof["range"] = old["range"]       # screen edges (baseline / corners) are not measured here
        user["profiles"][key] = prof
        m.update_config_file(self.cfg_path, {"profiles": user["profiles"]})
        self.key, self.profile = key, prof
        self.done = True
        self.progress = 1.0
        self.ctl.paused = self.was_paused
        self.ctl.command("reload")
        self.ctl.recenter(quiet=True)

    def cancel(self):
        self.ctl.paused = self.was_paused

    def as_dict(self):
        return {"active": not self.done, "done": self.done, "source": self.source, "flow": self.flow,
                "step": self.step, "index": self.i, "total": len(self.steps), "text": self.text(),
                "progress": self.progress, "key": getattr(self, "key", None), "message": self.message,
                "profile": getattr(self, "profile", None)}


def write_instance_token():
    """Random token proving to the desktop app that this server is our controller
    (desktop/controller.py). Per-user file, 0600, replaced atomically, never followed as a link."""
    import secrets
    from desktop.controller import token_path
    token = secrets.token_hex(16)
    path = token_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.unlink(tmp)                   # left over by a crashed process with the same pid
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    os.replace(tmp, path)
    return token


def saved_config_value(cfg_path, key):
    """A top-level value from config.json as saved on disk (None if missing/unreadable)."""
    try:
        with open(cfg_path) as f:
            return json.load(f).get(key)
    except (OSError, ValueError):
        return None


class WebUI:
    def __init__(self, ctl, cfg_path, port=8737):
        self.ctl, self.cfg_path, self.port = ctl, cfg_path, port
        self.dev = DeviceState()
        self.q = queue.Queue()
        self.aap = None
        i18n.apply_config(getattr(ctl, "cfg", None))
        self.status = i18n.tr_lazy("srv.status.starting")
        self.calib = None
        self.t_start = time.time()
        self.server = None
        self.rec = None           # enregistrement brut en cours : {"until", "path", "rows"}
        self.speech = None        # parole silencieuse (expérimental)
        self.speech_result = None
        try:
            import silent_speech
            self.speech = silent_speech.SpeechSession(os.path.join(paths.captures_dir(private=True), "silent_speech.json"))
            ctl.gesture_hooks.append(self.speech.on_sample)
        except Exception as e:  # noqa: BLE001
            m.log.warning("Parole silencieuse indisponible : %s", e)
        self.labo_tap = None      # page Labo : détecteur permissif + session guidée
        self.labo = None
        self.labo_done = None
        try:
            import labo
            self._labo_mod = labo
            g = getattr(ctl, "gestures", None)
            if g is not None:
                self.labo_tap = labo.LiveTap(lambda: ctl.cfg.get("gestures", {}))
                self.labo_tap.listeners.append(self._labo_listen)
                g.sample_taps.append(self.labo_tap.on_sample)
            ctl.gesture_hooks.append(self._labo_dm6)
        except Exception as e:  # noqa: BLE001
            m.log.warning("Labo indisponible : %s", e)
        self.garmin = None        # montre Garmin (garmin/service.py) ; garmin-ble est optionnel
        self.garmin_error = None
        try:
            from garmin import service as garmin_service
            self.garmin = garmin_service.GarminService().autostart()
        except Exception as e:  # noqa: BLE001
            self.garmin_error = str(e)
            m.log.warning("Module Garmin indisponible : %s", e)

    # ---- liaison avec la boucle principale
    def attach(self, aap):
        self.aap = aap
        self.dev.reset()
        aap.on_raw = lambda d, p: self.dev.feed(p, d)
        self.status = i18n.tr_lazy("srv.status.connected")

    def set_status(self, s):
        self.status = s

    def feed_packet(self, pkt):
        self.dev.feed(pkt, "rx")
        rec = self.rec
        if rec:
            now = time.monotonic()
            if now < rec["until"]:
                rec["rows"].append((round(now - rec["t0"], 4), rec.get("phase", ""), pkt.hex()))
            else:
                self.rec = None
                with open(rec["path"], "w") as f:
                    json.dump(rec["rows"], f)
                m.log.info("Enregistrement terminé : %s (%d paquets)", rec["path"], len(rec["rows"]))

    # ---- calibration of a game profile: head movement first (CalibSession), then the points
    HEAD_NO_DATA_S = 3.0

    def start_point_calibration(self):
        self.ctl.cancel_calibration()
        self.combo = {"old": self.calib, "t0": time.monotonic()}
        self.combo_end = None
        self.q.put(("calib", "start", "head", "souris"))

    def cancel_point_calibration(self):
        if getattr(self, "combo", None):
            self.combo = None
            self.combo_end = {"active": False, "step": "cancelled", "progress": 0.0, "hint": None, "result": None}
            self.q.put(("calib", "cancel"))
        else:
            self.ctl.cancel_calibration()

    def point_calibration_state(self):
        now = time.monotonic()
        combo = getattr(self, "combo", None)
        if combo:
            head = self.calib if self.calib is not combo["old"] else None
            seen = getattr(self.ctl, "last_ht", 0.0) or 0.0
            if now - max(seen, combo["t0"]) > self.HEAD_NO_DATA_S:       # no motion data: AirPods gone
                self.combo = None
                self.combo_end = {"active": False, "step": "failed", "progress": 0.0, "hint": "no_signal",
                                  "result": None}
                self.q.put(("calib", "cancel"))
            elif head is not None and head.done:
                self.combo, self.combo_end = None, None
                self.ctl.start_calibration()                           # head learned: on to the points
            else:
                d = head.as_dict() if head is not None else {}
                return {"active": True, "phase": "head", "step": "head", "progress": d.get("progress", 0.0),
                        "hint": None, "result": None,
                        "head": {k: d.get(k) for k in ("text", "index", "total", "progress", "message")}}
        if getattr(self, "combo_end", None) and self.ctl.calibration is None:
            return self.combo_end                                      # cancelled or failed while learning
        self.ctl.calibration_tick(now)                                 # also ends it when the AirPods are gone
        st = self.ctl.calibration_state()
        return {**st, "phase": "points"} if st.get("step") else st

    def process_queue(self):
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                return
            try:
                self._do(item)
            except Exception as e:  # noqa: BLE001
                m.log.warning("Action interface échouée %s : %s", item, e)

    def _do(self, item):
        kind = item[0]
        aap = self.aap
        if kind == "cmd":
            self.ctl.command(item[1])
        elif kind == "calib":
            if hasattr(self, "_calib_done_t"):
                del self._calib_done_t
            if self.calib:
                self.calib.cancel()
                self._unhook()
            self.calib = None
            if item[1] == "start":
                self.calib = CalibSession(self.ctl, self.cfg_path, item[2], item[3] if len(item) > 3 else None)
                self.ctl.sample_hooks.append(self.calib.hook)
        elif kind == "sensor" and aap and aap.sock:
            _, dev, action, interval, rid = item
            if action == "start":
                aap.rhid_set_feature(dev, 1, int(interval))
                aap.rhid_active[dev] = int(interval)
            elif action == "stop":
                aap.rhid_set_feature(dev, 1, 0)
                aap.rhid_active.pop(dev, None)
            elif action == "get":
                aap._rhid_send([m.rhid_sub(dev, m.RHID_GET, bytes([m.HID_FEATURE, int(rid)]))])
            elif action == "set":
                aap.rhid_set_feature(dev, int(rid), int(interval))
        elif kind == "control" and aap and aap.sock:
            vals = (list(item[2]) + [0, 0, 0, 0])[:4]
            aap.send(m.ctrl_cmd(int(item[1]), *[int(v) & 0xFF for v in vals]))
        elif kind == "raw" and aap and aap.sock:
            aap.send(bytes.fromhex(item[1].replace(" ", "")))
        elif kind == "reconnect" and aap and aap.sock:
            aap.sock.shutdown(2)
        elif kind == "gest":
            self._gest(item[1])
        elif kind == "speech" and self.speech:
            self.speech_result = self.speech.action(item[1])

    def _gest(self, body):
        """Actions gestes exécutées dans la boucle principale (même thread que les échantillons)."""
        g = getattr(self.ctl, "gestures", None)
        if g is None:
            return
        a = body.get("action")
        if a == "calib_start":
            g.start_calib(str(body.get("gesture", "nod")), body.get("reps"))
        elif a == "calib_cancel":
            g.cancel_calib()
        elif a == "rec_start":
            name = re.sub(r"[^\w.-]", "_", str(body.get("name") or f"gestes_{int(time.time())}"))
            g.start_record(os.path.join(paths.captures_dir(private=True), name + ".csv"), str(body.get("label", "")))
        elif a == "rec_label":
            g.set_label(str(body.get("label", "")))
        elif a == "rec_stop":
            info = g.stop_record()
            if info:
                m.log.info("Enregistrement gestes : %s (%d lignes)", info["path"], info["rows"])
        elif a == "clear":
            g.events.clear()

    # ---- Labo
    def _labo_listen(self, kind, obj):
        sess = self.labo
        if sess is not None and not sess.done:
            sess.on_tap(kind, obj)
            if kind == "hp":
                sess.tick()

    def _labo_dm6(self, ts_ns, now, att, w, g, a, rates):
        sess = self.labo
        if sess is None or sess.done:
            return
        q = None
        if att is not None and len(att) == 3:
            x, y, z = (c / 32768.0 for c in att)
            n2 = x * x + y * y + z * z
            q = (math.sqrt(max(0.0, 1 - n2)), x, y, z) if 0.01 < n2 < 1.01 else None
        sess.on_dm6(ts_ns, q, w, a)
        sess.tick(now)

    def labo_start(self, body):
        L = self._labo_mod
        proto = str(body.get("protocol", "dents"))
        words = body.get("words")
        if isinstance(words, str):
            words = [w for w in re.split(r"[,;\s]+", words) if w]
        steps = L.build_protocol(proto, body.get("reps"), words)
        if self.labo is not None and not self.labo.done:
            self.labo.cancel()
        name = f"labo_{proto}_{time.strftime('%Y%m%d_%H%M%S')}"
        path = os.path.join(paths.captures_dir(private=True), name + ".json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        now = time.monotonic()
        rec = {"t0": now, "until": now + steps[-1]["t1"] + 1.5, "path": path, "rows": [], "phase": ""}
        self.rec = rec

        def on_phase(ph):
            if self.rec is rec:
                rec["phase"] = ph
        self.labo = L.LaboSession(proto, steps, time.monotonic, on_phase, path)
        self.labo_done = None
        return {"ok": True, "path": path, "total": steps[-1]["t1"]}

    def labo_cancel(self):
        sess = self.labo
        if sess is not None and not sess.done:
            sess.cancel()
            if self.rec and self.rec.get("path") == sess.path:
                self.rec["until"] = time.monotonic()      # enregistrement partiel conservé
            sess.result = sess.analyze()

    def labo_poll(self):
        """Fin de session : bilan écrit à côté de l'enregistrement ; mots -> évaluation dans un thread."""
        sess = self.labo
        if sess is None:
            return
        sess.tick()
        if sess.done and self.labo_done is not sess:
            self.labo_done = sess
            if sess.result is None:
                sess.result = sess.analyze()
            base = os.path.splitext(sess.path)[0]

            def finish():
                if sess.protocol == "mots" and not sess.cancelled:
                    sess.evaluate_words(500, base + "_modele.json")
                try:
                    with open(base + "_bilan.json", "w") as f:
                        json.dump(sess.result, f, ensure_ascii=False, indent=1, default=str)
                except OSError as e:
                    m.log.warning("Labo : bilan non écrit : %s", e)
            threading.Thread(target=finish, daemon=True).start()

    def labo_state(self, since=None):
        self.labo_poll()
        gc = m.DEFAULTS["gestures"].copy()
        gc.update({k: v for k, v in self.ctl.cfg.get("gestures", {}).items() if k != "mapping"})
        return {"live": self.labo_tap.live(since) if self.labo_tap else None,
                "session": self.labo.as_dict() if self.labo else None,
                "config": {k: gc.get(k) for k in ("enabled", "click_thresh", "click_adaptive", "click_noise_k",
                                                  "use_raw_imu")}}

    def garmin_state(self, paired=False):
        g = self.garmin
        if g is None:
            return {"available": False, "missing": [], "error": self.garmin_error or tr("srv.error.garmin_missing")}
        d = g.state()
        if paired:
            d["paired"] = g.paired_devices()
        return d

    def garmin_action(self, body):
        g = self.garmin
        if g is None:
            raise RuntimeError(tr("srv.error.garmin_not_installed"))
        a = body.get("action")
        if a == "connect":
            g.connect(body.get("address") or None, bool(body.get("full")))
        elif a == "disconnect":
            g.disconnect()
        elif a == "recenter":
            g.recenter()
        elif a == "recalibrate":
            g.recalibrate_all()
        elif a == "pause":
            return {"ok": True, "enabled": g.pause()}
        elif a == "set":
            return {"ok": True, "values": g.set(body.get("values", {}))}
        else:
            raise ValueError(tr("srv.error.unknown_action"))
        return {"ok": True}

    def _unhook(self):
        self.ctl.sample_hooks[:] = [h for h in self.ctl.sample_hooks
                                    if getattr(h, "__self__", None) is not self.calib]

    # ---- données
    def telemetry(self):
        ctl, aap = self.ctl, self.aap
        now = time.monotonic()
        times = [t for t in ctl.sample_times if t > now - 2.0]
        rate = len(times) / 2.0
        gaps = sum(1 for a, b in zip(times, times[1:]) if b - a > 0.05)
        hist = ctl.history[-90:]
        last = hist[-1] if hist else None
        if self.calib and self.calib.done and now - getattr(self, "_calib_done_t", now) > 6:
            self._unhook()
        if self.calib and self.calib.done and not hasattr(self, "_calib_done_t"):
            self._calib_done_t = now
        live = bool(times) and now - times[-1] < 0.5
        return {
            "status": self.status if (aap and aap.sock) else self.status,
            "connected": bool(aap and aap.sock), "live": live,
            "rate": round(rate), "gaps": gaps,
            "mode": ctl.cfg["mode"], "source": ctl.source, "source_cfg": ctl.cfg["source"],
            "place": ctl.place, "paused": ctl.paused, "scroll": ctl.scroll_mode,
            "rates": [round(v, 2) for v in ctl.rates],
            "w": [round(v, 2) for v in last[1]] if last else [0, 0, 0],
            "g": [round(v / 1000, 3) for v in last[2]] if last else [0, 0, 0],
            "att": list(last[4]) if last else [0, 0, 0],
            "bias": [round(v, 2) for v in ctl.engine.bias], "bias_ok": ctl.engine.bias_ok,
            "acc": [round(v, 2) for v in getattr(ctl, "last_acc", (0, 0, 0))],
            "glide_v": [round(v * 100, 1) for v in ctl.glide.v],
            "target": [round(v, 4) for v in ctl.target], "cur": [round(v, 4) for v in ctl.cur],
            "vel": [round(v) for v in ctl.vel],
            "series": [[round(h[3][0], 1), round(h[3][1], 1), round(h[3][2], 1)] for h in hist[::3]],
            "calib": self.calib.as_dict() if self.calib else None,
            "battery": battery_view(self.dev.battery), "ear": ear_view(self.dev.ear),
            "gestures": self._gest_summary(),
            "speech": self.speech.as_dict() if self.speech else None,
        }

    def _gest_summary(self):
        g = getattr(self.ctl, "gestures", None)
        if g is None:
            return None
        s = g.summary()
        s["trace"] = s["trace"][::2]
        return s

    def state(self):
        aap, ctl = self.aap, self.ctl
        services = []
        if aap:
            for name, dev in sorted(aap.rhid_devs.items(), key=lambda kv: kv[1]):
                info, desc = plist_info(aap.rhid_plist.get(dev, b""))
                last = aap.rhid_last.get(dev)
                services.append({
                    "name": name, "dev": dev, "desc": tr(f"info.service.{name}") if name in SERVICE_DESC else "",
                    "max_report": info.get("MaxReportSize"), "keys": info,
                    "reports": hid_reports(desc), "count": aap.rhid_counts.get(dev, 0),
                    "last_age": round(time.monotonic() - last[0], 2) if last else None,
                    "last_hex": last[1][:80].hex(" ") if last else None,
                    "motion": dev == aap.rhid_motion,
                })
        replies = []
        if aap:
            for t, kind, dev, pl in aap.rhid_replies[-12:]:
                replies.append({"age": round(time.monotonic() - t, 1),
                                "kind": "GetReport" if kind == m.RHID_GET else "SetReport",
                                "dev": dev, "hex": pl.hex(" ")})
        with self.dev.lock:
            log = list(self.dev.log[-150:])
            counts = {("0x%02x" % k if k >= 0 else ("handshake" if k == -2 else tr("info.opcode_other"))): v
                      for k, v in self.dev.counts.items()}
        cfg = {k: v for k, v in ctl.cfg.items()}
        hs = getattr(ctl, "headsets", None)
        return {
            "headsets": hs.status() if hs else [],     # other headsets (Android head-tracker HID), experimental
            "addr": aap.addr if aap else None, "pid": aap.pid if aap else None,
            "model": model_name(aap.pid) if aap else None,
            "protocol": ("RemoteHID (0x800)" if aap and aap.rhid_devs else "SensorDataWX (0x100000)")
            if aap else None,
            "interval_us": aap.interval_us if aap else None,
            "info": info_view(self.dev.info), "battery": battery_view(self.dev.battery),
            "ear": ear_view(self.dev.ear),
            "controls": [control_view(c) for c in sorted(self.dev.controls.values(), key=lambda c: c["ident"])],
            "services": services, "replies": replies, "log": log, "counts": counts,
            "config": cfg, "profiles": ctl.cfg.get("profiles", {}), "flatpak": paths.in_flatpak(),
            "profile_key": ctl.profile_key(), "calibrated": ctl.profile_key() in ctl.cfg.get("profiles", {}),
            "uptime": round(time.time() - self.t_start),
            "gesture_stats": ctl.gestures.buf.stats() if getattr(ctl, "gestures", None) else None,
        }

    # ---- serveur HTTP
    def start(self):
        ui = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj, code=200):
                data = i18n.encode_json(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self._cors()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _site_origin(self):
                origin = self.headers.get("Origin")
                return origin if origin in SITE_ORIGINS and self.path.split("?")[0] in SITE_PATHS else None

            def _cors(self):
                origin = self._site_origin()
                if origin:
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")

            def do_OPTIONS(self):                           # preflight from the website (CORS + local network access)
                origin = self._site_origin()
                if not self._local_host() or not origin:
                    self.send_response(403); self.end_headers(); return
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Methods", "GET, POST")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Access-Control-Max-Age", "600")
                self.send_header("Vary", "Origin")
                self.end_headers()

            def _local_host(self):
                """DNS-rebinding guard: only answer requests addressed to our own host name."""
                port = self.server.server_address[1]
                host = (self.headers.get("Host") or "").lower()
                return host in (f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost")

            def do_GET(self):
                if not self._local_host():
                    return self._json({"error": tr("srv.error.forbidden")}, 403)
                qs = parse_qs(self.path.split("?", 1)[1].split("#")[0]) if "?" in self.path else {}
                self.path = self.path.split("?")[0].split("#")[0]
                if self.path.startswith("/vendor/") and self.path[8:] in VENDOR:
                    with open(os.path.join(HERE, "ui", "vendor", self.path[8:]), "rb") as f:
                        data = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", VENDOR[self.path[8:]])
                    self.send_header("Cache-Control", "max-age=86400")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path in ("/", "/index.html", "/calibrate", "/demo", "/demo/simple", "/vr"):
                    page = {"/calibrate": CALIBRATE_FILE, "/demo": DEMO_FILE,
                            "/demo/simple": DEMO_SIMPLE_FILE, "/vr": VR_FILE}.get(self.path, UI_FILE)
                    with open(page, "rb") as f:
                        data = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path == "/api/pose":                 # page /demo : pose de tête en direct
                    ui.q.put(("cmd", "demo"))
                    out = getattr(ui.ctl, "opentrack", None)
                    st = out.status() if out else {}
                    aap = ui.aap
                    self._json({"pose": st.get("pose", [0] * 6), "age": st.get("data_age_s"),
                                "source": st.get("source_used"), "connected": bool(aap and aap.sock),
                                "rec": bool(getattr(ui, "demo_rec", False)),
                                "video": getattr(ui, "demo_last_video", None)})
                elif self.path == "/api/ping":
                    self._json({"ok": True, "instance": ui.instance_token})
                elif self.path == "/api/i18n":
                    self._json(i18n.i18n_payload(ui.ctl.cfg, i18n.env_lang()))
                elif self.path == "/api/autostart":
                    from desktop import autostart
                    saved = saved_config_value(ui.cfg_path, "autostart")
                    self._json({"enabled": bool(saved if autostart.in_flatpak() else autostart.is_enabled() or saved),
                                "flatpak": autostart.in_flatpak()})
                elif self.path == "/api/games":
                    import games_service
                    self._json(games_service.list_payload(ui.cfg_path))
                elif self.path == "/api/version":               # version, channel, release notes
                    import releases
                    seen = saved_config_value(ui.cfg_path, "whats_new_seen")
                    v = releases.info(seen=seen)
                    if seen is None and v["version"]:            # first start: announce the next update
                        m.update_config_file(ui.cfg_path, {"whats_new_seen": v["version"]})
                    self._json(v)
                elif self.path == "/api/calibration":
                    self._json(ui.point_calibration_state())
                elif self.path in ("/api/state", "/api/state?watch=gestures"):
                    g = getattr(ui.ctl, "gestures", None)
                    if g and self.path.endswith("watch=gestures"):
                        g.watch()           # stats on the Gestures page even while gestures are off
                    self._json(ui.state())
                elif self.path == "/api/gestures":
                    g = getattr(ui.ctl, "gestures", None)
                    if g is None:
                        return self._json({"error": tr("srv.error.gestures_unavailable")}, 503)
                    d = g.summary()
                    d["events"] = list(g.events)
                    d["stats"] = g.buf.stats()
                    d["config"] = ui.ctl.cfg.get("gestures", {})
                    self._json(d)
                elif self.path == "/api/labo":
                    try:
                        since = float(qs["since"][0]) if "since" in qs else None
                    except ValueError:
                        since = None
                    self._json(ui.labo_state(since))
                elif self.path == "/api/opentrack":            # --- OpenTrack
                    import opentrack_out
                    self._json(opentrack_out.api_state(ui.ctl))
                elif self.path == "/api/garmin":
                    self._json(ui.garmin_state(paired=qs.get("paired", ["0"])[0] == "1"))
                elif self.path == "/api/stream":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    try:
                        while True:
                            self.wfile.write(b"data: " + i18n.encode_json(ui.telemetry()).encode() + b"\n\n")
                            self.wfile.flush()
                            time.sleep(1 / 30)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                else:
                    self._json({"error": tr("srv.error.not_found")}, 404)

            def do_POST(self):
                origin = self.headers.get("Origin") or ""
                limited = origin in SITE_ORIGINS or origin.startswith(("chrome-extension://", "moz-extension://"))
                if origin and not limited and not origin.startswith(("http://127.0.0.1", "http://localhost")):
                    return self._json({"error": tr("srv.error.forbidden")}, 403)
                if limited and self.path.split("?")[0] not in SITE_PATHS:   # website, browser extension: pose + recenter only
                    return self._json({"error": tr("srv.error.forbidden")}, 403)
                if not self._local_host():
                    return self._json({"error": tr("srv.error.forbidden")}, 403)
                # CSRF guard: any web page can POST to 127.0.0.1. Only JSON bodies (cross-site JSON
                # needs a CORS preflight we never answer) and, when the browser sends one, our own
                # Origin are accepted.
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype != "application/json":
                    return self._json({"error": tr("srv.error.bad_request")}, 415)
                origin = self.headers.get("Origin")
                port = self.server.server_address[1]
                if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
                    return self._json({"error": tr("srv.error.forbidden")}, 403)
                n = int(self.headers.get("Content-Length", 0))
                if self.path == "/api/demo/upload":            # page /demo : vidéo enregistrée depuis le canvas
                    if not 0 < n <= 400 * 1024 * 1024:
                        return self._json({"error": "bad size"}, 400)
                    out_dir = os.path.join(HERE, "captures", "private")
                    os.makedirs(out_dir, exist_ok=True)
                    path = os.path.join(out_dir, time.strftime("cockpit_%Y%m%d-%H%M%S.webm"))
                    with open(path, "wb") as f:
                        left = n
                        while left:
                            chunk = self.rfile.read(min(left, 1 << 20))
                            if not chunk:
                                break
                            f.write(chunk); left -= len(chunk)
                    ui.demo_last_video = path
                    return self._json({"ok": True, "path": path})
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._json({"error": tr("srv.error.invalid_json")}, 400)
                if self.path == "/api/demo":                   # {"record": true|false} : la page /demo s'enregistre
                    ui.demo_rec = bool(body.get("record"))
                    if ui.demo_rec:
                        ui.demo_last_video = None
                    return self._json({"ok": True, "record": ui.demo_rec})
                if self.path == "/api/cmd":
                    if limited and body.get("cmd") != "recenter":
                        return self._json({"error": tr("srv.error.forbidden")}, 403)   # the website may only recenter
                    ui.q.put(("cmd", str(body.get("cmd", ""))))
                elif self.path == "/api/config":
                    if "ui_lang" in body and body["ui_lang"] not in i18n.SETTINGS:
                        return self._json({"error": tr("srv.error.bad_language")}, 400)
                    m.update_config_file(ui.cfg_path, body)
                    if "ui_lang" in body:              # language follows at once (full reload is queued)
                        ui.ctl.cfg["ui_lang"] = body["ui_lang"]
                        i18n.apply_config(ui.ctl.cfg)
                    ui.q.put(("cmd", "reload"))
                elif self.path == "/api/calib":
                    ui.q.put(("calib", body.get("action", "start"), body.get("source", "auto"), body.get("flow")))
                elif self.path == "/api/sensor":
                    ui.q.put(("sensor", int(body["dev"]), body.get("action", "get"),
                              int(body.get("interval", 10000)), int(body.get("report", 1))))
                elif self.path == "/api/control":
                    ui.q.put(("control", int(body["ident"]), body.get("values", [])))
                elif self.path == "/api/raw":
                    ui.q.put(("raw", str(body.get("hex", ""))))
                elif self.path == "/api/reconnect":
                    ui.q.put(("reconnect",))
                elif self.path == "/api/record":
                    secs = float(body.get("seconds", 10))
                    path = os.path.join(paths.captures_dir(), os.path.basename(str(body.get("name", f"record_{int(time.time())}"))) + ".json")
                    now = time.monotonic()
                    ui.rec = {"t0": now, "until": now + secs, "path": path, "rows": []}
                    return self._json({"ok": True, "path": path})
                elif self.path == "/api/gestures":
                    a = body.get("action")
                    if a in ("enable", "disable"):
                        m.update_config_file(ui.cfg_path, {"gestures": {"enabled": a == "enable"}})
                        ui.q.put(("cmd", "reload"))
                    elif a == "set":                    # seuils : {"values": {"click_thresh": 1.2, ...}}
                        vals = {k: v for k, v in dict(body.get("values", {})).items()
                                if k in m.DEFAULTS["gestures"] and k != "mapping"}
                        m.update_config_file(ui.cfg_path, {"gestures": vals})
                        ui.q.put(("cmd", "reload"))
                    elif a == "mapping":                # {"mapping": {"nod": "left_click", ...}}
                        mp = dict(ui.ctl.cfg.get("gestures", {}).get("mapping", {}))
                        mp.update({str(k): str(v) for k, v in dict(body.get("mapping", {})).items()})
                        m.update_config_file(ui.cfg_path, {"gestures": {"mapping": mp}})
                        ui.q.put(("cmd", "reload"))
                    else:
                        ui.q.put(("gest", body))
                elif self.path == "/api/speech":
                    ui.q.put(("speech", body))
                elif self.path == "/api/labo":
                    if ui.labo_tap is None:
                        return self._json({"error": tr("srv.error.labo_unavailable")}, 503)
                    a = body.get("action")
                    try:
                        if a == "start":
                            return self._json(ui.labo_start(body))
                        if a == "cancel":
                            ui.labo_cancel()
                    except ValueError as e:
                        return self._json({"error": str(e)}, 400)
                elif self.path == "/api/phase":
                    if ui.rec:
                        ui.rec["phase"] = str(body.get("phase", ""))
                elif self.path == "/api/opentrack":            # --- OpenTrack + manette virtuelle
                    import opentrack_out
                    import sim_joystick
                    if body.get("action") == "recenter":
                        ui.q.put(("cmd", "recenter"))
                        return self._json({"ok": True})
                    upd = opentrack_out.sanitize({k: v for k, v in body.items() if k != "joystick"})
                    jupd = sim_joystick.sanitize(body.get("joystick") or {})
                    if upd.get("enabled") is False:
                        ui.ctl.opentrack_forced = False      # coupe aussi un --opentrack de session
                    if jupd.get("enabled") is False:
                        ui.ctl.joystick_forced = False       # … et un --sim-joystick
                    changes = {**({"opentrack": upd} if upd else {}), **({"sim_joystick": jupd} if jupd else {})}
                    if changes:
                        m.update_config_file(ui.cfg_path, changes)
                        ui.q.put(("cmd", "reload"))
                elif self.path == "/api/garmin":
                    try:
                        return self._json(ui.garmin_action(body))
                    except ValueError as e:
                        return self._json({"error": str(e)}, 400)
                    except RuntimeError as e:
                        return self._json({"error": str(e)}, 503)
                elif self.path == "/api/wizard":
                    done = bool(body.get("done"))
                    m.update_config_file(ui.cfg_path, {"wizard_done": done})
                    ui.ctl.cfg["wizard_done"] = done
                    return self._json({"ok": True})
                elif self.path == "/api/autostart":
                    from desktop import autostart
                    on = bool(body.get("enabled"))
                    try:
                        autostart.set_enabled(on)
                    except Exception as e:  # noqa: BLE001  (portal / filesystem error -> report)
                        return self._json({"error": tr("srv.error.autostart", error=e)}, 500)
                    m.update_config_file(ui.cfg_path, {"autostart": on})
                    return self._json({"ok": True, "enabled": on})
                elif self.path.startswith("/api/games/"):
                    return self._games_post(self.path[len("/api/games/"):], body)
                elif self.path == "/api/version":               # "what's new" read
                    seen = body.get("seen")
                    if not isinstance(seen, str) or not 0 < len(seen) <= 32:
                        return self._json({"error": tr("srv.error.bad_request")}, 400)
                    m.update_config_file(ui.cfg_path, {"whats_new_seen": seen})
                    return self._json({"ok": True})
                elif self.path == "/api/calibration":
                    action = body.get("action")
                    if action == "start":
                        ui.start_point_calibration()
                    elif action == "cancel":
                        ui.cancel_point_calibration()
                    elif action == "save_baseline":
                        pointer = ui.ctl.save_baseline(body)
                        if pointer is None:
                            return self._json({"error": tr("srv.error.bad_request")}, 400)
                        ui.q.put(("cmd", "reload"))                # main loop applies it
                        return self._json({"ok": True, "pointer": pointer})
                    else:
                        return self._json({"error": tr("srv.error.not_found")}, 400)
                    return self._json(ui.point_calibration_state())
                else:
                    return self._json({"error": tr("srv.error.not_found")}, 404)
                self._json({"ok": True})

            def _games_post(self, appid, body):
                import bridge_install
                import games_service
                game = games_service.find(appid, ui.cfg_path)
                if game is None:
                    return self._json({"error": tr("srv.error.not_found")}, 404)
                try:
                    if "enabled" in body:
                        entry = games_service.set_enabled(ui.cfg_path, game, bool(body["enabled"]))
                    elif isinstance(body.get("profile"), dict):
                        entry = games_service.set_profile(ui.cfg_path, game, body["profile"])
                    elif body.get("steam") in ("native", "flatpak"):      # game in both Steams
                        entry = games_service.set_steam(ui.cfg_path, game, body["steam"] == "flatpak")
                    else:
                        return self._json({"error": tr("srv.error.bad_request")}, 400)
                except bridge_install.BridgeError as e:
                    return self._json({"error": tr("games.error." + e.code), "code": e.code}, 409)
                except OSError as e:
                    return self._json({"error": tr("games.error.io_error", error=e), "code": "io_error"}, 500)
                m.update_config_file(ui.cfg_path, {"games": {game.appid: entry}})
                ui.q.put(("cmd", "reload"))          # the controller re-syncs the game bridge
                return self._json({"ok": True, "game": entry})

        self.instance_token = write_instance_token()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        if self.garmin:
            self.garmin.close()
        if self.server:
            self.server.shutdown()
