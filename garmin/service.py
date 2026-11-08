# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Matteo Gaudioso (starman-tech) — https://github.com/starman-tech/mousepods
"""GarminService: the Garmin mouse inside the MousePods web process (page « Garmin », /api/garmin).

Runs garmin_mouse.App (BLE + engine on its own asyncio thread) without the Tk window, keeps the
settings in garmin_mouse.json and the calibration in garmin_calib.json (both git-ignored).
garmin-ble is optional: when it is missing, state() says so and connect() refuses.
"""
from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import time
from collections import deque

from . import garmin_mouse as gm
from i18n import tr, tr_lazy  # noqa: E402  (importable once garmin_mouse has put the repository root on sys.path)

MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
# settings editable from the page: key -> (min, max) for numbers, None for booleans / choices
SETTINGS = {"max_speed": (300.0, 5000.0), "deadzone": (0.05, 0.5), "velocity_tau_s": (0.02, 0.25),
            "tap_g": (0.2, 1.2), "exponent": (1.0, 3.0), "pause_deg": (25.0, 90.0),
            "invert_x": None, "invert_y": None, "auto_center": None, "notifications": None,
            "autoconnect": None, "mode": ("joystick", "pointeur")}
HINTS = ("garmin", "fenix", "forerunner", "venu", "vivo", "instinct", "epix", "enduro", "tactix",
         "quatix", "marq", "descent", "approach", "lily", "edge")


def missing_modules():
    """Python modules the BLE link needs and that are not installed (empty = ready)."""
    return [m for m in ("garmin_ble", "bleak") if importlib.util.find_spec(m) is None]


def sanitize(values):
    """Keep known settings, clamp numbers, coerce types. Unknown keys are dropped."""
    out = {}
    for k, v in dict(values or {}).items():
        spec = SETTINGS.get(k, False)
        if spec is False:
            continue
        if spec is None:
            out[k] = bool(v)
        elif isinstance(spec[0], str):
            if v in spec:
                out[k] = v
        else:
            try:
                out[k] = min(max(float(v), spec[0]), spec[1])
            except (TypeError, ValueError):
                pass
    return out


class GarminService:
    def __init__(self, cfg_path=None, calib_path=None, app_factory=None, notifier=None):
        self.cfg_path, self.calib_path = cfg_path or gm.CFG_PATH, calib_path or gm.CALIB_PATH
        self.cfg = gm.load_cfg(self.cfg_path)
        self.app_factory = app_factory or gm.App
        self.desktop_notify = notifier if notifier is not None else gm.notify
        self.app = None
        self.messages = deque(maxlen=30)
        self.full_next = False         # « Recalibrer tout » pressed while disconnected

    # ---- notifications (desktop + list shown on the page)
    def _notify(self, cfg, text):
        self.messages.append({"t": time.time(), "text": text})
        if self.desktop_notify:
            self.desktop_notify(cfg, text)

    def autostart(self):
        if self.cfg.get("autoconnect") and self.cfg.get("address") and not missing_modules():
            self.connect()
        return self

    # ---- actions
    def connect(self, address=None, full=False):
        miss = missing_modules()
        if miss:
            raise RuntimeError(tr("garmin.error.not_installed", missing=", ".join(miss)))
        if address:
            if not MAC_RE.match(address.strip()):
                raise ValueError(tr("garmin.error.bad_address"))
            self.cfg["address"] = address.strip().upper()
            self.save()
        if not self.cfg.get("address"):
            raise ValueError(tr("garmin.error.no_address"))
        if self.app is not None and self.app.running:
            return
        full = full or self.full_next or gm.load_calib(self.calib_path) is None
        self.full_next = False
        self.app = self.app_factory(self.cfg["address"], self.cfg, full, notifier=self._notify,
                                    calib_path=self.calib_path)
        self.app.run_thread()

    def disconnect(self):
        app = self.app
        if app is not None:
            app.status = tr_lazy("garmin.status.disconnecting")
            app.close(timeout=3.0)

    def recenter(self):
        if self.app is None or not self.app.running:
            raise RuntimeError(tr("garmin.error.not_connected"))
        self.app.recalibrate(False)

    def recalibrate_all(self):
        if self.app is not None and self.app.running:
            self.app.recalibrate(True)
        else:
            self.full_next = True     # done at the next connection

    def pause(self):
        if self.app is None or not self.app.running:
            raise RuntimeError(tr("garmin.error.not_connected"))
        self.app.toggle()
        return self.app.enabled

    def set(self, values):
        upd = sanitize(values)
        self.cfg.update(upd)           # same dict as the running engine: applied live
        self.save()
        return upd

    def save(self):
        gm.save_cfg(self.cfg, self.cfg_path)

    def close(self):
        if self.app is not None:
            self.app.close(timeout=1.0)

    @staticmethod
    def paired_devices(timeout=4.0):
        """Paired Bluetooth devices from `bluetoothctl devices Paired` (read-only), Garmin-looking first."""
        exe = shutil.which("bluetoothctl")
        if not exe:
            return []
        try:
            out = subprocess.run([exe, "devices", "Paired"], capture_output=True, text=True, timeout=timeout).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        return parse_paired(out)

    # ---- état
    def state(self):
        miss = missing_modules()
        cal = gm.load_calib(self.calib_path)
        app = self.app
        return {"available": not miss, "missing": miss, "address": self.cfg.get("address"),
                "config": {k: self.cfg.get(k) for k in SETTINGS},
                "calibrated": cal is not None, "range_deg": cal.get("range_deg") if cal else None,
                "full_next": self.full_next,
                "app": app.state() if app is not None else None,
                "messages": list(self.messages)[-8:]}


def parse_paired(text):
    rows = []
    for line in text.splitlines():
        m = re.match(r"\s*Device\s+([0-9A-Fa-f:]{17})\s*(.*)$", line)
        if m:
            name = m.group(2).strip()
            rows.append({"address": m.group(1).upper(), "name": name,
                         "garmin": any(h in name.lower() for h in HINTS)})
    return sorted(rows, key=lambda r: not r["garmin"])
