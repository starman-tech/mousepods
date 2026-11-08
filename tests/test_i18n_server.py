# SPDX-License-Identifier: Apache-2.0
"""Texts the Python side sends to the UI must go through i18n.tr().

Run:  python -m unittest tests.test_i18n_server -v
"""
import ast
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import i18n  # noqa: E402

# Modules whose strings reach the web UI. Tk windows are checked separately.
UI_MODULES = ["mousepods_web.py", "airpods_mouse_controller.py", "labo.py", "gestures.py",
              "silent_speech.py", "garmin/garmin_mouse.py", "garmin/service.py"]
TK_FUNCTIONS = {"settings_window", "calibrate"}
FRENCH = re.compile(r"[éèêàâçùûôîœÉÈÀÊ]|\b(de|la|le|les|des|du|en|pas|une|un|est|avec|pour|sur|au|aux|ou|et|"
                    r"encore|aucun|aucune|attente|droite|gauche|haut|bas|immobile|tourne|incline|regarde|garde|"
                    r"tiens)\b", re.I)
# French values that are protocol/state identifiers compared in code, never shown as such.
# Each entry needs a reason.
import airpods_mouse_controller as _ctl  # noqa: E402

IDENTIFIERS = {
    _ctl.BLUETOOTHCTL_MISSING,   # bilingual log/exception text; the UI shows srv.status.bluetoothctl_missing
}
SKIP_CALLS = {"debug", "info", "warning", "error", "exception", "critical", "print", "add_argument",
              "ArgumentParser", "tr", "_"}


def french_literals(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    skip = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            name = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
            if name in SKIP_CALLS:
                skip.update(id(a) for a in ast.walk(n))
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant):
                skip.add(id(n.body[0].value))
            if isinstance(n, ast.FunctionDef) and n.name in TK_FUNCTIONS:
                skip.update(id(a) for a in ast.walk(n))
        if isinstance(n, ast.Raise) and n.exc is not None:
            # exception messages end up in logs, not in the UI
            skip.update(id(a) for a in ast.walk(n.exc))
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip:
            v = n.value
            if v in IDENTIFIERS:
                continue
            if re.search(r"[éèêàâçùûôîœÉÈÀÊ]", v) or (" " in v.strip() and FRENCH.search(v)):
                out.append(f"{path}:{n.lineno}: {v[:60]!r}")
    return out


class ServerStringsTests(unittest.TestCase):
    def test_model_name_translated(self):
        self.assertEqual(i18n.tr("model.2013", "en"), "AirPods (3rd generation)")
        self.assertEqual(i18n.tr("model.2013", "fr"), "AirPods (3ᵉ génération)")

    def test_info_labels_complete(self):
        import mousepods_web as web
        for key in web.INFO_LABEL_KEYS:
            self.assertNotEqual(i18n.tr(key, "en"), key)

    def test_server_has_no_french_ui_literals(self):
        bad = []
        for path in UI_MODULES:
            bad += french_literals(path)
        self.assertEqual(bad, [])

    def test_tk_windows_have_no_french_literals(self):
        with open(os.path.join(ROOT, "airpods_mouse_controller.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        bad = []
        for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in TK_FUNCTIONS):
            skip = set()
            for n in ast.walk(fn):
                if isinstance(n, ast.Call):
                    name = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
                    if name in SKIP_CALLS - {"tr"}:
                        skip.update(id(a) for a in ast.walk(n))
                    if name == "tr" and len(n.args) > 1 and isinstance(n.args[1], ast.Constant):
                        bad.append(f"{fn.name}:{n.lineno}: tr() with fixed language {n.args[1].value!r}")
            if fn.body and isinstance(fn.body[0], ast.Expr):
                skip.add(id(fn.body[0].value))
            for n in ast.walk(fn):
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip:
                    v = n.value
                    if re.search(r"[éèêàâçùûôîœÉÈÀÊ]", v) or (" " in v.strip() and FRENCH.search(v)) or v == "Recentrer":
                        bad.append(f"{fn.name}:{n.lineno}: {v[:60]!r}")
        self.assertEqual(bad, [])

    def test_language_switch_is_visible_immediately(self):
        import json
        import tempfile
        import types
        import urllib.request
        import mousepods_web as web
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, "config.json")
            with open(cfg_path, "w") as f:
                f.write("{}")
            ui = web.WebUI(types.SimpleNamespace(cfg={"ui_lang": "fr"}), cfg_path, port=0)
            ui.start()
            port = ui.server.server_address[1]
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/config", method="POST",
                                         data=json.dumps({"ui_lang": "en"}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req).read()
            got = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/i18n"))
            ui.server.shutdown()
        self.assertEqual((got["setting"], got["lang"]), ("en", "en"))

    def test_tk_language_sets_process_language(self):
        import airpods_mouse_controller as m
        i18n.set_lang("en")
        self.assertEqual(m.tk_language({"ui_lang": "fr"}), "fr")
        self.assertEqual(i18n.get_lang(), "fr")   # SensorReader.status etc. follow too
        i18n.set_lang("en")

    def test_tk_windows_use_tk_language(self):
        import inspect
        import airpods_mouse_controller as m
        for fn in (m.calibrate, m.settings_window):
            self.assertIn("tk_language(", inspect.getsource(fn), fn.__name__)

    def test_invalid_language_setting_is_rejected(self):
        import json
        import tempfile
        import types
        import urllib.error
        import urllib.request
        import mousepods_web as web
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, "config.json")
            with open(cfg_path, "w") as f:
                f.write("{}")
            ui = web.WebUI(types.SimpleNamespace(cfg={"ui_lang": "fr"}), cfg_path, port=0)
            ui.start()
            port = ui.server.server_address[1]
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/config", method="POST",
                                         data=json.dumps({"ui_lang": "xx"}).encode(),
                                         headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req)
            got = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/i18n"))
            with open(cfg_path) as f:
                saved = json.load(f)
            ui.server.shutdown()
        self.assertEqual(cm.exception.code, 400)
        self.assertEqual(got["setting"], "fr")
        self.assertNotIn("ui_lang", saved)

    def test_current_lang_follows_config(self):
        i18n.set_lang("fr")
        self.assertEqual(i18n.tr("lang.auto"), "Automatique")
        i18n.set_lang("en")
        self.assertEqual(i18n.tr("lang.auto"), "Automatic")


if __name__ == "__main__":
    unittest.main()
