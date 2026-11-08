# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Matteo Gaudioso (starman-tech) — https://github.com/starman-tech/mousepods
"""Garmin watch as a mouse: wrist tilt -> cursor, double tap on the watch -> click.

    python -m garmin.garmin_mouse --address XX:XX:XX:XX:XX:XX       # Tk window + mouse
    python -m garmin.garmin_mouse --calibrate                       # redo the guided calibration
    python -m garmin.garmin_mouse --mode pointeur                   # tilt = absolute position
    python -m garmin.garmin_mouse --replay captures/private/garmin/accel_XXXX.jsonl

The same engine runs inside the MousePods web interface (page « Garmin », garmin/service.py).
The address is remembered in garmin_mouse.json (git-ignored) after the first run.
Source: service 16 (accelerometer only, 25 Hz, 3 samples per BLE packet every 120 ms).
The BLE link uses garmin-ble (AGPL-3.0-only), installed separately: requirements-garmin.txt.

Guided calibration (5 poses, each validated by holding it still ~1 s):
  neutral, tilt RIGHT, tilt LEFT, hand UP, hand DOWN.
From your own poses we get the X and Y tilt axes (whatever the watch orientation on the wrist)
and your comfortable range in each of the 4 directions. Saved in garmin_calib.json (git-ignored).
At every start only the neutral pose is re-learnt (1 s still).

Output (120 Hz, smoothed, so 25 Hz input never shows as steps):
  joystick: tilt as a fraction of your range, beyond a deadzone -> speed (power curve)
  pointeur: tilt fraction -> screen position
  arm dropped (big tilt) -> pause. Resting a few degrees off neutral is absorbed (auto-center).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

from . import glink

ROOT = Path(__file__).resolve().parent.parent
CFG_PATH = ROOT / "garmin_mouse.json"        # git-ignored (watch address)
CALIB_PATH = ROOT / "garmin_calib.json"      # git-ignored
SAMPLE_DT = 0.040            # 3 samples per 120 ms packet
if str(ROOT) not in sys.path:  # i18n.py lives at the repository root (also when run as python -m garmin.…)
    sys.path.insert(0, str(ROOT))
from i18n import tr, tr_lazy  # noqa: E402

DEFAULTS = {
    "address": None,
    "mode": "joystick",
    "invert_x": False, "invert_y": False,
    "deadzone": 0.22,                       # fraction of your calibrated range
    "max_speed": 2200.0, "exponent": 1.7,   # px/s at full range, curve
    "velocity_tau_s": 0.07,                 # output smoothing
    "pause_deg": 55.0,
    "auto_center": True, "auto_center_band": 0.35, "auto_center_tau_s": 2.0,
    "filter_mincutoff": 1.5, "filter_beta": 0.02,
    "tap_g": 0.45, "tap_quiet_g": 0.12, "double_tap_s": 0.5, "tap_freeze_s": 0.35, "tap_undo_s": 0.16,
    "render_hz": 120,
    "notifications": True,
    "autoconnect": False,                   # web interface: connect when MousePods starts
}


# --------------------------------------------------------------------------- decoding / filters
def s12(raw):
    return raw - 4096 if raw & 0x800 else raw


def decode16(p: bytes):
    """16-byte service-16 payload -> list of (x, y, z) in g."""
    hdr = p[0] | p[1] << 8
    n = min(hdr >> 13, 3)
    v = []
    for i in range(9):
        b = 2 + (i // 2) * 3
        raw = (p[b] | (p[b + 1] & 0x0F) << 8) if i % 2 == 0 else (p[b + 1] >> 4 | p[b + 2] << 4)
        v.append(s12(raw) / 256.0)
    return [tuple(v[3 * k:3 * k + 3]) for k in range(n)]


class OneEuro:
    def __init__(self, mincutoff, beta, dcutoff=1.0):
        self.mincutoff, self.beta, self.dcutoff = mincutoff, beta, dcutoff
        self.x = self.dx = None

    @staticmethod
    def _a(c, dt):
        return 1.0 / (1.0 + 1.0 / (2 * math.pi * c * dt))

    def __call__(self, x, dt):
        if self.x is None:
            self.x, self.dx = x, 0.0
            return x
        dx = (x - self.x) / dt
        self.dx += self._a(self.dcutoff, dt) * (dx - self.dx)
        self.x += self._a(self.mincutoff + self.beta * abs(self.dx), dt) * (x - self.x)
        return self.x

    def reset(self, x):
        self.x, self.dx = x, 0.0


def unit(v):
    v = np.asarray(v, float)
    n = np.linalg.norm(v)
    return v / n if n > 1e-9 else v


def tangent(v, g0):
    return v - (v @ g0) * g0


# --------------------------------------------------------------------------- calibration
STEPS = [                    # (pose, prompt translation key)
    ("neutral", "garmin.calib.neutral"),
    ("right", "garmin.calib.right"),
    ("left", "garmin.calib.left"),
    ("up", "garmin.calib.up"),
    ("down", "garmin.calib.down"),
]
NEUTRAL_ONLY = [("neutral", "garmin.calib.neutral_only")]


class Calibrator:
    """Feed samples; walks through the poses. Pure logic (no I/O)."""
    HOLD = 25            # samples (~1 s) still to validate a pose
    STILL = 0.02         # max std of the unit gravity vector (~1.2 deg)
    MIN_TILT = 10.0      # deg away from neutral for a directional pose

    def __init__(self, steps=STEPS, base=None):
        self.steps = list(steps)
        self.i = 0
        self.win = deque(maxlen=self.HOLD)
        self.held = 0
        self.poses = {}
        self.base = base           # previous calibration (neutral-only mode)
        self.error = ""
        self.result = None

    @property
    def done(self):
        return self.result is not None

    @property
    def prompt(self):
        return tr("garmin.calib.done") if self.done else tr(self.steps[self.i][1])

    @property
    def progress(self):
        return min(1.0, self.held / self.HOLD)

    @property
    def step_label(self):
        return f"{min(self.i + 1, len(self.steps))}/{len(self.steps)}"

    def preview(self):
        """Rough tilt (deg) relative to neutral before calibration is complete, for the UI dot."""
        if not self.win or "neutral" not in self.poses:
            return 0.0, 0.0
        g0, v = self.poses["neutral"], self.win[-1]
        if "right" in self.poses and "left" in self.poses:
            ex = unit(tangent(self.poses["right"], g0) - tangent(self.poses["left"], g0))
        else:
            ex = unit(tangent(np.array([1.0, 0, 0]), g0))
        ey = unit(np.cross(g0, ex))
        if "up" in self.poses and tangent(self.poses["up"], g0) @ ey < 0:
            ey = -ey
        return (math.degrees(math.asin(np.clip(v @ ex, -1, 1))),
                math.degrees(math.asin(np.clip(v @ ey, -1, 1))))

    def feed(self, a):
        if self.done:
            return
        v = unit(a)
        if not 0.7 < np.linalg.norm(a) < 1.3:
            self.held = 0
            return
        self.win.append(v)
        still = len(self.win) >= 5 and np.array(self.win)[-5:].std(axis=0).max() < self.STILL
        key = self.steps[self.i][0]
        ok = still and self._pose_ok(key, v)
        self.held = self.held + 1 if ok else 0
        if self.held >= self.HOLD:
            self.poses[key] = unit(np.array(self.win)[-self.HOLD:].mean(axis=0))
            self.held = 0
            self.i += 1
            if self.i == len(self.steps):
                self._finish()

    def _pose_ok(self, key, v):
        if key == "neutral":
            return True
        g0 = self.poses["neutral"]
        ang = math.degrees(math.acos(np.clip(v @ g0, -1, 1)))
        if ang < self.MIN_TILT:
            self.error = ""
            return False
        t = tangent(v, g0)
        opposite = {"left": "right", "down": "up"}.get(key)
        if opposite and t @ tangent(self.poses[opposite], g0) > 0:
            self.error = tr("garmin.calib.wrong_side")
            return False
        if key in ("up", "down"):
            ex = unit(tangent(self.poses["right"], g0) - tangent(self.poses["left"], g0))
            if abs(unit(t) @ ex) > 0.75:
                self.error = tr("garmin.calib.not_up_down")
                return False
        self.error = ""
        return True

    def _finish(self):
        g0 = self.poses["neutral"]
        if self.base is not None:                       # neutral-only: keep axes, re-orthogonalize
            ex = unit(tangent(np.array(self.base["ex"]), g0))
            ey = np.array(self.base["ey"])
            ey = unit(tangent(ey, g0) - (ey @ ex) * ex)
            self.result = {**self.base, "g0": g0.tolist(), "ex": ex.tolist(), "ey": ey.tolist()}
            return
        P = {k: self.poses[k] for k in ("right", "left", "up", "down")}
        ex = unit(tangent(P["right"], g0) - tangent(P["left"], g0))
        ey = tangent(P["up"], g0) - tangent(P["down"], g0)
        ey = unit(ey - (ey @ ex) * ex)

        def deg(v, e):
            return abs(math.degrees(math.asin(np.clip(v @ e, -1, 1))))
        rng = {"right": deg(P["right"], ex), "left": deg(P["left"], ex),
               "up": deg(P["up"], ey), "down": deg(P["down"], ey)}
        rng = {k: round(max(8.0, v), 1) for k, v in rng.items()}
        self.result = {"g0": g0.tolist(), "ex": ex.tolist(), "ey": ey.tolist(), "range_deg": rng,
                       "created": time.strftime("%Y-%m-%d %H:%M:%S")}


# --------------------------------------------------------------------------- engine
class TiltEngine:
    """Calibrated tilt -> target velocity / position, plus taps. Pure logic (no I/O)."""

    def __init__(self, cfg, calib):
        self.c = cfg
        self.set_calibration(calib)
        self.fx = OneEuro(cfg["filter_mincutoff"], cfg["filter_beta"])
        self.fy = OneEuro(cfg["filter_mincutoff"], cfg["filter_beta"])
        self.ax = self.ay = 0.0              # filtered tilt, deg (x: right +, y: up +)
        self.ox = self.oy = 0.0              # auto-center offsets, deg
        self.recent = deque(maxlen=25)
        self.hist = deque(maxlen=40)
        self.lp = None
        self.hp_hist = deque(maxlen=6)
        self.last_tap = -9.0
        self.freeze_until = -9.0
        self.paused = False
        self.events = []
        self.t = 0.0

    def set_calibration(self, calib):
        self.cal = calib
        self.g0 = np.array(calib["g0"]); self.ex = np.array(calib["ex"]); self.ey = np.array(calib["ey"])
        self.rng = calib["range_deg"]
        self.ox = self.oy = 0.0

    def feed(self, a, t=None):
        self.t = t if t is not None else self.t + SAMPLE_DT
        a = np.asarray(a, float)
        if self.lp is None:
            self.lp = a.copy()
        hp = float(np.linalg.norm(a - self.lp))
        self.lp += 0.35 * (a - self.lp)
        # a tap is a sharp jolt out of a calm wrist (the 2nd tap of a double may follow the 1st's echo)
        calm = len(self.hp_hist) == self.hp_hist.maxlen and max(list(self.hp_hist)[:-1]) < self.c["tap_quiet_g"]
        second = self.t - self.last_tap < self.c["double_tap_s"]
        if hp > self.c["tap_g"] and (calm or second) and self.t - self.last_tap > 0.12:
            self._tap(hp)
        self.hp_hist.append(hp)
        n = np.linalg.norm(a)
        if not 0.6 < n < 1.4 or self.t < self.freeze_until:
            return
        u = a / n
        bx = math.degrees(math.asin(np.clip(u @ self.ex, -1, 1)))
        by = math.degrees(math.asin(np.clip(u @ self.ey, -1, 1)))
        self.ax = self.fx(bx, SAMPLE_DT)
        self.ay = self.fy(by, SAMPLE_DT)
        self.hist.append((self.t, self.ax, self.ay))
        self.paused = math.degrees(math.acos(np.clip(u @ self.g0, -1, 1))) > max(
            self.c["pause_deg"], 1.3 * max(self.rng.values()))
        self._auto_center()

    def _auto_center(self):
        """Drift fix: while the wrist rests still near neutral, neutral follows it slowly."""
        self.recent.append((self.ax, self.ay))
        if not self.c["auto_center"] or len(self.recent) < self.recent.maxlen:
            return
        nx, ny = self.norm()
        if max(abs(nx), abs(ny)) > self.c["auto_center_band"]:
            return
        if np.array(self.recent).std(axis=0).max() > 1.0:
            return
        k = SAMPLE_DT / self.c["auto_center_tau_s"]
        self.ox += k * (self.ax - self.ox)
        self.oy += k * (self.ay - self.oy)

    def norm(self):
        """Tilt as a signed fraction of your calibrated range: x right +, y up +."""
        rx, ry = self.ax - self.ox, self.ay - self.oy
        nx = rx / (self.rng["right"] if rx > 0 else self.rng["left"])
        ny = ry / (self.rng["up"] if ry > 0 else self.rng["down"])
        return nx, ny

    def _tap(self, g):
        self.events.append(("tap", self.t, round(g, 2)))
        back = self.t - self.c["tap_undo_s"]
        prev = [h for h in self.hist if h[0] <= back]
        if prev:
            _, ax, ay = prev[-1]
            self.ax, self.ay = ax, ay
            self.fx.reset(ax); self.fy.reset(ay)
        self.events.append(("undo", back))
        self.freeze_until = self.t + self.c["tap_freeze_s"]
        if self.t - self.last_tap < self.c["double_tap_s"]:
            self.events.append(("click", self.t))
            self.last_tap = -9.0
        else:
            self.last_tap = self.t

    def frozen(self):
        return self.t < self.freeze_until

    def velocity(self):
        """Target cursor velocity in px/s, screen convention (y down)."""
        c = self.c
        if self.paused or self.frozen():
            return 0.0, 0.0
        dz = c["deadzone"]

        def curve(n):
            m = (abs(n) - dz) / (1 - dz)
            return 0.0 if m <= 0 else math.copysign(c["max_speed"] * min(m, 1.25) ** c["exponent"], n)
        nx, ny = self.norm()
        vx, vy = curve(nx), -curve(ny)
        return (-vx if c["invert_x"] else vx), (-vy if c["invert_y"] else vy)

    def position(self):
        nx, ny = self.norm()
        nx = -nx if self.c["invert_x"] else nx
        ny = -ny if self.c["invert_y"] else ny
        return min(max(0.5 + 0.5 * nx, 0.0), 1.0), min(max(0.5 - 0.5 * ny, 0.0), 1.0)


# --------------------------------------------------------------------------- output
class Pointer:
    ABS_MAX = 32767

    def __init__(self):
        from evdev import UInput, AbsInfo, ecodes as e
        self.e = e
        keys = [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE]
        self.rel = UInput({e.EV_KEY: keys, e.EV_REL: [e.REL_X, e.REL_Y]}, name="Garmin Mouse")
        info = AbsInfo(value=0, min=0, max=self.ABS_MAX, fuzz=0, flat=0, resolution=0)
        self.abs = UInput({e.EV_KEY: keys, e.EV_ABS: [(e.ABS_X, info), (e.ABS_Y, info)]},
                          name="Garmin Mouse (pointeur)")
        self.fx = self.fy = 0.0

    def move_rel(self, dx, dy):
        self.fx += dx; self.fy += dy
        ix, iy = int(self.fx), int(self.fy)
        self.fx -= ix; self.fy -= iy
        if ix or iy:
            if ix:
                self.rel.write(self.e.EV_REL, self.e.REL_X, ix)
            if iy:
                self.rel.write(self.e.EV_REL, self.e.REL_Y, iy)
            self.rel.syn()

    def move_abs(self, fx, fy):
        self.abs.write(self.e.EV_ABS, self.e.ABS_X, int(fx * self.ABS_MAX))
        self.abs.write(self.e.EV_ABS, self.e.ABS_Y, int(fy * self.ABS_MAX))
        self.abs.syn()

    def click(self):
        for v in (1, 0):
            self.rel.write(self.e.EV_KEY, self.e.BTN_LEFT, v)
            self.rel.syn()

    def close(self):
        self.rel.close(); self.abs.close()


class Renderer:
    """120 Hz output: smoothed velocity (joystick) or smoothed position (pointeur)."""

    def __init__(self, eng: TiltEngine, ptr, cfg):
        self.eng, self.ptr, self.c = eng, ptr, cfg
        self.vx = self.vy = 0.0
        self.moves = deque(maxlen=60)
        self.px = self.py = None
        self.clicks = 0

    def step(self, dt, now):
        for ev in self.eng.events:
            if ev[0] == "undo":
                back = now - self.c["tap_undo_s"] - 0.05
                ux = sum(m[1] for m in self.moves if m[0] >= back)
                uy = sum(m[2] for m in self.moves if m[0] >= back)
                if self.c["mode"] == "joystick" and (ux or uy):
                    self.ptr.move_rel(-ux, -uy)
                self.moves.clear()
                self.vx = self.vy = 0.0
            elif ev[0] == "click":
                self.ptr.click()
                self.clicks += 1
        self.eng.events.clear()
        k = 1 - math.exp(-dt / self.c["velocity_tau_s"])
        if self.c["mode"] == "pointeur":
            if self.eng.paused or self.eng.frozen():
                return
            p = self.eng.position()
            self.px = p[0] if self.px is None else self.px + k * (p[0] - self.px)
            self.py = p[1] if self.py is None else self.py + k * (p[1] - self.py)
            # Only take the cursor while the wrist actually moves: writing the absolute position
            # every frame would pin the cursor and make the real mouse unusable (even to press
            # « Déconnecter »). A move bigger than ~0.4 % of the screen grabs it for 0.3 s.
            anchor = getattr(self, "_anchor", None)
            if anchor is None or max(abs(self.px - anchor[0]), abs(self.py - anchor[1])) > 0.004:
                self._anchor = (self.px, self.py)
                self._grab_until = now + 0.3
            if now < getattr(self, "_grab_until", -1.0):
                self.ptr.move_abs(self.px, self.py)
            return
        tx, ty = self.eng.velocity()
        self.vx += k * (tx - self.vx)
        self.vy += k * (ty - self.vy)
        dx, dy = self.vx * dt, self.vy * dt
        if abs(dx) > 1e-3 or abs(dy) > 1e-3:
            self.ptr.move_rel(dx, dy)
            self.moves.append((now, dx, dy))


# --------------------------------------------------------------------------- app
def notify(cfg, text):
    print(text, flush=True)
    if cfg.get("notifications") and shutil.which("notify-send"):
        subprocess.Popen(["notify-send", "-u", "critical", "-t", "2500", "-a", "Garmin Mouse",
                          "Garmin Mouse", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def load_cfg(path=None):
    path = Path(path or CFG_PATH)
    cfg = dict(DEFAULTS)
    if path.exists():
        try:
            old = json.loads(path.read_text())
        except ValueError:
            old = {}
        cfg.update({k: v for k, v in old.items() if k in DEFAULTS})
    return cfg


def save_cfg(cfg, path=None):
    Path(path or CFG_PATH).write_text(json.dumps(cfg, indent=2))


def load_calib(path=None):
    path = Path(path or CALIB_PATH)
    try:
        return json.loads(path.read_text()) if path.exists() else None
    except ValueError:
        return None


class App:
    """BLE + engine on an asyncio thread. The UI (Tk window or web page) only reads state() and
    calls recalibrate() / toggle() / close(); nothing here depends on Tk."""

    def __init__(self, address, cfg, full_calibration=False, pointer_factory=None, notifier=None,
                 calib_path=None):
        self.address, self.cfg = address, cfg
        self.calib_path = Path(calib_path or CALIB_PATH)
        self.pointer_factory = pointer_factory or Pointer
        self.notifier = notifier or notify
        base = None if full_calibration else load_calib(self.calib_path)
        self.cal = Calibrator(STEPS) if base is None else Calibrator(NEUTRAL_ONLY, base)
        self.eng = self.ren = self.ptr = None
        self.enabled = True
        self.connected = False
        self.status = tr_lazy("garmin.status.connecting")
        self.failed = False          # status is an error message (the UI shows it in red)
        self.rate = 0.0
        self.cmds = queue.Queue()
        self.stop = threading.Event()
        self.thread = None
        self._n = 0

    def _say(self, text):
        self.notifier(self.cfg, text)

    def recalibrate(self, full):
        self.cmds.put(full)

    def toggle(self):
        self.enabled = not self.enabled
        self._say(tr("garmin.msg.mouse_on") if self.enabled else tr("garmin.msg.mouse_paused"))

    def on_sample(self, a):
        self._n += 1
        if self.eng is None:
            prev = self.cal.i
            self.cal.feed(a)
            if self.cal.done:
                self.calib_path.write_text(json.dumps(self.cal.result, indent=2))
                eng = TiltEngine(self.cfg, self.cal.result)
                self.ren = Renderer(eng, self.ptr, self.cfg)
                self.eng = eng
                r = self.cal.result["range_deg"]
                self._say(tr("garmin.msg.calibrated", right=r["right"], left=r["left"], up=r["up"],
                             down=r["down"]))
            elif self.cal.i != prev:
                self._say(tr("garmin.msg.step_ok", step=self.cal.step_label, prompt=self.cal.prompt))
            return
        was = self.eng.paused
        self.eng.feed(a)
        if was != self.eng.paused:
            self.status = tr_lazy("garmin.status.paused_arm") if self.eng.paused else tr_lazy("garmin.status.active")

    def _handle_cmds(self):
        while not self.cmds.empty():
            full = self.cmds.get()
            base = None if full or self.eng is None else self.eng.cal
            self.eng = self.ren = None
            self.cal = Calibrator(STEPS) if base is None else Calibrator(NEUTRAL_ONLY, base)
            self._say(tr("garmin.msg.calib_step", step=self.cal.step_label, prompt=self.cal.prompt))

    def tick(self, dt, now):
        """One output step (called at render_hz by main(); also used by the tests)."""
        self._handle_cmds()
        ren = self.ren
        if ren is not None and self.enabled:
            ren.step(dt, now)
        elif ren is not None:
            ren.eng.events.clear()

    async def main(self):
        try:
            self.ptr = self.pointer_factory()
        except Exception as exc:          # no /dev/uinput access, evdev missing…
            self.status = tr_lazy("garmin.status.error_pointer", error=exc)
            self.failed = True
            self._say(self.status)
            return

        def on_frame(fr):
            if fr.direction.value == "rx" and fr.decoded and fr.decoded.startswith("accelerometer"):
                for a in decode16(fr.raw[1:]):
                    self.on_sample(a)
        try:
            from garmin_ble import metrics
            async with glink.session(self.address, reconnect="exponential") as watch:
                watch.on_frame(on_frame)
                self.connected = True
                self.status = tr_lazy("garmin.status.connected")
                self._say(tr("garmin.msg.connected", step=self.cal.step_label, prompt=self.cal.prompt))

                async def drain():
                    async for _ in watch.stream(metrics.ACCELEROMETER):
                        pass
                task = asyncio.ensure_future(drain())
                last = t_rate = time.perf_counter()
                n_rate = 0
                while not self.stop.is_set() and not task.done():
                    await asyncio.sleep(1.0 / self.cfg["render_hz"])
                    now = time.perf_counter()
                    self.tick(min(now - last, 0.05), now)
                    last = now
                    if now - t_rate > 1.0:
                        self.rate = (self._n - n_rate) / (now - t_rate)
                        n_rate, t_rate = self._n, now
                task.cancel()
            self.status = tr_lazy("garmin.status.disconnected")
        except Exception as exc:          # show it in the UI instead of dying silently
            self.status = tr_lazy("garmin.status.error", error=exc)
            self.failed = True
            self._say(self.status)
        finally:
            self.connected = False
            self.rate = 0.0
            if self.ptr is not None:
                self.ptr.close()

    def run_thread(self):
        self.thread = threading.Thread(target=lambda: asyncio.run(self.main()), name="garmin", daemon=True)
        self.thread.start()
        return self

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def close(self, timeout=3.0):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout)

    def state(self):
        """Snapshot for a UI (JSON-serializable)."""
        e, ren, cal = self.eng, self.ren, self.cal
        d = {"status": self.status, "failed": self.failed, "connected": self.connected, "running": self.running,
             "rate": round(self.rate, 1), "enabled": self.enabled, "clicks": ren.clicks if ren else 0,
             "calibrating": e is None}
        if e is None:
            px, py = cal.preview()
            d["calib"] = {"prompt": cal.prompt, "step": min(cal.i + 1, len(cal.steps)), "steps": len(cal.steps),
                          "full": len(cal.steps) > 1, "progress": round(cal.progress, 3), "error": cal.error,
                          "tilt_deg": [round(px, 1), round(py, 1)]}
        else:
            nx, ny = e.norm()
            d.update({"paused": e.paused, "frozen": e.frozen(), "norm": [round(nx, 3), round(ny, 3)],
                      "range_deg": e.rng})
        return d


def run_window(app: App):
    import tkinter as tk
    root = tk.Tk()
    root.title("Garmin Mouse")
    root.geometry("580x700")
    root.configure(bg="#111")
    big = tk.Label(root, text="", font=("Sans", 17, "bold"), fg="white", bg="#111", wraplength=540, height=3)
    big.pack(pady=(14, 4))
    sub = tk.Label(root, text="", font=("Sans", 11), fg="#ffb347", bg="#111", wraplength=540)
    sub.pack()
    S = 300
    cv = tk.Canvas(root, width=S, height=S, bg="#1b1b1b", highlightthickness=0)
    cv.pack(pady=10)
    stat = tk.Label(root, text="", font=("Sans", 10), fg="#aaa", bg="#111")
    stat.pack()
    bar = tk.Frame(root, bg="#111")
    bar.pack(pady=8)
    for txt, fn in ((tr("garmin.recenter"), lambda: app.recalibrate(False)),
                    (tr("garmin.recalibrate_all"), lambda: app.recalibrate(True)),
                    (tr("garmin.win.pause_resume"), app.toggle)):
        tk.Button(bar, text=txt, command=fn, font=("Sans", 11)).pack(side="left", padx=6)

    # live settings (saved to garmin_mouse.json)
    sets = tk.Frame(root, bg="#111")
    sets.pack(pady=4)

    def slider(label, key, lo, hi, res):
        f = tk.Frame(sets, bg="#111"); f.pack(fill="x")
        tk.Label(f, text=label, width=14, anchor="w", fg="#ddd", bg="#111").pack(side="left")
        s = tk.Scale(f, from_=lo, to=hi, resolution=res, orient="horizontal", length=300,
                     bg="#111", fg="#ddd", highlightthickness=0,
                     command=lambda v: (app.cfg.__setitem__(key, float(v)), save_cfg(app.cfg)))
        s.set(app.cfg[key]); s.pack(side="left")
    slider(tr("garmin.win.max_speed"), "max_speed", 300, 5000, 100)
    slider(tr("garmin.win.deadzone"), "deadzone", 0.05, 0.5, 0.01)
    slider(tr("garmin.win.smoothing"), "velocity_tau_s", 0.02, 0.25, 0.01)
    slider(tr("garmin.win.tap_strength"), "tap_g", 0.2, 1.2, 0.05)
    mode = tk.StringVar(value=app.cfg["mode"])
    mf = tk.Frame(sets, bg="#111"); mf.pack(pady=4)
    for m in ("joystick", "pointeur"):
        tk.Radiobutton(mf, text=m, value=m, variable=mode, bg="#111", fg="#ddd", selectcolor="#333",
                       command=lambda: (app.cfg.__setitem__("mode", mode.get()), save_cfg(app.cfg))
                       ).pack(side="left", padx=8)

    def tick():
        cv.delete("all")
        c = S / 2
        cv.create_line(c, 0, c, S, fill="#333"); cv.create_line(0, c, S, c, fill="#333")
        e = app.eng
        if e is None:
            big.config(text=f"{app.cal.step_label}  {app.cal.prompt}")
            sub.config(text=app.cal.error)
            px, py = app.cal.preview()
            x, y = c + px / 40 * c, c - py / 40 * c
            cv.create_arc(c - 40, c - 40, c + 40, c + 40, start=90, extent=-359.9 * app.cal.progress,
                          style="arc", outline="#4cd964", width=6)
            cv.create_oval(x - 9, y - 9, x + 9, y + 9, fill="#ffb347", outline="")
        else:
            dz = app.cfg["deadzone"] * (c - 6)
            cv.create_rectangle(c - dz, c - dz, c + dz, c + dz, outline="#555", dash=(3, 3))
            cv.create_rectangle(6, 6, S - 6, S - 6, outline="#444")
            nx, ny = e.norm()
            x, y = c + max(-1.2, min(1.2, nx)) * (c - 6), c - max(-1.2, min(1.2, ny)) * (c - 6)
            off = e.paused or not app.enabled
            col = "#666" if off else ("#ff3b30" if e.frozen() else "#4cd964")
            cv.create_oval(x - 10, y - 10, x + 10, y + 10, fill=col, outline="")
            big.config(text=tr("garmin.win.mouse_paused" if off else "garmin.win.mouse_active", mode=app.cfg["mode"]))
            sub.config(text=tr("garmin.win.usage"))
        ren = app.ren
        stat.config(text=tr("garmin.win.stats", status=app.status, rate=f"{app.rate:.0f}")
                         + (tr("garmin.win.clicks", n=ren.clicks) if ren else ""))
        root.after(33, tick)

    def close():
        app.stop.set()
        root.after(300, root.destroy)
    root.protocol("WM_DELETE_WINDOW", close)
    tick()
    root.mainloop()


def replay(path, cfg):
    """Run the engine offline on a capture (neutral from the first second, default axes)."""
    rows = [r for r in glink.load(path) if r.get("dir") == "rx" and str(r.get("dec", "")).startswith("accelerometer")]
    samples = [a for r in rows for a in decode16(bytes.fromhex(r["hex"])[1:])]
    g0 = unit(np.mean(samples[:25], axis=0))
    ex = unit(tangent(np.array([1.0, 0, 0]), g0)); ey = unit(np.cross(g0, ex))
    eng = TiltEngine(cfg, {"g0": g0.tolist(), "ex": ex.tolist(), "ey": ey.tolist(),
                           "range_deg": {"right": 20, "left": 20, "up": 20, "down": 20}})
    for a in samples:
        eng.feed(a)
    return {"samples": len(samples), "taps": sum(e[0] == "tap" for e in eng.events),
            "clicks": sum(e[0] == "click" for e in eng.events)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", help="watch BLE address (remembered in garmin_mouse.json)")
    ap.add_argument("--mode", choices=("joystick", "pointeur"))
    ap.add_argument("--calibrate", action="store_true", help="redo the full guided calibration")
    ap.add_argument("--replay")
    a = ap.parse_args()
    import i18n
    i18n.apply_config({"ui_lang": i18n.setting_from_file(str(ROOT / "config.json"))})   # MousePods' saved language
    cfg = load_cfg()
    if a.mode:
        cfg["mode"] = a.mode
    if a.replay:
        print(json.dumps(replay(a.replay, cfg), indent=2))
        sys.exit(0)
    if a.address:
        cfg["address"] = a.address
    save_cfg(cfg)
    app = App(cfg["address"], cfg, a.calibrate)
    app.run_thread()
    try:
        run_window(app)
    except KeyboardInterrupt:
        app.stop.set()
