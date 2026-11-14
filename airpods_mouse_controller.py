#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Matteo Gaudioso (starman-tech) — https://github.com/starman-tech/mousepods
"""
airpods_mouse_controller.py — AirPods -> souris / pointeur (Linux, Wayland ou X11).

  * AirPods 3        : protocole Apple RemoteHID (HID sur AACP, RTBuddy 0x800), jusqu'à ~200 Hz.
  * AirPods Pro 2/3/4: protocole SensorDataWX (RTBuddy 0x100000).
  * Mouvement : gyroscope + gravité des écouteurs -> lacet autour de la VERTICALE, tangage autour
    d'un axe latéral appris en calibration. Marche quelle que soit l'orientation :
    écouteurs portés (tête), écouteurs dans le boîtier ouvert, ou un AirPod tenu en main.
  * Modes : "souris" (la vitesse du geste déplace le curseur, avec accélération)
            "pointeur" (la direction visée = position à l'écran, comme un laser).
  * Injection : /dev/uinput (python-evdev). Réglages rechargés à chaud depuis config.json.

Usage :
  airpods_mouse_controller.py                  lance le contrôleur
  airpods_mouse_controller.py --settings       panneau de réglages (curseurs en direct)
  airpods_mouse_controller.py --calibrate      calibration à l'écran (tête / boîtier / main)
  airpods_mouse_controller.py --diag           affiche les vitesses/angles en direct
  airpods_mouse_controller.py --opentrack      + suivi de tête vers OpenTrack (UDP 127.0.0.1:4242)
  airpods_mouse_controller.py --sim-joystick   + manette virtuelle « tête » (jeux Linux natifs), voir docs/SIMS.md
  airpods_mouse_controller.py --send CMD       recenter|toggle|pause|resume|scroll|mode|quit
  airpods_mouse_controller.py --install-hotkeys  raccourcis GNOME (Ctrl+Alt+C/P/S/M)
"""
import argparse
import json
import logging
import math
import os
import re
import select
import shutil
import socket
import struct
import subprocess
import tempfile
import sys
import threading
import time

import bluez_dbus
import l2cap
import i18n
from i18n import tr

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(HERE, "config.json")
CTRL_SOCK = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "airpods-mouse.sock")
WINDOWS = sys.platform == "win32"
CTRL_UDP = ("127.0.0.1", 8736)     # Windows has no AF_UNIX datagram sockets: --send uses loopback UDP there
log =logging.getLogger("airpods-mouse")

# =========================================================================== configuration
DEFAULTS = {
    "device_address": "auto",
    "ui_lang": "auto",             # "auto" (system language) | "en" | "de" | "fr"
    "wizard_done": False,          # first-run wizard in the web UI completed
    "mouse_paused": True,          # mouse control starts off; Pause/Resume is remembered
    "games": {},                   # Steam appid -> {"enabled": bool, "profile": {...}} (game bridge)
    "game_baseline": None,         # Calibration page: edges for every game without own values
    "auto_connect": True,
    "mode": "souris",              # "souris" | "pointeur" | "glisse"
    "source": "auto",              # "auto" | "head" | "case" | "hand"
    "sensitivity_x": 1.0,
    "sensitivity_y": 1.0,
    "invert_x": False,
    "invert_y": False,
    "smoothing": 0.35,             # 0 = brut, 1 = très lisse
    "render_hz": 120,
    "render_lag_s": 0.025,
    "sensor_interval_us": 10000,   # 10000 = 100 Hz (moins de coupures Bluetooth que 200 Hz)
    "gyro_dps_per_lsb": 0.122,
    "souris": {"px_per_deg": 20.0, "acceleration": 1.0, "rate_deadzone_dps": 2.5},
    "pointeur": {"screen_pct_per_deg": 3.0, "rate_deadzone_dps": 0.6},
    "glisse": {"px_per_cm": 40.0, "accel_deadzone": 0.2, "still_acc": 0.4, "still_dps": 40.0,
               "still_time_s": 0.12, "leak_s": 3.0},
    "bias": {"still_range_dps": 3.0, "still_time_s": 0.4, "rate": 0.03},
    "stem": {"enabled": True, "single": "left_click", "double": "right_click",
             "triple": "toggle_scroll", "long": "toggle_pause"},
    "roll_click": {"enabled": False, "trigger_deg": 15.0, "max_duration_s": 0.6,
                   "right": "left_click", "left": "right_click"},
    "dwell_click": {"enabled": False, "time_s": 1.0, "radius_px": 12, "min_move_px": 60},
    "scroll": {"lines_per_deg": 0.25},
    # gestes tête / mâchoire (gestures.py). Tous les seuils ici, réglables à chaud depuis l'interface.
    "gestures": {
        "enabled": False,
        "require_activation": True,   # double clic des dents -> fenêtre d'action
        "activation": "teeth_double",
        "window_s": 2.0,
        "freeze_cursor_when_armed": True,   # curseur figé pendant la fenêtre d'action
        "freeze_after_s": 0.6,              # … et encore 0,6 s après l'action
        "max_interp_gap_s": 0.03,     # trou capteur plus court : interpolé ; plus long : segment invalide
        # source préférée : accéléro + gyro HID bruts (~201 Hz, horodatage capteur µs) ; devmotion6 sinon.
        # 3 flux saturent le lien Bluetooth (devmotion6 tombe à ~43 Hz irrégulier) : tant que les gestes
        # sont actifs, devmotion6 (souris) passe à dm6_interval_with_raw_us (0 = inchangé).
        "use_raw_imu": True, "raw_interval_us": 5000, "dm6_interval_with_raw_us": 20000,
        # claquement des dents (pic d'accélération passe-haut)
        "click_hp_hz": 8.0, "click_thresh": 0.45, "click_low_frac": 0.5, "click_max_ms": 120,
        "click_refractory_ms": 120, "click_max_gyro_dps": 20.0, "click_gyro_win_s": 0.15,
        "click_quiet_s": 0.5,
        # seuil adaptatif : seuil effectif = max(click_thresh, k × bruit de fond) ; bruit = centile pct de
        # |acc passe-haut| sur win_s, tête quasi immobile (< still_dps). Monte si l'on parle / mâche.
        "click_adaptive": True, "click_noise_k": 4.0, "click_noise_pct": 0.9, "click_noise_win_s": 2.0,
        "click_noise_still_dps": 30.0,
        # écho : impulsion < frac × pic précédent dans les echo_s qui suivent un claquement (relâchement
        # de la mâchoire, ~0,2–0,4 s après) -> ignorée ; impulsions de mastication : pic >= chew_impulse_min
        "click_echo_s": 0.45, "click_echo_frac": 0.3, "chew_impulse_min": 0.3,
        "click_end_frac": 0.25,       # fin d'un pic : sous max(lo/2, frac × pic) (durée indépendante du seuil)
        "double_min_s": 0.08, "double_max_s": 0.45,
        "click_side_axis": "",        # "" = côté inconnu ; ex. "x" (x>0 = gauche) ou "-x" après essais
        # mâchoire (expérimental)
        "clench_enabled": False, "clench_rms": 0.12, "clench_min_s": 0.6,
        "slide_enabled": False,       # pas de règle plausible : jamais détecté
        # tête (vitesses lacet/tangage/roulis calibrées, °/s et degrés)
        "lobe_dps": 40.0, "nod_deg": 8.0, "shake_deg": 10.0, "nod_lobes": 2, "shake_lobes": 3,
        "osc_max_s": 2.0, "lobe_gap_s": 0.4, "dominance": 2.0,
        "tilt_deg": 15.0, "tilt_hold_s": 0.25, "cross_axis_ratio": 1.3, "look_deg": 15.0, "look_hold_s": 0.5,
        "hold_max_dps": 30.0, "refractory_s": 0.4, "base_tau_s": 8.0,
        # rejet
        "big_motion_dps": 800.0, "big_motion_deg": 90.0, "big_motion_hold_s": 0.5,
        "walk_rms": 1.0, "walk_ratio": 0.35, "walk_window_s": 3.0, "walk_hold_s": 1.5,
        "chew_count": 4, "chew_window_s": 3.0, "chew_hold_s": 2.0, "chew_merge_s": 0.35,
        "calib_reps": 10, "calib_frac": 0.6,
        "mapping": {"teeth_click": "left_click", "nod": "left_click", "shake": "right_click",
                    "tilt_left": "none", "tilt_right": "none", "look_up": "none", "look_down": "none",
                    "jaw_clench": "none", "jaw_slide_left": "none", "jaw_slide_right": "none"},
    },
    "notifications": True,
    # --- OpenTrack (suivi de tête pour simulateurs, voir opentrack_out.py et docs/OPENTRACK.md)
    # --- other headsets: Android's standard head-tracker HID (Sony XM5/XM6, Pixel Buds Pro…), see hid_headtracker.py
    "headsets": {"enabled": True, "mouse": True, "px_per_deg": 22.0},
    "opentrack": {"enabled": False, "host": "127.0.0.1", "port": 4242, "rate_hz": 120,
                  "invert_yaw": False, "invert_pitch": False, "invert_roll": False,
                  "scale_yaw": 1.0, "scale_pitch": 1.0, "scale_roll": 1.0, "smoothing": 0.2,
                  "pause_mouse_when_opentrack": True, "drift_correction": True, "drift_tau_s": 2.0,
                  "yaw_autocenter_s": 0.0, "lean_experimental": False, "source": "quaternion"},
    # --- manette virtuelle « tête » (sim_joystick.py) : mêmes angles que la sortie OpenTrack
    "sim_joystick": {"enabled": False, "full_yaw_deg": 180.0, "full_pitch_deg": 90.0, "full_roll_deg": 180.0,
                     "full_cm": 100.0},
    "profiles": {},
}
TUNABLE_SECTIONS = ("souris", "pointeur", "glisse", "bias", "stem", "roll_click", "dwell_click", "scroll")
DEFAULT_PROFILE = {"yaw_sign": 1, "lat": [0.0, 0.0, 1.0], "pitch_sign": 1, "roll_sign": 1}
SOURCE_NAMES = {"head": "calib.source.head", "case": "calib.source.case", "hand": "calib.source.hand"}  # keys


def deep_merge(base, over):
    for k, v in over.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(base.get(k), dict) and k != "profiles":
            base[k].update(v)
        else:
            base[k] = v
    return base


def load_config(path):
    cfg = json.loads(json.dumps(DEFAULTS))
    if os.path.exists(path):
        with open(path) as f:
            deep_merge(cfg, json.load(f))
    if cfg["mode"] in ("absolute",):
        cfg["mode"] = "pointeur"
    elif cfg["mode"] in ("relative",):
        cfg["mode"] = "souris"
    return cfg


_CONFIG_LOCK = threading.Lock()     # web request threads and the controller write config.json


def update_config_file(path, updates):
    """Fusionne `updates` dans config.json (préserve les autres clés et l'aide).
    Read-merge-write under one lock, through a private temp file, so concurrent writers keep
    each other's updates and never leave a half-written file."""
    with _CONFIG_LOCK:
        user = {}
        if os.path.exists(path):
            with open(path) as f:
                user = json.load(f)
        for k, v in updates.items():
            if isinstance(v, dict) and isinstance(user.get(k), dict):
                user[k].update(v)
            else:
                user[k] = v
        fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=os.path.dirname(os.path.abspath(path)))
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(user, f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.replace(tmp, path)
        except BaseException:
            try:
                os.remove(tmp)
            except FileNotFoundError:
                pass
            raise


# =========================================================================== Bluetooth
BLUETOOTHCTL_MISSING = ("bluetoothctl introuvable / not found: installe le paquet bluez "
                        "(Fedora, distrobox : sudo dnf install bluez ; Debian/Ubuntu : sudo apt install bluez), "
                        "ou mets l'adresse des AirPods dans device_address")


def have_bluetoothctl():
    return shutil.which("bluetoothctl") is not None


class BluetoothToolMissing(OSError):
    """No way to list/connect paired devices: no bluetoothctl and no BlueZ D-Bus (errno 112 as upstream)."""

    def __init__(self):
        super().__init__(112, BLUETOOTHCTL_MISSING)


def bt_info(addr):
    try:
        return subprocess.run(["bluetoothctl", "info", addr], capture_output=True, text=True,
                              timeout=5).stdout
    except subprocess.TimeoutExpired:
        return ""
    except FileNotFoundError:
        raise BluetoothToolMissing() from None


def apple_audio_devices():
    if bluez_dbus.available():
        return bluez_dbus.list_apple_devices()
    out = []
    try:
        lines = subprocess.run(["bluetoothctl", "devices", "Paired"], capture_output=True,
                               text=True, timeout=5).stdout.splitlines()
    except subprocess.TimeoutExpired:
        return out
    except FileNotFoundError:
        raise BluetoothToolMissing() from None
    for ln in lines:
        m = re.match(r"Device ([0-9A-F:]{17}) (.*)", ln.strip())
        if not m:
            continue
        info = bt_info(m.group(1))
        mod = re.search(r"Modalias: bluetooth:v004Cp([0-9A-F]{4})", info)
        if mod:
            out.append((m.group(1), m.group(2), mod.group(1).lower(), "Connected: yes" in info))
    return out


def bt_connected(addr):
    if bluez_dbus.available():
        got = bluez_dbus.info(addr)
        return bool(got and got["connected"])
    return "Connected: yes" in bt_info(addr)


def bt_connect(addr, timeout):
    """Ask BlueZ to connect `addr` (D-Bus if available, else bluetoothctl)."""
    if bluez_dbus.available():
        return bluez_dbus.connect(addr, timeout)
    subprocess.run(["bluetoothctl", "connect", addr], capture_output=True, text=True, timeout=timeout)
    return bt_connected(addr)


def bt_disconnect(addr):
    if bluez_dbus.available():
        bluez_dbus.disconnect(addr)
    else:
        subprocess.run(["bluetoothctl", "disconnect", addr], capture_output=True, timeout=10)


def product_id(addr):
    if bluez_dbus.available():
        got = bluez_dbus.info(addr)
        return got["pid"] if got else "0000"
    try:
        info = bt_info(addr)
    except BluetoothToolMissing:
        return "0000"   # model unknown, but a fixed address can still connect
    m = re.search(r"Modalias: bluetooth:v004Cp([0-9A-F]{4})", info)
    return m.group(1).lower() if m else "0000"


# =========================================================================== AAP / RTBuddy
H = bytes.fromhex
HDR = b"\x04\x00\x04\x00"
PKT_HANDSHAKE = H("00000400010002000000000000000000")
PKT_FEATURES = H("040004004d00d700000000000000")
PKT_NOTIFICATIONS = H("040004000f00ffffffff")
WX_PREFIX = H("04000400170000001000")
SVC_ACTIVITY, SVC_DEVMOTION6 = 0x0E, 0x10
SOL_BLUETOOTH, BT_SECURITY, BT_SECURITY_MEDIUM = 274, 4, 2
SOL_L2CAP, L2CAP_OPTIONS = 6, 1
IOS_MTU = 2582   # avec le MTU Linux par défaut (672) les AirPods n'annoncent pas leurs capteurs


def ctrl_cmd(ident, d1=0, d2=0, d3=0, d4=0):
    return HDR + bytes([0x09, 0x00, ident, d1, d2, d3, d4])


PKT_STEM_EVENTS_ON = ctrl_cmd(0x39, 0x0F)
PKT_STEM_EVENTS_OFF = ctrl_cmd(0x39, 0x00)
STEM_TYPES = {0x05: "single", 0x06: "double", 0x07: "triple", 0x08: "long"}

# --- Apple RemoteHID (HID sur AACP, descripteur RTBuddy 0x800)
# corps : [version=0][génération][0][côté*2] puis sous-paquets ; chaque sous-paquet commence par
# un u32 LE : bits 0-6 deviceID, 7-16 longueur (en-tête inclus), 17-19 type, 21 réponse.
# types : 0 Connect (HostConnect si vide / DeviceConnect = plist), 1 Disconnect, 2 InputReport,
#         3 SetReport, 4 GetReport, 5 DeviceConfirmConnect.
RHID_PREFIX = H("04000400170000080000")
RHID_CONNECT, RHID_INPUT, RHID_SET, RHID_GET = 0, 2, 3, 4
HID_FEATURE = 2


def rhid_frame(gen, subpackets):
    body = bytes([0, gen & 0xFF, 0, 0]) + b"".join(subpackets)
    return RHID_PREFIX + struct.pack("<H", len(body)) + body


def rhid_sub(dev, kind, payload=b""):
    n = 4 + len(payload)
    return struct.pack("<I", (dev & 0x7F) | ((n & 0x3FF) << 7) | ((kind & 7) << 17)) + payload


def rhid_parse(pkt):
    """[(kind, dev, payload)] d'une trame RemoteHID."""
    out = []
    if len(pkt) < 16 or pkt[:10] != RHID_PREFIX:
        return out
    body = pkt[12:12 + struct.unpack_from("<H", pkt, 10)[0]]
    i = 4
    while i + 4 <= len(body):
        h = struct.unpack_from("<I", body, i)[0]
        n = (h >> 7) & 0x3FF
        if n < 4:
            break
        out.append(((h >> 17) & 7, h & 0x7F, body[i + 4:i + n]))
        i += n
    return out


def _varint(b, i):
    v = shift = 0
    while i < len(b):
        c = b[i]
        i += 1
        v |= (c & 0x7F) << shift
        if not c & 0x80:
            return v, i
        shift += 7
    raise ValueError("varint")


def pb_fields(buf):
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(buf, i)
        elif wt == 2:
            n, i = _varint(buf, i)
            v = buf[i:i + n]
            i += n
        elif wt == 5:
            v, i = buf[i:i + 4], i + 4
        elif wt == 1:
            v, i = buf[i:i + 8], i + 8
        else:
            return
        yield f, v


def motion_blocks(pkt):
    """Blocs capteur (58 o) d'un paquet SensorDataWX (souvent plusieurs par paquet)."""
    if len(pkt) < 12 or pkt[:10] != WX_PREFIX:
        return []
    n = struct.unpack_from("<H", pkt, 10)[0]
    out = []
    try:
        for f, v in pb_fields(pkt[12:12 + n]):
            if f == 7 and isinstance(v, (bytes, bytearray)):
                svc, blk = None, None
                for f2, v2 in pb_fields(v):
                    if f2 == 1:
                        svc = v2
                    elif f2 == 3:
                        blk = v2
                if blk is not None and len(blk) >= 50 and svc in (SVC_ACTIVITY, SVC_DEVMOTION6, 15):
                    out.append(blk)
    except ValueError:
        pass
    return out


def ear_place(pkt):
    """Paquet 0x06 -> 'ear' | 'case' | 'out' (0 = oreille, 1 = dehors, 2 = boîtier)."""
    a, b = pkt[6], pkt[7]
    if 0 in (a, b):
        return "ear"
    if 1 in (a, b):          # un écouteur sorti (l'autre peut rester dans le boîtier) -> en main
        return "out"
    if 2 in (a, b):
        return "case"
    return "out"


class NoAirPodsConnected(OSError):
    """No paired AirPods are connected (errno 112, as before)."""

    def __init__(self):
        super().__init__(112, "no AirPods connected")


def connect_failure_status(error):
    """Status text for the UI after a failed connection attempt."""
    if isinstance(error, BluetoothToolMissing):
        return i18n.tr_lazy("srv.status.bluetoothctl_missing")
    if isinstance(error, NoAirPodsConnected):
        return i18n.tr_lazy("srv.status.no_airpods")
    return i18n.tr_lazy("srv.status.searching", error=error)


class AAPClient:
    def __init__(self, addr="auto", auto_connect=True, interval_us=10000):
        self.want = addr
        self.addr = None if addr == "auto" else addr
        self.auto_connect = auto_connect
        self.interval_us = int(interval_us)
        self.sock = None
        self.seq = 0x40
        self.gen = 0
        self.pid = "0000"
        self.rhid_devs = {}
        self.rhid_motion = None
        self.place = None
        self.stem_events = []
        # observabilité (interface)
        self.rhid_plist = {}         # dev -> plist XML (DeviceConnect)
        self.rhid_last = {}          # dev -> (temps, payload) dernier InputReport
        self.rhid_counts = {}        # dev -> nombre d'InputReport
        self.rhid_replies = []       # [(temps, kind, dev, payload)] réponses Get/SetReport
        self.rhid_active = {}        # dev -> intervalle des flux démarrés à la main (relancés après réannonce)
        self.imu_kinds = {}          # dev -> "a" | "w" : flux IMU bruts demandés par les gestes
        self.imu_reports = []        # [(kind, payload)] en attente pour Controller.raw_imu_hooks
        self.on_raw = None           # callback(direction, paquet)

    def _pick(self):
        if self.want != "auto":
            return self.want
        if not bluez_dbus.available() and not have_bluetoothctl():
            # without either we cannot list paired devices: say so instead of "no AirPods connected"
            raise BluetoothToolMissing()
        if self.addr and bt_connected(self.addr):   # reconnexion : déjà connu
            return self.addr
        devs = apple_audio_devices()
        con = [d for d in devs if d[3]]
        if con:
            return con[0][0]
        if self.auto_connect:
            for d in devs:
                log.info("Tentative de connexion : %s (%s)", d[1], d[0])
                try:
                    if bt_connect(d[0], 12):
                        return d[0]
                except subprocess.TimeoutExpired:
                    continue
        raise NoAirPodsConnected()

    def connect(self):
        if WINDOWS:                      # native Bluetooth stack + the windows/aap-poc driver, see aap_windows.py
            import aap_windows
            s, self.addr, self.pid = aap_windows.connect(self.want)
            self._hello(s)
            return
        self.addr = self._pick()
        if self.auto_connect and (bluez_dbus.available() or have_bluetoothctl()) and not bt_connected(self.addr):
            try:
                bt_connect(self.addr, 20)
            except subprocess.TimeoutExpired:
                pass
        self.pid = product_id(self.addr)
        s = l2cap.open_seqpacket(self.addr, 0x1001, IOS_MTU, 8)   # ctypes path if Python lacks AF_BLUETOOTH
        s.settimeout(None)
        self._hello(s)

    def _hello(self, s):
        self.sock = s
        for p, d in ((PKT_HANDSHAKE, 0.3), (PKT_FEATURES, 0.15), (PKT_NOTIFICATIONS, 0.15)):
            s.send(p)
            time.sleep(d)
        self.rhid_devs, self.rhid_motion = {}, None
        self.rhid_host_connect()
        log.info("Canal AAP ouvert avec %s (modèle %s)", self.addr, self.pid)

    def send(self, pkt):
        if self.sock:
            if self.on_raw:
                self.on_raw("tx", pkt)
            try:
                self.sock.send(pkt)
            except OSError as e:
                log.warning("Envoi AAP impossible : %s", e)

    # ---- SensorDataWX (AirPods Pro / 4)
    def stream(self, svc, interval_us):
        self.seq = (self.seq + 1) & 0x7F
        body = bytes([0x08, self.seq, 0x42, 0x0B, 0x08, svc, 0x10, 0x02, 0x1A, 0x05, 0x01]) + \
            struct.pack("<I", interval_us)
        self.send(WX_PREFIX + struct.pack("<H", len(body)) + body)

    # ---- RemoteHID (AirPods 3)
    def _rhid_send(self, subs):
        self.gen = (self.gen + 1) & 0xFF
        self.send(rhid_frame(self.gen, subs))

    def rhid_host_connect(self):
        self._rhid_send([rhid_sub(0, RHID_CONNECT)])

    def rhid_set_feature(self, dev, report_id, value):
        self._rhid_send([rhid_sub(dev, RHID_SET, bytes([HID_FEATURE, report_id]) +
                                  struct.pack("<I", value))])

    def rhid_start(self):
        dev = self.rhid_devs.get("devmotion6")
        if dev is not None:
            self.rhid_set_feature(dev, 1, self.interval_us)   # feature 1 = Report Interval (µs)
            self.rhid_motion = dev

    def start_tracking(self):
        if self.pid == "2013" and not self.rhid_devs:   # AirPods 3 : RemoteHID uniquement
            self.rhid_host_connect()
            return
        if self.rhid_devs:
            if "devmotion6" in self.rhid_devs:
                self.rhid_start()
            else:
                self.rhid_host_connect()
            return
        self.stream(SVC_ACTIVITY, 40000)
        time.sleep(0.25)
        self.stream(SVC_DEVMOTION6, self.interval_us)
        self.stream(SVC_ACTIVITY, 0)
        self.rhid_host_connect()

    def process(self, pkt):
        """Paquet AAP -> liste de blocs capteur de 58 o ; met à jour place / stem_events."""
        if pkt[:10] == WX_PREFIX:
            return motion_blocks(pkt)
        if pkt[:10] == RHID_PREFIX:
            blocks = []
            for kind, dev, pl in rhid_parse(pkt):
                if kind == RHID_CONNECT and b"<?xml" in pl:
                    m = re.search(rb"AccessoryService</key><string>(\w+)", pl)
                    self.rhid_plist[dev] = pl
                    if m:
                        name = m.group(1).decode()
                        new = name not in self.rhid_devs
                        self.rhid_devs[name] = dev
                        # une réannonce (ex. changement d'écouteur principal) annule les flux : on relance
                        if name == "devmotion6":
                            log.info("Capteur devmotion6 (RemoteHID id %d) %s — démarrage", dev,
                                     "annoncé" if new else "réannoncé")
                            self.rhid_start()
                        if dev in self.rhid_active:
                            self.rhid_set_feature(dev, 1, self.rhid_active[dev])
                elif kind == RHID_INPUT:
                    self.rhid_counts[dev] = self.rhid_counts.get(dev, 0) + 1
                    self.rhid_last[dev] = (time.monotonic(), pl)
                    if dev == self.rhid_motion and len(pl) >= 51:
                        blocks.append(pl[1:])      # pl[0] = type de rapport
                    elif dev in self.imu_kinds:
                        self.imu_reports.append((self.imu_kinds[dev], pl))
                        del self.imu_reports[:-400]
                elif kind in (RHID_SET, RHID_GET):
                    self.rhid_replies.append((time.monotonic(), kind, dev, pl))
                    del self.rhid_replies[:-50]
            return blocks
        if pkt[:4] == HDR and len(pkt) >= 8:
            if pkt[4] == 0x06:
                self.place = ear_place(pkt)
            elif pkt[4] == 0x19:
                self.stem_events.append(STEM_TYPES.get(pkt[6], "0x%02x" % pkt[6]))
        return []

    def close(self, stem_restore=False):
        if self.sock:
            try:
                if self.rhid_motion is not None:
                    self.rhid_set_feature(self.rhid_motion, 1, 0)
                self.stream(SVC_DEVMOTION6, 0)
                if stem_restore:
                    self.send(PKT_STEM_EVENTS_OFF)
                time.sleep(0.05)
                self.sock.close()
            except OSError:
                pass
        self.sock = None


# =========================================================================== mesures & maths
def vdot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def vcross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def vnorm(a):
    n = math.sqrt(vdot(a, a))
    return (a[0] / n, a[1] / n, a[2] / n) if n > 1e-9 else tuple(a)


def vlen(a):
    return math.sqrt(vdot(a, a))


def vsub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def vscale(a, k):
    return (a[0] * k, a[1] * k, a[2] * k)


def reject(v, axis):
    """Composante de v perpendiculaire à axis (unitaire)."""
    return vsub(v, vscale(axis, vdot(v, axis)))


def parse_block(blk, dps_per_lsb=0.122):
    """Bloc 58 o -> (horodatage ns, vitesse angulaire °/s (corps), gravité (corps, ~1000 = 1 g))."""
    ts = struct.unpack_from("<Q", blk, 1)[0]
    w = struct.unpack_from("<hhh", blk, 26)
    g = struct.unpack_from("<hhh", blk, 44)
    return ts, (w[0] * dps_per_lsb, w[1] * dps_per_lsb, w[2] * dps_per_lsb), (float(g[0]), float(g[1]), float(g[2]))


def parse_accel(blk):
    """Accélération utilisateur (gravité retirée), repère corps, en m/s² (~1000 unités = 1 g)."""
    a = struct.unpack_from("<hhh", blk, 38)
    k = 9.81 / 1000.0
    return (a[0] * k, a[1] * k, a[2] * k)


def quat_from_att(att):
    """Partie vectorielle Q15 [20:26] -> quaternion unitaire (w, x, y, z), ou None."""
    x, y, z = (c / 32768.0 for c in att)
    n2 = x * x + y * y + z * z
    if not 0.01 < n2 < 1.01:
        return None
    return (math.sqrt(max(0.0, 1.0 - n2)), x, y, z)


def qrot(q, v):
    """Applique la rotation q (corps -> monde) au vecteur v."""
    w, x, y, z = q
    vx, vy, vz = v
    return (vx * (1 - 2 * (y * y + z * z)) + vy * 2 * (x * y - w * z) + vz * 2 * (x * z + w * y),
            vx * 2 * (x * y + w * z) + vy * (1 - 2 * (x * x + z * z)) + vz * 2 * (y * z - w * x),
            vx * 2 * (x * z - w * y) + vy * 2 * (y * z + w * x) + vz * (1 - 2 * (x * x + y * y)))


class GlideEngine:
    """Mode « glisse » : l'appareil posé à plat est poussé sur la table comme une souris.

    * L'accélération (gravité retirée) est ramenée dans le repère de la TABLE grâce au quaternion
      d'attitude des AirPods : une rotation de l'écouteur pendant le geste ne fausse plus la direction.
    * Les directions « droite / avant » sont figées au début de chaque geste.
    * La vitesse est remise à zéro dès l'arrêt (ZUPT) ; la vitesse résiduelle à ce moment est l'erreur
      accumulée pendant le geste : on rattrape l'écart de position (correction rétroactive).
    """

    def __init__(self, gcfg, profile=None):
        self.c = gcfg
        self.set_profile(profile or {})
        self.v = [0.0, 0.0]
        self.still_t = 0.0
        self.up = None
        self.a_f = (0.0, 0.0, 0.0)
        self.in_stroke = False
        self.stroke_t = 0.0
        self.axes_w = None
        self.pending = [0.0, 0.0]          # correction de position (m) à appliquer par le contrôleur
        self.strokes = 0

    def set_profile(self, p):
        self.right = vnorm(tuple(p.get("right", (1.0, 0.0, 0.0))))
        self.fwd = vnorm(tuple(p.get("fwd", (0.0, 1.0, 0.0))))

    def _axes(self, q, up):
        if q is not None:
            r = reject(qrot(q, self.right), up)
            f = reject(qrot(q, self.fwd), up)
        else:
            r, f = reject(self.right, up), reject(self.fwd, up)
        r = vnorm(r)
        return r, vnorm(reject(f, r))

    def step(self, wc, g, a, dt, q=None):
        if vlen(g) > 1e-3:
            gn = vnorm(qrot(q, g) if q is not None else g)
            self.up = gn if self.up is None else vnorm(tuple(x + 0.1 * (y - x) for x, y in zip(self.up, gn)))
        if self.up is None:
            return 0.0, 0.0
        aw = qrot(q, a) if q is not None else a
        k = 1.0 - math.exp(-dt / 0.03)                 # lissage ~30 ms du bruit (~0,03 g)
        self.a_f = tuple(x + k * (y - x) for x, y in zip(self.a_f, aw))
        ah = reject(self.a_f, self.up)
        moving = not (vlen(wc) < self.c["still_dps"] and vlen(ah) < self.c["still_acc"])
        self.still_t = 0.0 if moving else self.still_t + dt
        if self.still_t >= self.c["still_time_s"]:
            if self.in_stroke:                        # fin de geste : correction rétroactive
                self.pending[0] -= self.v[0] * self.stroke_t / 2
                self.pending[1] -= self.v[1] * self.stroke_t / 2
                self.strokes += 1
            self.in_stroke = False
            self.v = [0.0, 0.0]
            self.axes_w = self._axes(q, self.up)      # directions mises à jour à l'arrêt seulement
            return 0.0, 0.0
        if not self.in_stroke:
            self.in_stroke, self.stroke_t = True, 0.0
            if self.axes_w is None:
                self.axes_w = self._axes(q, self.up)
        self.stroke_t += dt
        r, f = self.axes_w
        ar = soft_deadzone(vdot(ah, r), self.c["accel_deadzone"])
        af = soft_deadzone(vdot(ah, f), self.c["accel_deadzone"])
        leak = math.exp(-dt / max(self.c["leak_s"], 0.05))
        self.v = [(self.v[0] + ar * dt) * leak, (self.v[1] + af * dt) * leak]
        return self.v[0], self.v[1]

    def take_correction(self):
        c, self.pending = self.pending, [0.0, 0.0]
        return c

    def reset(self):
        self.v = [0.0, 0.0]
        self.a_f = (0.0, 0.0, 0.0)
        self.in_stroke = False
        self.pending = [0.0, 0.0]
        self.axes_w = None


class MotionEngine:
    """Vitesses (lacet, tangage, roulis) en °/s, indépendantes de l'orientation de l'appareil.

    lacet  = rotation autour de la verticale (donnée par la gravité)
    tangage = rotation autour de l'axe latéral `lat` (appris en calibration) projeté à l'horizontale
    roulis = rotation autour de l'axe avant (vertical x latéral)
    Le biais du gyroscope est ré-estimé automatiquement dès que l'appareil est immobile.
    """

    def __init__(self, bias_cfg, profile=None):
        self.bcfg = bias_cfg
        self.set_profile(profile or DEFAULT_PROFILE)
        self.bias = (0.0, 0.0, 0.0)
        self.g = None
        self.win = []
        self.gwin = []
        self.still_t = 0.0
        self.bias_ok = False
        self._lat_h = None

    def set_profile(self, p):
        self.p = {**DEFAULT_PROFILE, **p}
        self.lat = vnorm(tuple(self.p["lat"]))
        self._lat_h = None

    def step(self, w, g, dt):
        if vlen(g) > 1e-3:
            gn = vnorm(g)
            self.g = gn if self.g is None else vnorm(tuple(a + 0.15 * (b - a) for a, b in zip(self.g, gn)))
        if self.g is None:
            return 0.0, 0.0, 0.0
        # --- biais : fenêtre glissante ; si l'étendue reste faible assez longtemps -> immobile
        self.win.append(w)
        self.gwin.append(self.g)
        if len(self.win) > 40:
            del self.win[0]
            del self.gwin[0]
        rng = max(max(s[k] for s in self.win) - min(s[k] for s in self.win) for k in range(3))
        g_move = math.degrees(math.acos(min(1.0, max(-1.0, vdot(self.gwin[0], self.gwin[-1])))))
        if rng < self.bcfg["still_range_dps"] and g_move < 1.5 and len(self.win) >= 8:
            self.still_t += dt
        else:
            self.still_t = 0.0
        if self.still_t >= self.bcfg["still_time_s"]:
            mean = tuple(sum(s[k] for s in self.win) / len(self.win) for k in range(3))
            # une rotation lente et régulière n'est PAS un biais : on n'accepte que des écarts plausibles
            jump = vlen(vsub(mean, self.bias))
            if jump < (20.0 if not self.bias_ok else 5.0):
                r = 1.0 if not self.bias_ok else self.bcfg["rate"]
                self.bias = tuple(b + r * (m - b) for b, m in zip(self.bias, mean))
                self.bias_ok = True
        wc = vsub(w, self.bias)
        # --- projection
        up = self.g
        yaw = vdot(wc, up)
        lat_h = reject(self.lat, up)
        if vlen(lat_h) < 0.25:          # axe latéral presque vertical : garder le précédent
            lat_h = self._lat_h or lat_h
        lat_h = vnorm(lat_h)
        self._lat_h = lat_h
        wh = reject(wc, up)
        pitch = vdot(wh, lat_h)
        roll = vdot(wh, vcross(up, lat_h))
        return yaw * self.p["yaw_sign"], pitch * self.p["pitch_sign"], roll * self.p["roll_sign"]


# --- calibration (fonctions pures, testées)
class CalibAccumulator:
    """Intègre les mesures d'une étape de calibration (corps de l'appareil)."""

    def __init__(self, bias, g_ref):
        self.bias, self.up = bias, vnorm(g_ref)
        self.yaw = 0.0
        self.horiz = (0.0, 0.0, 0.0)

    def add(self, w, dt):
        wc = vsub(w, self.bias)
        self.yaw += vdot(wc, self.up) * dt
        self.horiz = tuple(h + c * dt for h, c in zip(self.horiz, reject(wc, self.up)))

    def horiz_deg(self):
        return vlen(self.horiz)


def profile_from_calibration(up, yaw_left_integral, up_horiz_integral, roll_right_integral_vec):
    """Construit le profil : gauche = lacet négatif, haut = tangage positif, droite = roulis positif."""
    lat = vnorm(up_horiz_integral)                 # lever la tête = rotation autour de l'axe latéral
    prof = {"yaw_sign": -1 if yaw_left_integral > 0 else 1, "lat": [round(c, 4) for c in lat],
            "pitch_sign": 1, "roll_sign": 1}
    fwd = vcross(vnorm(up), vnorm(reject(lat, vnorm(up))))
    prof["roll_sign"] = 1 if vdot(roll_right_integral_vec, fwd) > 0 else -1
    return prof


def glide_profile(right_v, fwd_v):
    """Axes de glisse (repère corps) à partir des vitesses de pointe mesurées."""
    r = vnorm(right_v)
    f = vnorm(reject(fwd_v, r))
    return {"right": [round(c, 4) for c in r], "fwd": [round(c, 4) for c in f]}


def corner_range(tl, br):
    """Angles (lacet, tangage) visés pour les coins haut-gauche et bas-droit -> amplitudes en degrés."""
    (yl, pt), (yr, pb) = tl, br
    if not (yl < -2 and yr > 2 and pt > 1 and pb < -1):
        return None
    return {"x_neg": round(-yl, 2), "x_pos": round(yr, 2), "y_pos": round(pt, 2), "y_neg": round(-pb, 2)}


# =========================================================================== sortie souris
class Pointer:
    ABS_MAX = 32767

    def __init__(self):
        from evdev import UInput, ecodes as e, AbsInfo
        self.e = e
        keys = [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE]
        self.rel = UInput({e.EV_KEY: keys, e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL]},
                          name="AirPods Mouse (souris)", vendor=0x05AC, product=0x2013)
        info = AbsInfo(value=0, min=0, max=self.ABS_MAX, fuzz=0, flat=0, resolution=0)
        self.abs = UInput({e.EV_KEY: keys, e.EV_ABS: [(e.ABS_X, info), (e.ABS_Y, info)]},
                          name="AirPods Mouse (pointeur)", vendor=0x05AC, product=0x2014)
        self._fx = self._fy = 0.0
        self._wheel = 0.0
        self._last_abs = None

    def move_abs(self, fx, fy):
        e = self.e
        ix = int(min(max(fx, 0.0), 1.0) * self.ABS_MAX)
        iy = int(min(max(fy, 0.0), 1.0) * self.ABS_MAX)
        if (ix, iy) == self._last_abs:
            return
        self._last_abs = (ix, iy)
        self.abs.write(e.EV_ABS, e.ABS_X, ix)
        self.abs.write(e.EV_ABS, e.ABS_Y, iy)
        self.abs.syn()

    def move_rel(self, dx, dy):
        e = self.e
        self._fx += dx
        self._fy += dy
        ix, iy = int(self._fx), int(self._fy)
        self._fx -= ix
        self._fy -= iy
        if ix or iy:
            if ix:
                self.rel.write(e.EV_REL, e.REL_X, ix)
            if iy:
                self.rel.write(e.EV_REL, e.REL_Y, iy)
            self.rel.syn()
            self._last_abs = None

    def click(self, button="left"):
        e = self.e
        code = {"left": e.BTN_LEFT, "right": e.BTN_RIGHT, "middle": e.BTN_MIDDLE}[button]
        for v in (1, 0):
            self.rel.write(e.EV_KEY, code, v)
            self.rel.syn()
            time.sleep(0.015)

    def scroll(self, lines):
        self._wheel += lines
        n = int(self._wheel)
        if n:
            self._wheel -= n
            self.rel.write(self.e.EV_REL, self.e.REL_WHEEL, n)
            self.rel.syn()

    def close(self):
        self.rel.close()
        self.abs.close()


class NullPointer:
    def __getattr__(self, name):
        return lambda *a, **k: None


def notify(cfg, text):
    log.info(text)
    if cfg.get("notifications") and shutil.which("notify-send"):
        subprocess.Popen(["notify-send", "-a", "AirPods Mouse", "-t", "1500", "AirPods Mouse", text],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class OneEuro:
    def __init__(self, mincutoff, beta, dcutoff=1.0):
        self.mincutoff, self.beta, self.dcutoff = mincutoff, beta, dcutoff
        self.x = self.dx = None

    @staticmethod
    def _a(cutoff, dt):
        return 1.0 / (1.0 + 1.0 / (2 * math.pi * cutoff * dt))

    def __call__(self, x, dt):
        if self.x is None or dt <= 0:
            self.x, self.dx = x, 0.0
            return x
        dx = (x - self.x) / dt
        self.dx += self._a(self.dcutoff, dt) * (dx - self.dx)
        self.x += self._a(self.mincutoff + self.beta * abs(self.dx), dt) * (x - self.x)
        return self.x

    def reset(self, x=0.0):
        self.x, self.dx = x, 0.0


def soft_deadzone(v, dz):
    return 0.0 if abs(v) <= dz else math.copysign(abs(v) - dz, v)


# =========================================================================== moteur
_SYNC_LOCK = threading.RLock()       # one sync_opentrack at a time (main loop and web threads)


class Controller:
    def __init__(self, cfg, pointer, diag=False, cfg_path=None):
        self.cfg = cfg
        i18n.apply_config(cfg)
        self.cfg_path = cfg_path
        self.ptr = pointer
        self.headsets = None         # hid_headtracker.Manager (other headsets), started by run()
        self._hs_last = {}           # headset name -> (yaw, pitch) of the previous sample, for the mouse
        self.diag = diag
        self.pid = "0000"
        self.place = "ear"
        self.engine = MotionEngine(cfg["bias"])
        self.glide = GlideEngine(cfg["glisse"])
        self._make_filters()
        self.paused = bool(cfg.get("mouse_paused", True))
        self.scroll_mode = False
        self.ang = [0.0, 0.0]            # angles intégrés (mode pointeur)
        self.vel = (0.0, 0.0)            # vitesse curseur px/s (mode souris)
        self.target = (0.5, 0.5)
        self.cur = (0.5, 0.5)
        self.last_ts = None
        self.last_sample = 0.0
        self.last_ht = 0.0
        self.last_start_sent = 0.0
        self.n = 0
        self.rates = (0.0, 0.0, 0.0)
        self.roll_ang = 0.0
        self.roll_t0 = None
        self.roll_latched = False
        self.dwell_anchor = None
        self.dwell_t0 = 0.0
        self.dwell_moved = 0.0
        self.dwell_fired = True
        self.pos_px = [0.0, 0.0]
        self._last_print = 0.0
        self._last_render = time.monotonic()
        self._cfg_mtime = self._mtime()
        self.history = []            # [(temps, w, g, (lacet, tangage, roulis), att)] pour l'interface
        self.sample_hooks = []       # callbacks(w, g, dt) (calibration dans l'interface)
        self.gesture_hooks = []      # callbacks(ts_ns, now, att, w, g, a, (lacet, tangage, roulis)) — gestures.py
        self.raw_imu_hooks = []      # callbacks(kind "a"|"w", payload HID, now) — IMU brute pour gestures.py
        self.sample_times = []
        self.opentrack = None        # OpenTrackOutput actif (voir sync_opentrack)
        self.game_bridge = None      # game_bridge.GameBridge while at least one game is enabled
        self.calibration = None      # calibration.Calibration of a game profile while it runs
        self.last_calibration = None # the finished one, until the next start (its result is read once)
        self._calib_prev_paused = False
        self._calib_lock = threading.Lock()
        self.opentrack_forced = False  # --opentrack : activé pour la session
        self.demo_until = 0.0        # page /demo ouverte : pose calculée sans sortie (voir command("demo"))
        self.joystick_forced = False   # --sim-joystick : activé pour la session

    def _make_filters(self):
        s = min(max(float(self.cfg["smoothing"]), 0.0), 1.0)
        mc = 8.0 + (0.6 - 8.0) * s
        self.fy, self.fp = OneEuro(mc, 0.02), OneEuro(mc, 0.02)

    # ---- configuration à chaud
    def _mtime(self):
        try:
            return os.path.getmtime(self.cfg_path) if self.cfg_path else 0
        except OSError:
            return 0

    def maybe_reload(self):
        m = self._mtime()
        if m and m != self._cfg_mtime:
            self._cfg_mtime = m
            try:
                new = load_config(self.cfg_path)
            except (OSError, ValueError) as e:
                log.warning("config.json illisible (%s)", e)
                return
            old_mode = self.cfg["mode"]
            self.cfg.clear()
            self.cfg.update(new)
            want_paused = bool(new.get("mouse_paused", True))
            with self._calib_lock:
                if self.calibration is not None:
                    self._calib_prev_paused = want_paused    # applies when the calibration ends
                elif want_paused != self.paused:
                    self.paused = want_paused
                    self.vel = (0.0, 0.0)
            i18n.apply_config(self.cfg)
            self.engine.bcfg = self.cfg["bias"]
            self.glide.c = self.cfg["glisse"]
            self._make_filters()
            self._load_profile()
            self.sync_opentrack()
            if self.cfg["mode"] != old_mode:
                self.recenter(quiet=True)
            log.info("Réglages rechargés")

    # ---- source / profil
    @property
    def source(self):
        if self.cfg["source"] != "auto":
            return self.cfg["source"]
        return {"ear": "head", "case": "case", "out": "hand"}.get(self.place, "head")

    def profile_key(self):
        return f"{self.pid}_{self.source}"

    def _load_profile(self):
        self.glide.set_profile(self.cfg["profiles"].get(f"{self.pid}_glide", {}))
        prof = self.cfg["profiles"].get(self.profile_key())
        if prof is None or "lat" not in prof:
            log.warning("Profil %s non calibré : lance --calibrate dans cette position",
                        self.profile_key())
            prof = DEFAULT_PROFILE
        self.engine.set_profile(prof)

    def set_device(self, pid):
        self.pid = pid
        self._load_profile()

    def set_place(self, place):
        if place != self.place:
            self.place = place
            if self.cfg["source"] != "auto":       # source choisie à la main : rien ne bouge
                return
            log.info("Position détectée : %s -> source %s", place, tr(SOURCE_NAMES[self.source], "fr"))
            self._load_profile()
            self.recenter(quiet=True)

    # ---- commandes
    def command(self, cmd):
        cmd = cmd.strip().lower()
        if cmd == "recenter":
            self.recenter()
        elif cmd == "demo":                # la page /demo est ouverte : garder la pose de tête calculée
            self.demo_until = time.monotonic() + 3.0
            if not self.opentrack:
                self.sync_opentrack()
        elif cmd in ("toggle", "toggle_pause"):
            self.set_paused(not self.paused)
        elif cmd == "pause":
            self.set_paused(True)
        elif cmd == "resume":
            self.set_paused(False)
        elif cmd in ("scroll", "toggle_scroll"):
            self.scroll_mode = not self.scroll_mode
            notify(self.cfg, tr("srv.status.scroll_mode") if self.scroll_mode else tr("srv.status.cursor_mode"))
        elif cmd in ("mode", "toggle_mode"):
            order = ["souris", "pointeur", "glisse"]
            new = order[(order.index(self.cfg["mode"]) + 1) % 3] if self.cfg["mode"] in order else "souris"
            self.cfg["mode"] = new
            if self.cfg_path:
                update_config_file(self.cfg_path, {"mode": new})
                self._cfg_mtime = self._mtime()
            self.recenter(quiet=True)
            notify(self.cfg, tr("srv.status.mode", mode=tr(f"srv.mode.{new}")))
        elif cmd in ("left_click", "right_click", "middle_click"):
            if not self.inactive:
                self.ptr.click(cmd.split("_")[0])
                log.info("Clic %s", cmd.split("_")[0])
        elif cmd == "reload":
            self._cfg_mtime = 0
            self.maybe_reload()
        elif cmd == "quit":
            raise KeyboardInterrupt
        elif cmd not in ("none", ""):
            log.warning("Commande inconnue : %s", cmd)

    # ---- IMU brute pour les gestes (accel/gyro HID ~200 Hz)
    def raw_imu_wanted(self):
        gc = self.cfg.get("gestures", {})
        return bool(gc.get("enabled") and gc.get("use_raw_imu", True) and self.raw_imu_hooks)

    def motion_interval_us(self):
        """Intervalle devmotion6 (souris) : réduit tant que l'IMU brute tourne, si configuré."""
        iv = int(self.cfg.get("gestures", {}).get("dm6_interval_with_raw_us", 0) or 0)
        return iv if (self.raw_imu_wanted() and iv > 0) else int(self.cfg["sensor_interval_us"])

    def sync_raw_imu(self, aap):
        """Démarre / arrête les flux accel + gyro bruts selon la config (relancés après réannonce
        grâce à rhid_active). N'arrête que les flux démarrés ici."""
        want = self.raw_imu_wanted()
        iv = int(self.cfg.get("gestures", {}).get("raw_interval_us", 5000))
        kinds = getattr(aap, "imu_kinds", None)
        if kinds is None or not getattr(aap, "sock", None):
            return
        for name, kind in (("accel", "a"), ("gyro", "w")):
            dev = aap.rhid_devs.get(name)
            if dev is None:
                continue
            if want and (aap.rhid_active.get(dev) != iv or dev not in kinds):
                aap.rhid_set_feature(dev, 1, iv)
                aap.rhid_active[dev] = iv
                kinds[dev] = kind
                log.info("Gestes : flux %s brut (RemoteHID %d) à %d µs", name, dev, iv)
            elif not want and dev in kinds:
                aap.rhid_set_feature(dev, 1, 0)
                aap.rhid_active.pop(dev, None)
                kinds.pop(dev, None)
                log.info("Gestes : flux %s brut arrêté", name)

    def on_head_pose(self, yaw, pitch, roll, dt, yaw_rate=0.0, name=""):
        """Head angles from a HID head-tracker headset (another thread): simulator outputs, demo pages, mouse."""
        ot = self.opentrack
        if ot:
            ot.feed_angles(yaw, pitch, roll, dt, yaw_rate)
        hc = self.cfg.get("headsets", {})
        prev = self._hs_last.get(name)
        self._hs_last[name] = (yaw, pitch)
        if prev is None or not hc.get("mouse", True) or self.inactive or self.cfg.get("mode") != "souris":
            return
        k = float(hc.get("px_per_deg", 22.0))
        dyaw = (yaw - prev[0] + 180.0) % 360.0 - 180.0
        self.ptr.move_rel(dyaw * k, -(pitch - prev[1]) * k)

    def recenter(self, quiet=False):
        if self.headsets:
            self.headsets.recenter()
        self.ang = [0.0, 0.0]
        self.vel = (0.0, 0.0)
        self.glide.reset()
        self.fy.reset(0.0)
        self.fp.reset(0.0)
        self.target = self.cur = (0.5, 0.5)
        ot = self.opentrack
        if ot:
            ot.recenter()
        if not self.diag and not self.paused and not self.opentrack_pauses_mouse:
            self.ptr.move_abs(0.5, 0.5)
        if not quiet:
            notify(self.cfg, tr("srv.status.recentered"))

    def set_paused(self, p):
        self.paused = p
        self.vel = (0.0, 0.0)
        self.cfg["mouse_paused"] = bool(p)
        if self.cfg_path:
            update_config_file(self.cfg_path, {"mouse_paused": bool(p)})
            self._cfg_mtime = self._mtime()
        notify(self.cfg, tr("srv.status.paused") if p else tr("srv.status.control_active"))

    @property
    def inactive(self):
        if self.paused or self.opentrack_pauses_mouse:
            return True
        g = getattr(self, "gestures", None)
        gc = self.cfg.get("gestures", {})
        return bool(g and gc.get("enabled") and gc.get("freeze_cursor_when_armed", True) and g.cursor_frozen())

    # ---- OpenTrack (suivi de tête -> simulateurs) ------------------------------------------
    @property
    def opentrack_pauses_mouse(self):
        return bool(self.opentrack) and self.cfg.get("opentrack", {}).get("pause_mouse_when_opentrack", True)

    def sync_opentrack(self):
        # called from the main loop (config reload) and from web threads (calibration start/end)
        with _SYNC_LOCK:
            Controller._sync_opentrack(self)

    def _sync_opentrack(self):
        """Démarre / arrête / reconfigure les sorties « simulateur » selon la config (ou --opentrack,
        --sim-joystick) : UDP vers OpenTrack et/ou manette virtuelle, alimentées par la même pose."""
        oc = self.cfg.setdefault("opentrack", {})
        jc = self.cfg.setdefault("sim_joystick", {})
        if self.opentrack_forced:
            oc["enabled"] = True      # en mémoire seulement (affiché par l'interface, jamais écrit)
        if self.joystick_forced:
            jc["enabled"] = True
        want_udp, want_js = bool(oc.get("enabled")), bool(jc.get("enabled"))
        games_cfg = self.cfg.get("games") or {}
        want_games = any((g or {}).get("enabled") for g in games_cfg.values())
        want_calib = getattr(self, "calibration", None) is not None   # calibration reads the head angle
        want_demo = time.monotonic() < getattr(self, "demo_until", 0.0)
        if (want_udp or want_js or want_games or want_calib or want_demo) and not self.opentrack:
            try:
                import opentrack_out
                try:
                    self.opentrack = opentrack_out.OpenTrackOutput(oc, udp=want_udp).start()
                except (OSError, ValueError, TypeError) as e:
                    if want_udp:
                        raise
                    # broken UDP settings must not keep the game bridge / joystick from running
                    log.warning("OpenTrack settings ignored (%s) - output without UDP", e)
                    self.opentrack = opentrack_out.OpenTrackOutput({}, udp=False).start()
                if want_udp:
                    log.info("OpenTrack : envoi vers %s:%s à %s Hz", oc.get("host"), oc.get("port"), oc.get("rate_hz"))
                    notify(self.cfg, tr("srv.status.opentrack_on"))
            except (OSError, ValueError) as e:
                log.warning("OpenTrack indisponible : %s", e)
        elif want_udp or want_js or want_games or want_calib or want_demo:
            if want_udp != self.opentrack.udp:
                notify(self.cfg, tr("srv.status.opentrack_on") if want_udp else tr("srv.status.opentrack_off"))
            try:
                self.opentrack.configure(oc, udp=want_udp)
            except (OSError, ValueError, TypeError) as e:
                log.warning("OpenTrack settings ignored (%s)", e)
                self.opentrack.configure({}, udp=False)
        elif self.opentrack:
            self.opentrack.stop()
            self.opentrack = None
            if self.game_bridge:
                self.game_bridge.close()
                self.game_bridge = None
            log.info("Sorties simulateur : arrêtées")
            if not getattr(self, "_calib_ending", False):
                notify(self.cfg, tr("srv.status.sim_outputs_off"))
            return
        if self.opentrack:
            self.opentrack.tracker.suspend_autocenter = want_calib
            if want_games:
                if not self.game_bridge:
                    import game_bridge
                    self.game_bridge = game_bridge.GameBridge(os.environ.get("MOUSEPODS_SHM_DIR", "/dev/shm"))
                self.game_bridge.configure(games_cfg, self.cfg.get("game_baseline"))
                self.opentrack.set_game_bridge(self.game_bridge)
            else:
                self.opentrack.set_game_bridge(None)
                if self.game_bridge:
                    self.game_bridge.close()
                    self.game_bridge = None
            had = self.opentrack.joystick is not None
            js = self.opentrack.set_joystick(jc if want_js else None)
            if want_js and not had:
                if js is not None:
                    log.info("Manette virtuelle « tête » créée")
                    notify(self.cfg, tr("srv.status.joystick_on"))
                else:
                    log.warning("Manette virtuelle impossible : %s", self.opentrack.joystick_error)
    # ---- /OpenTrack ------------------------------------------------------------------------

    # ---- calibration of a game profile (overlay with points, see calibration.py)
    _calib_ending = False

    def start_calibration(self):
        """Start (or restart) a calibration: mouse paused for its duration, tracker running."""
        import calibration
        with self._calib_lock:
            if self.calibration is None:
                self._calib_prev_paused = self.paused
            self.paused = True                    # not set_paused: nothing is saved
            self.vel = (0.0, 0.0)
            self.calibration = calibration.Calibration(on_center=lambda: self.recenter(quiet=True))
            self.last_calibration = None
        self.sync_opentrack()

    def calibration_feed(self, now, yaw, pitch):
        with self._calib_lock:
            c = self.calibration
            if c is None:
                return
            c.feed(now, yaw, pitch)
            ended = c.finished
        if ended:
            self._end_calibration()

    def calibration_tick(self, now):
        with self._calib_lock:
            c = self.calibration
            if c is None:
                return
            c.tick(now)
            ended = c.finished
        if ended:
            self._end_calibration()

    def cancel_calibration(self):
        with self._calib_lock:
            if self.calibration is None:
                return
            self.calibration.cancel()
        self._end_calibration()

    def _end_calibration(self):
        with self._calib_lock:
            if self.calibration is None:
                return
            self.last_calibration, self.calibration = self.calibration, None
            self.paused = self._calib_prev_paused
            self.vel = (0.0, 0.0)
        self._calib_ending = True
        try:
            self.sync_opentrack()
        finally:
            self._calib_ending = False

    def save_baseline(self, raw):
        """Baseline calibration: default range for every game, and the pointer's screen edges for
        the current AirPods profile (if that one is calibrated). Returns whether the pointer got it,
        None if the values are unusable."""
        import game_profiles
        base = game_profiles.normalize_baseline(raw)
        if base is None or not self.cfg_path:
            return None
        updates = {"game_baseline": base}
        key = self.profile_key()
        with open(self.cfg_path) as f:
            prof = (json.load(f).get("profiles") or {}).get(key)
        pointer = isinstance(prof, dict) and "lat" in prof
        if pointer:
            prof = dict(prof)
            prof["range"] = {"x_neg": base["yaw"]["neg"], "x_pos": base["yaw"]["pos"],
                             "y_pos": base["pitch"]["pos"], "y_neg": base["pitch"]["neg"]}
            updates["profiles"] = {key: prof}
        update_config_file(self.cfg_path, updates)
        self.cfg["game_baseline"] = base          # the state shows it now; games/pointer follow on reload
        return pointer

    def calibration_state(self):
        with self._calib_lock:
            c = self.calibration or self.last_calibration
            if c is None:
                return {"active": False, "step": None}
            return {**c.state(), "active": self.calibration is not None}

    # ---- échantillons
    def on_packet(self, pkt, aap):
        now = time.monotonic()
        for blk in aap.process(pkt):
            self.last_ht = now
            self.on_block(blk, now)
        reports = getattr(aap, "imu_reports", None)
        while reports:
            kind, pl = reports.pop(0)
            for hook in list(self.raw_imu_hooks):
                hook(kind, pl, now)
        # l'état oreille oscille quand on tient un écouteur (capteur de proximité) : on attend 1,5 s
        if aap.place and not getattr(self, "_place_known", False):
            self._place_known = True              # premier état reçu : appliqué tout de suite
            self.set_place(aap.place)
        elif aap.place and aap.place != self.place:
            if aap.place != getattr(self, "_place_cand", None):
                self._place_cand, self._place_since = aap.place, now
            elif now - self._place_since >= 3.0:
                self.set_place(aap.place)
        else:
            self._place_cand = None
        while aap.stem_events:
            kind = aap.stem_events.pop(0)
            log.info("Pression tige : %s", kind)
            st = self.cfg["stem"]
            if st["enabled"] and not self.diag:
                action = st.get(kind, "none")
                if action in ("toggle_pause", "toggle", "recenter") or not self.paused:
                    self.command(action)

    def on_block(self, blk, now):
        ts, w, g = parse_block(blk, self.cfg["gyro_dps_per_lsb"])
        dt = (ts - self.last_ts) / 1e9 if self.last_ts else 0.0
        if not 0.0 < dt < 0.15:
            dt = min(max(now - self.last_sample, 0.001), 0.05) if self.last_sample else 0.01
        self.last_ts, self.last_sample = ts, now
        self.n += 1
        a = parse_accel(blk)
        self.last_acc = a
        yaw, pitch, roll = self.engine.step(w, g, dt)
        att = struct.unpack_from("<hhh", blk, 20)
        ot = self.opentrack          # one reference: a web thread may stop it meanwhile
        if ot:                       # OpenTrack / manette : alimentés même souris en pause
            p = self.engine.p
            ot.feed(yaw, pitch, roll, dt, self.engine.g, self.engine.lat,
                    (p["pitch_sign"], p["roll_sign"], p["yaw_sign"]), att)
            if self.calibration is not None:
                head = ot.head()
                if head is not None:
                    self.calibration_feed(now, head[0], head[1])
            if self.demo_until and time.monotonic() > self.demo_until:   # page /demo fermée
                self.demo_until = 0.0
                self.sync_opentrack()
        for hook in list(self.sample_hooks):
            hook(w, g, dt, a)
        for hook in list(self.gesture_hooks):
            hook(ts, now, att, w, g, a, (yaw, pitch, roll))
        self.history.append((now, w, g, (yaw, pitch, roll), att))
        if len(self.history) > 1200:
            del self.history[:200]
        self.sample_times.append(now)
        if len(self.sample_times) > 1200:
            del self.sample_times[:200]
        if self.cfg["mode"] == "glisse":
            q = quat_from_att(struct.unpack_from("<hhh", blk, 20))
            self.on_glide(vsub(w, self.engine.bias), g, a, dt, now, q)
        else:
            self.on_rates(yaw, pitch, roll, dt, now)

    def on_glide(self, wc, g, a, dt, now, q=None):
        c = self.cfg
        vr, vf = self.glide.step(wc, g, a, dt, q)
        self.rates = (vr * 100, vf * 100, 0.0)       # cm/s, pour l'affichage
        corr = self.glide.take_correction()
        if self.diag or self.inactive:
            self.vel = (0.0, 0.0)
            return
        ix = -1.0 if c["invert_x"] else 1.0
        iy = -1.0 if c["invert_y"] else 1.0
        k = c["glisse"]["px_per_cm"] * 100.0           # px par mètre
        self.vel = (ix * vr * k * c["sensitivity_x"], -iy * vf * k * c["sensitivity_y"])
        if corr[0] or corr[1]:                        # rattrapage de fin de geste
            self.ptr.move_rel(ix * corr[0] * k * c["sensitivity_x"], -iy * corr[1] * k * c["sensitivity_y"])
        if c["dwell_click"]["enabled"]:
            self.dwell(now)

    def on_rates(self, yaw, pitch, roll, dt, now):
        c = self.cfg
        yaw_f, pitch_f = self.fy(yaw, dt), self.fp(pitch, dt)
        self.rates = (yaw_f, pitch_f, roll)
        ix = -1.0 if c["invert_x"] else 1.0
        iy = -1.0 if c["invert_y"] else 1.0
        if self.diag:
            if now - self._last_print > 0.1:
                self._last_print = now
                print(f"\r[{tr(SOURCE_NAMES[self.source], 'fr'):7s}] lacet {yaw_f:+7.1f}°/s  tangage {pitch_f:+7.1f}°/s"
                      f"  roulis {roll:+7.1f}°/s  biais {'ok' if self.engine.bias_ok else '..'}"
                      f"  éch. {self.n}   ", end="", flush=True)
            return
        if self.inactive:
            self.vel = (0.0, 0.0)
            return
        # roulis : clic par inclinaison rapide (intégrateur à fuite)
        self.roll_ang = self.roll_ang * math.exp(-dt / 0.4) + roll * dt
        if c["roll_click"]["enabled"] and self.roll_gesture(now):
            self.vel = (0.0, 0.0)
            return
        if self.scroll_mode:
            self.ptr.scroll(-iy * pitch_f * dt * c["scroll"]["lines_per_deg"] * 4)
            self.vel = (0.0, 0.0)
            return
        if c["mode"] == "souris":
            s = c["souris"]
            dz = s["rate_deadzone_dps"]
            vy_, vp_ = soft_deadzone(yaw_f, dz), soft_deadzone(pitch_f, dz)
            speed = math.hypot(vy_, vp_)
            gain = s["px_per_deg"] * (1.0 + s["acceleration"] * min(speed / 60.0, 2.5))
            self.vel = (ix * vy_ * gain * c["sensitivity_x"], -iy * vp_ * gain * c["sensitivity_y"])
        else:
            p = c["pointeur"]
            dz = p.get("rate_deadzone_dps", 0.6)        # identique à la calibration des coins
            self.ang[0] += soft_deadzone(yaw_f, dz) * dt
            self.ang[1] += soft_deadzone(pitch_f, dz) * dt
            rng = self.engine.p.get("range")
            if rng:   # calibré en visant les coins : angle du coin -> bord exact de l'écran
                sx, sy = c["sensitivity_x"], c["sensitivity_y"]
                xl, xr = rng["x_neg"] / sx, rng["x_pos"] / sx
                yu, yd = rng["y_pos"] / sy, rng["y_neg"] / sy
                self.ang[0] = min(max(self.ang[0], -xl), xr)
                self.ang[1] = min(max(self.ang[1], -yd), yu)
                fx = 0.5 + 0.5 * (self.ang[0] / xr if self.ang[0] > 0 else self.ang[0] / xl)
                fy = 0.5 - 0.5 * (self.ang[1] / yu if self.ang[1] > 0 else self.ang[1] / yd)
                self.target = (0.5 + ix * (fx - 0.5), 0.5 + iy * (fy - 0.5))
            else:
                k = p["screen_pct_per_deg"] / 100.0
                kx, ky = k * c["sensitivity_x"], k * c["sensitivity_y"] * 1.6
                # pas d'accumulation au-delà des bords : on revient tout de suite en sens inverse
                lim_x, lim_y = 0.5 / max(kx, 1e-6), 0.5 / max(ky, 1e-6)
                self.ang[0] = min(max(self.ang[0], -lim_x), lim_x)
                self.ang[1] = min(max(self.ang[1], -lim_y), lim_y)
                self.target = (0.5 + ix * self.ang[0] * kx, 0.5 - iy * self.ang[1] * ky)
        if c["dwell_click"]["enabled"]:
            self.dwell(now)

    def render(self, now):
        dt = min(now - self._last_render, 0.1)
        self._last_render = now
        if self.diag or self.inactive:
            return
        if self.cfg["mode"] in ("souris", "glisse"):
            # trous dans le flux Bluetooth : on continue sur la lancée 80 ms puis on freine
            age = now - self.last_sample
            fade = 1.0 if age < 0.08 else max(0.0, 1.0 - (age - 0.08) / 0.1)
            dx, dy = self.vel[0] * dt * fade, self.vel[1] * dt * fade
            if dx or dy:
                self.ptr.move_rel(dx, dy)
                self.pos_px[0] += dx
                self.pos_px[1] += dy
        else:
            a = 1.0 - math.exp(-dt / max(self.cfg["render_lag_s"], 0.001))
            nx = self.cur[0] + (self.target[0] - self.cur[0]) * a
            ny = self.cur[1] + (self.target[1] - self.cur[1]) * a
            self.ptr.move_abs(nx, ny)
            self.pos_px = [nx * 1920, ny * 1200]
            self.cur = (nx, ny)

    def roll_gesture(self, now):
        rc = self.cfg["roll_click"]
        a = abs(self.roll_ang)
        if self.roll_latched:
            if a < rc["trigger_deg"] * 0.5:
                self.roll_latched = False
            return True
        if a > 4.0 and self.roll_t0 is None:
            self.roll_t0 = now
        elif a < 2.0:
            self.roll_t0 = None
        if a >= rc["trigger_deg"] and self.roll_t0 is not None:
            if now - self.roll_t0 <= rc["max_duration_s"]:
                action = rc["right"] if self.roll_ang > 0 else rc["left"]
                log.info("Inclinaison %s -> %s", "droite" if self.roll_ang > 0 else "gauche", action)
                self.command(action)
            self.roll_latched = True
            self.roll_t0 = None
            return True
        return a > 4.0

    def dwell(self, now):
        dc = self.cfg["dwell_click"]
        x, y = self.pos_px
        if self.dwell_anchor is None:
            self.dwell_anchor, self.dwell_t0 = (x, y), now
            return
        d = math.hypot(x - self.dwell_anchor[0], y - self.dwell_anchor[1])
        if d > dc["radius_px"]:
            self.dwell_moved += d
            self.dwell_anchor, self.dwell_t0, self.dwell_fired = (x, y), now, False
        elif not self.dwell_fired and self.dwell_moved >= dc["min_move_px"] and now - self.dwell_t0 >= dc["time_s"]:
            self.dwell_fired, self.dwell_moved = True, 0.0
            log.info("Clic par immobilité")
            self.ptr.click("left")


def open_ctrl_socket():
    if WINDOWS:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(CTRL_UDP)
        return s
    try:
        os.unlink(CTRL_SOCK)
    except FileNotFoundError:
        pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.bind(CTRL_SOCK)
    os.chmod(CTRL_SOCK, 0o600)
    return s


def run(cfg, cfg_path, diag=False, web_port=8737, opentrack=False, joystick=False):
    ctrl = open_ctrl_socket()
    if WINDOWS:
        import aap_windows
        aap_windows.raise_timer_resolution()
        if not diag:
            log.warning("Mouse mode is not available on Windows yet: running as --diag")
            diag = True
    ptr = NullPointer() if diag else Pointer()
    ctl = Controller(cfg, ptr, diag=diag, cfg_path=cfg_path)
    try:                       # gestes tête / mâchoire : module optionnel, ne doit jamais bloquer la souris
        import gestures
        ctl.gestures = gestures.GestureEngine(
            lambda: ctl.cfg.get("gestures", {}), command=ctl.command,
            notifier=lambda text: notify(ctl.cfg, text),
            save=lambda upd: update_config_file(cfg_path, {"gestures": upd}),
            profile_get=lambda: ctl.engine.p)
        ctl.gesture_hooks.append(ctl.gestures.on_sample)
        ctl.raw_imu_hooks.append(ctl.gestures.on_raw)
    except Exception as e:  # noqa: BLE001
        log.warning("Module gestes indisponible : %s", e)
    try:                       # other headsets (Android head-tracker HID): optional, never blocks the AirPods
        import hid_headtracker
        ctl.headsets = hid_headtracker.Manager(ctl.on_head_pose,
                                               enabled=lambda: ctl.cfg.get("headsets", {}).get("enabled", True))
        ctl.headsets.start()
    except Exception as e:  # noqa: BLE001
        log.warning("Head-tracker headsets unavailable: %s", e)
    ctl.opentrack_forced = opentrack
    ctl.joystick_forced = joystick
    ctl.sync_opentrack()
    web = None
    if web_port:
        try:
            import mousepods_web
            web = mousepods_web.WebUI(ctl, cfg_path, web_port)
            web.start()
            log.info("Interface : http://127.0.0.1:%d", web_port)
        except Exception as e:  # l'interface ne doit jamais empêcher la souris de marcher
            log.warning("Interface web indisponible : %s", e)
    stem_on = cfg["stem"]["enabled"] and not diag
    backoff = 2
    last_recovery = [-1e9]
    warned_case = False
    cfg_addr_hint = [None]
    tool_missing_logged = [False]
    log.info("Démarrage — mode %s, source %s", "DIAGNOSTIC" if diag else cfg["mode"], cfg["source"])
    aap = None
    try:
        while True:
            aap = AAPClient(cfg["device_address"], cfg["auto_connect"], cfg["sensor_interval_us"])
            if cfg_addr_hint[0]:
                aap.addr = cfg_addr_hint[0]
            try:
                aap.connect()
            except (OSError, subprocess.TimeoutExpired) as e:
                if isinstance(e, BluetoothToolMissing):
                    if not tool_missing_logged[0]:
                        log.error("%s", e)
                        tool_missing_logged[0] = True
                else:
                    log.warning("Connexion impossible (%s) — nouvel essai dans %ss", e, backoff)
                if web:
                    web.set_status(connect_failure_status(e))
                t_end = time.monotonic() + backoff
                while time.monotonic() < t_end:
                    r, _, _ = select.select([ctrl], [], [], min(0.2, max(0.0, t_end - time.monotonic())))
                    if r:
                        ctl.command(ctrl.recv(256).decode(errors="ignore"))
                    if web:
                        web.process_queue()
                backoff = min(backoff * 2, 20)
                continue
            backoff = 2
            ctl._place_known = False
            if web:
                web.attach(aap)
            ctl.set_device(aap.pid)
            if stem_on:
                aap.send(PKT_STEM_EVENTS_ON)
            aap.interval_us = ctl.motion_interval_us()
            aap.start_tracking()
            ctl.sync_raw_imu(aap)
            ctl.last_start_sent = ctl.last_ht = time.monotonic()
            connected_at = time.monotonic()
            frame = 1.0 / max(int(cfg["render_hz"]), 10)
            next_frame = time.monotonic()
            next_reload = time.monotonic() + 1.0
            try:
                while True:
                    r, _, _ = select.select([aap.sock, ctrl], [], [], max(0.0, next_frame - time.monotonic()))
                    if aap.sock in r:
                        pkt = aap.sock.recv(8192)
                        if not pkt:
                            raise OSError("canal fermé")
                        if web:
                            web.feed_packet(pkt)
                        ctl.on_packet(pkt, aap)
                    if web:
                        web.process_queue()
                    if ctrl in r:
                        ctl.command(ctrl.recv(256).decode(errors="ignore"))
                    now = time.monotonic()
                    if now >= next_frame:
                        ctl.render(now)
                        next_frame = now + frame
                    if now >= next_reload:
                        next_reload = now + 1.0
                        ctl.maybe_reload()
                        if ctl.motion_interval_us() != aap.interval_us:
                            aap.interval_us = ctl.motion_interval_us()
                            aap.start_tracking()
                        ctl.sync_raw_imu(aap)
                    wait = 20 if ctl.place == "case" else 4
                    if now - ctl.last_ht > wait and now - ctl.last_start_sent > wait:
                        log.info("Pas de données de mouvement — relance du flux")
                        aap.start_tracking()
                        ctl.last_start_sent = now
                    # AirPods bloqués (plus d'annonce des capteurs alors qu'ils sont sortis du boîtier) :
                    # seule une reconnexion Bluetooth complète les débloque.
                    if (aap.place in ("ear", "out") and not aap.rhid_devs and aap.pid == "2013"
                            and now - ctl.last_ht > 10 and now - connected_at > 10
                            and now - last_recovery[0] > 60):
                        last_recovery[0] = now
                        log.warning("Capteurs non annoncés — reconnexion Bluetooth complète")
                        aap.close()
                        if not WINDOWS:              # no BlueZ there; reopening the driver channel is all we can do
                            bt_disconnect(aap.addr)
                        time.sleep(2)
                        raise OSError("relance Bluetooth")
                    if aap.place == "case" and aap.pid == "2013" and not aap.rhid_devs and not warned_case:
                        warned_case = True
                        notify(cfg, tr("srv.status.airpods3_case"))
            except OSError as e:
                log.warning("Canal AAP perdu (%s), reconnexion…", e)
                if web:
                    web.set_status(i18n.tr_lazy("srv.status.lost_reconnecting"))
                prev_addr = aap.addr
                aap.close()
                time.sleep(0.3)
                if cfg["device_address"] == "auto" and prev_addr:
                    cfg_addr_hint[0] = prev_addr
    except KeyboardInterrupt:
        pass
    finally:
        if diag:
            print()
        if aap:
            aap.close(stem_restore=stem_on)
        if web:
            web.stop()
        if ctl.opentrack:
            ctl.opentrack.stop()
        ptr.close()
        ctrl.close()
        try:
            os.unlink(CTRL_SOCK)
        except OSError:
            pass
        log.info("Arrêté")


# =========================================================================== lecture capteur (thread, pour les fenêtres)
class SensorReader(threading.Thread):
    def __init__(self, cfg):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.lock = threading.Lock()
        self.samples = []            # (temps, dt, w, g)
        self.place = None
        self.pid = "0000"
        self.status = tr("srv.status.connecting")
        self.stop = False

    def run(self):
        aap = None
        while not self.stop:
            aap = AAPClient(self.cfg["device_address"], self.cfg["auto_connect"],
                            self.cfg["sensor_interval_us"])
            try:
                aap.connect()
                self.pid = aap.pid
                aap.start_tracking()
                self.status = tr("srv.status.connected_waiting")
                last_ts = None
                last_ht = last_start = time.monotonic()
                while not self.stop:
                    r, _, _ = select.select([aap.sock], [], [], 0.2)
                    now = time.monotonic()
                    if r:
                        pkt = aap.sock.recv(8192)
                        if not pkt:
                            raise OSError("canal fermé")
                        for blk in aap.process(pkt):
                            ts, w, g = parse_block(blk, self.cfg["gyro_dps_per_lsb"])
                            dt = (ts - last_ts) / 1e9 if last_ts else 0.01
                            if not 0 < dt < 0.15:
                                dt = 0.01
                            last_ts, last_ht = ts, now
                            with self.lock:
                                self.samples.append((now, dt, w, g))
                                del self.samples[:-3000]
                            self.status = tr("srv.status.receiving")
                        self.place = aap.place or self.place
                    if now - last_ht > 4 and now - last_start > 4:
                        aap.start_tracking()
                        last_start = now
                        self.status = tr("srv.status.no_data")
            except (OSError, subprocess.TimeoutExpired) as e:
                self.status = tr("srv.status.lost_retry", error=e)
                if aap:
                    aap.close()
                time.sleep(2)
        if aap:
            aap.close()

    def since(self, t0):
        with self.lock:
            return [s for s in self.samples if s[0] > t0]


# =========================================================================== calibration à l'écran
CALIB_TEXT = {src: {st: f"calib.step.{src}.{st}" for st in ("still", "yaw", "pitch", "roll", "back")}
              for src in ("head", "case", "hand")}          # translation keys (tr)
CALIB_STEPS = ["still", "yaw", "back", "pitch", "back", "roll", "back"]


def tk_language(cfg):
    """Resolve the UI language for a standalone Tk window and make it the process language,
    so texts produced elsewhere (SensorReader.status, ...) match the window."""
    lang = i18n.resolve_lang(cfg.get("ui_lang"), i18n.env_lang())
    i18n.set_lang(lang)
    return lang


def calibrate(cfg, cfg_path):
    import tkinter as tk
    lang = tk_language(cfg)

    def L(key, **variables):
        return tr(key, lang, **variables)
    reader = SensorReader(cfg)
    reader.start()
    root = tk.Tk()
    root.title(L("tk.calib.title"))
    root.geometry("800x520")
    root.configure(bg="#111")
    top = tk.Frame(root, bg="#111")
    top.pack(pady=(12, 0))
    src_var = tk.StringVar(value="auto")
    tk.Label(top, text=L("tk.calib.source"), fg="#ccc", bg="#111", font=("Sans", 12)).pack(side="left")
    for val, txt in (("auto", L("tk.src.auto")), ("head", L("tk.src.head")), ("case", L("tk.src.case")), ("hand", L("tk.src.hand_airpod"))):
        tk.Radiobutton(top, text=txt, value=val, variable=src_var, fg="#ccc", bg="#111",
                       selectcolor="#333", font=("Sans", 12)).pack(side="left", padx=4)
    title = tk.Label(root, text="", fg="#9cf", bg="#111", font=("Sans", 14))
    title.pack(pady=(14, 4))
    instr = tk.Label(root, text="", fg="white", bg="#111", font=("Sans", 26, "bold"), justify="center",
                     wraplength=760)
    instr.pack(pady=16, expand=True)
    canvas = tk.Canvas(root, width=640, height=34, bg="#222", highlightthickness=0)
    canvas.pack(pady=6)
    status = tk.Label(root, text="", fg="#aaa", bg="#111", font=("Sans", 11))
    status.pack(pady=4)
    btn = tk.Button(root, text=L("tk.calib.restart"), font=("Sans", 12))
    btn.pack(pady=(0, 14))

    st = {}

    def restart():
        st.clear()
        st.update(i=0, t0=time.monotonic(), still_since=None, bias=None, up=None, acc=None,
                  yaw=None, pitch=None, roll=None, done=False, source=None)
    btn.configure(command=restart)
    restart()

    def bar(frac, color="#39f"):
        canvas.delete("all")
        canvas.create_rectangle(0, 0, int(640 * min(max(frac, 0.0), 1.0)), 34, fill=color, width=0)

    def current_source():
        if src_var.get() != "auto":
            return src_var.get()
        return {"ear": "head", "case": "case", "out": "hand"}.get(reader.place or "ear", "head")

    def finish():
        prof = profile_from_calibration(st["up"], st["yaw"], st["pitch"], st["roll"])
        key = f"{reader.pid}_{st['source']}"
        with open(cfg_path) as f:
            user = json.load(f)
        user.setdefault("profiles", {})[key] = prof
        update_config_file(cfg_path, {"profiles": user["profiles"]})
        instr.configure(text=L("tk.calib.done"))
        title.configure(text=L("tk.calib.saved", key=key))
        bar(1, "#3c6")
        st["done"] = True

    def tick():
        status.configure(text=L("tk.calib.status", status=reader.status, place=reader.place or "?"))
        if not st["done"]:
            now = time.monotonic()
            samples = reader.since(st["t0"])
            if st["source"] is None:
                st["source"] = current_source()
            src = st["source"]
            step = CALIB_STEPS[st["i"]]
            title.configure(text=L("tk.calib.step", source=L(SOURCE_NAMES[src]).upper(), step=st["i"] + 1, total=len(CALIB_STEPS)))
            instr.configure(text=L(CALIB_TEXT[src][step]))
            if not samples:
                bar(0)
            elif step in ("still", "back"):
                recent = [s for s in samples if s[0] > now - 0.4]
                ws = [s[2] for s in recent]
                rng = max((max(w[k] for w in ws) - min(w[k] for w in ws)) for k in range(3)) if len(ws) > 4 else 99
                if rng < 6.0:
                    st["still_since"] = st["still_since"] or now
                    held = now - st["still_since"]
                    bar(held / 1.0, "#3c6")
                    if held >= 1.0:
                        if step == "still":
                            st["bias"] = tuple(sum(w[k] for w in ws) / len(ws) for k in range(3))
                            gs = [s[3] for s in recent]
                            st["up"] = vnorm(tuple(sum(g[k] for g in gs) / len(gs) for k in range(3)))
                        st["i"] += 1
                        st["t0"], st["still_since"], st["acc"] = now, None, None
                else:
                    st["still_since"] = None
                    bar(0)
            else:
                if st["acc"] is None:
                    st["acc"] = CalibAccumulator(st["bias"], st["up"])
                    st["n_used"] = 0
                for s in samples[st["n_used"]:]:
                    st["acc"].add(s[2], s[1])
                st["n_used"] = len(samples)
                acc = st["acc"]
                mag = abs(acc.yaw) if step == "yaw" else acc.horiz_deg()
                need = 20.0
                bar(mag / need)
                if mag >= need:
                    if step == "yaw":
                        st["yaw"] = acc.yaw
                    elif step == "pitch":
                        st["pitch"] = acc.horiz
                    else:
                        st["roll"] = acc.horiz
                    st["i"] += 1
                    st["t0"], st["still_since"], st["acc"] = now, None, None
            if st["i"] >= len(CALIB_STEPS):
                finish()
        root.after(40, tick)

    tick()

    def close():
        reader.stop = True
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", close)
    root.mainloop()
    reader.stop = True


# =========================================================================== panneau de réglages
def settings_window(cfg_path):
    import tkinter as tk
    from tkinter import ttk
    cfg = load_config(cfg_path)
    lang = tk_language(cfg)

    def L(key, **variables):
        return tr(key, lang, **variables)
    root = tk.Tk()
    root.title(L("tk.settings.title"))
    root.geometry("560x820")
    pending = {"job": None}
    vars_ = {}

    def save():
        pending["job"] = None
        upd = {"mode": vars_["mode"].get(), "source": vars_["source"].get(),
               "invert_x": vars_["invert_x"].get(), "invert_y": vars_["invert_y"].get(),
               "souris": {}, "pointeur": {}, "stem": {}, "roll_click": {}, "dwell_click": {}}
        for key, (sec, var) in sliders.items():
            v = round(float(var.get()), 4)
            if sec:
                upd[sec][key] = v
            else:
                upd[key] = int(v) if key == "sensor_interval_us" else v
        upd["stem"]["enabled"] = vars_["stem"].get()
        upd["roll_click"]["enabled"] = vars_["roll"].get()
        upd["dwell_click"]["enabled"] = vars_["dwell"].get()
        update_config_file(cfg_path, upd)
        status.configure(text=L("tk.settings.saved"))

    def changed(*_):
        status.configure(text="…")
        if pending["job"]:
            root.after_cancel(pending["job"])
        pending["job"] = root.after(250, save)

    frm = ttk.Frame(root, padding=12)
    frm.pack(fill="both", expand=True)
    row = [0]

    def section(txt):
        ttk.Label(frm, text=txt, font=("Sans", 12, "bold")).grid(row=row[0], column=0, columnspan=3,
                                                                  sticky="w", pady=(10, 2))
        row[0] += 1

    def radios(name, label, options):
        ttk.Label(frm, text=label).grid(row=row[0], column=0, sticky="w")
        v = tk.StringVar(value=cfg[name])
        f = ttk.Frame(frm)
        f.grid(row=row[0], column=1, columnspan=2, sticky="w")
        for val, txt in options:
            ttk.Radiobutton(f, text=txt, value=val, variable=v, command=changed).pack(side="left", padx=3)
        vars_[name] = v
        row[0] += 1

    sliders = {}

    def slider(key, label, lo, hi, sec=None, res=0.01):
        val = cfg[sec][key] if sec else cfg[key]
        ttk.Label(frm, text=label).grid(row=row[0], column=0, sticky="w")
        v = tk.DoubleVar(value=val)
        s = tk.Scale(frm, from_=lo, to=hi, resolution=res, orient="horizontal", variable=v,
                     length=300, command=changed)
        s.grid(row=row[0], column=1, columnspan=2, sticky="we")
        sliders[key] = (sec, v)
        row[0] += 1

    def check(name, label, value):
        v = tk.BooleanVar(value=value)
        ttk.Checkbutton(frm, text=label, variable=v, command=changed).grid(row=row[0], column=0,
                                                                          columnspan=3, sticky="w")
        vars_[name] = v
        row[0] += 1

    section(L("tk.sec.operation"))
    radios("mode", L("tk.mode"), (("souris", L("tk.mode.mouse")), ("pointeur", L("tk.mode.pointer"))))
    radios("source", L("tk.source"), (("auto", L("tk.src.auto")), ("head", L("tk.src.head")), ("case", L("tk.src.case")), ("hand", L("tk.src.hand"))))
    section(L("tk.sec.sensitivity"))
    slider("sensitivity_x", L("tk.sens_x"), 0.1, 5.0)
    slider("sensitivity_y", L("tk.sens_y"), 0.1, 5.0)
    check("invert_x", L("tk.invert_x"), cfg["invert_x"])
    check("invert_y", L("tk.invert_y"), cfg["invert_y"])
    section(L("tk.sec.mouse"))
    slider("px_per_deg", L("tk.px_per_deg"), 2, 80, "souris", 0.5)
    slider("acceleration", L("tk.acceleration"), 0, 4, "souris", 0.05)
    slider("rate_deadzone_dps", L("tk.deadzone"), 0, 15, "souris", 0.1)
    section(L("tk.sec.pointer"))
    slider("screen_pct_per_deg", L("tk.screen_pct"), 0.3, 10, "pointeur", 0.05)
    section(L("tk.sec.smooth"))
    slider("smoothing", L("tk.smoothing"), 0, 1)
    slider("render_lag_s", L("tk.render_lag"), 0.0, 0.15, None, 0.005)
    slider("sensor_interval_us", L("tk.sensor_interval"), 5000, 40000, None, 1000)
    section(L("tk.sec.clicks"))
    check("stem", L("tk.stem"), cfg["stem"]["enabled"])
    check("roll", L("tk.roll"), cfg["roll_click"]["enabled"])
    check("dwell", L("tk.dwell"), cfg["dwell_click"]["enabled"])
    slider("time_s", L("tk.dwell_time"), 0.4, 3.0, "dwell_click", 0.1)

    bf = ttk.Frame(frm)
    bf.grid(row=row[0], column=0, columnspan=3, pady=14)
    ttk.Button(bf, text=L("tk.recenter"), command=lambda: send_command("recenter")).pack(side="left", padx=4)
    ttk.Button(bf, text=L("tk.toggle"), command=lambda: send_command("toggle")).pack(side="left", padx=4)
    ttk.Button(bf, text=L("tk.calibrate"), command=lambda: subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--calibrate", "-c", cfg_path])).pack(side="left", padx=4)
    row[0] += 1
    status = ttk.Label(frm, text=L("tk.live_hint"))
    status.grid(row=row[0], column=0, columnspan=3)
    root.mainloop()


# =========================================================================== CLI
def send_command(cmd):
    if WINDOWS:                          # UDP cannot tell whether the controller is listening
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
            u.sendto(cmd.encode(), CTRL_UDP)
        return 0
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.sendto(cmd.encode(), CTRL_SOCK)
    except (FileNotFoundError, ConnectionRefusedError):
        print("Le contrôleur ne tourne pas.", file=sys.stderr)
        return 1
    return 0


HOTKEYS = [                      # name: translation key
    ("airpods-mouse-recenter", "srv.hotkey.recenter", "<Control><Alt>c", "recenter"),
    ("airpods-mouse-toggle", "srv.hotkey.toggle", "<Control><Alt>p", "toggle"),
    ("airpods-mouse-scroll", "srv.hotkey.scroll", "<Control><Alt>s", "scroll"),
    ("airpods-mouse-mode", "srv.hotkey.mode", "<Control><Alt>m", "mode"),
]
GS_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
GS_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"


def install_hotkeys():
    import ast
    cur = subprocess.run(["gsettings", "get", GS_SCHEMA, "custom-keybindings"],
                         capture_output=True, text=True, check=True).stdout.strip()
    paths = ast.literal_eval(cur.replace("@as ", "")) if cur else []
    py = os.path.join(HERE, ".venv", "bin", "python")
    py = py if os.path.exists(py) else sys.executable
    for ident, name_key, binding, cmd in HOTKEYS:
        name = tr(name_key)
        p = f"{GS_PATH}{ident}/"
        if p not in paths:
            paths.append(p)
        sch = f"{GS_SCHEMA}.custom-keybinding:{p}"
        command = f"{py} {os.path.abspath(__file__)} --send {cmd}"
        for key, val in (("name", name), ("binding", binding), ("command", command)):
            subprocess.run(["gsettings", "set", sch, key, val], check=True)
        print(f"  {binding:18s} -> {name}")
    subprocess.run(["gsettings", "set", GS_SCHEMA, "custom-keybindings", str(paths)], check=True)


def open_ui(port, cfg_path):
    import urllib.request
    url = f"http://127.0.0.1:{port}/"
    try:
        urllib.request.urlopen(url + "api/ping", timeout=1)
    except OSError:
        log.info("Lancement du contrôleur en arrière-plan…")
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "-c", cfg_path, "--port", str(port)],
                         stdout=open(os.path.join(HERE, "controller.log"), "a"), stderr=subprocess.STDOUT,
                         start_new_session=True)
        for _ in range(50):
            time.sleep(0.2)
            try:
                urllib.request.urlopen(url + "api/ping", timeout=1)
                break
            except OSError:
                pass
    for browser in ("google-chrome", "chromium", "chromium-browser", "brave-browser"):
        if shutil.which(browser):
            subprocess.Popen([browser, f"--app={url}", "--window-size=1280,860",
                              f"--class=AirPodsMouse"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            return
    subprocess.Popen(["xdg-open", url])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default=DEFAULT_CONFIG_PATH)
    ap.add_argument("--diag", action="store_true")
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--settings", action="store_true")
    ap.add_argument("--ui", action="store_true", help="ouvre l'application (lance le contrôleur si besoin)")
    ap.add_argument("--port", type=int, default=8737)
    ap.add_argument("--no-web", action="store_true")
    ap.add_argument("--send", metavar="CMD")
    ap.add_argument("--install-hotkeys", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--opentrack", action="store_true", help="envoie le suivi de tête à OpenTrack (UDP) pour la session")
    ap.add_argument("--sim-joystick", action="store_true",
                    help="crée une manette virtuelle dont les axes sont l'orientation de la tête (session)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if args.send:
        sys.exit(send_command(args.send))
    if args.install_hotkeys:
        i18n.apply_config(load_config(args.config))
        install_hotkeys()
        return
    if args.settings:
        settings_window(args.config)
        return
    if args.ui:
        open_ui(args.port, args.config)
        return
    cfg = load_config(args.config)
    if args.calibrate:
        calibrate(cfg, args.config)
        return
    run(cfg, args.config, diag=args.diag, web_port=None if args.no_web else args.port, opentrack=args.opentrack,
        joystick=args.sim_joystick)


if __name__ == "__main__":
    main()
