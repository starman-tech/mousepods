# SPDX-License-Identifier: Apache-2.0
"""First-run wizard: shown until done, completion persisted.

Run:  python -m unittest tests.test_wizard -v
"""
import json
import os
import re
import shutil
import sys
import tempfile
import types
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import airpods_mouse_controller as m  # noqa: E402
import mousepods_web as web  # noqa: E402


def html():
    with open(os.path.join(ROOT, "ui", "index.html"), encoding="utf-8") as f:
        return f.read()


class WizardTests(unittest.TestCase):
    def test_wizard_page_exists(self):
        h = html()
        self.assertRegex(h, r'<section class="page" id="p-welcome">\s*<h1 data-i18n="welcome\.title">')
        for step in ("wz-lang", "wz-airpods", "wz-calib", "wz-recenter", "wz-autostart", "wz-done"):
            self.assertIn(f'id="{step}"', h)

    def test_wizard_shown_until_done(self):
        js = html()
        self.assertRegex(js, r"wizard_done[^\n]*go\(\"welcome\"\)")
        self.assertIn('"wizard_done": False', open(os.path.join(ROOT, "airpods_mouse_controller.py"),
                                                    encoding="utf-8").read())
        self.assertFalse(m.DEFAULTS["wizard_done"])

    def test_wizard_shows_live_connection(self):
        """While connected the server status text is empty: the wizard must show the live state
        like the sidebar does, not "—" (seen on Bazzite with AirPods streaming at 99 Hz)."""
        h = html()
        fn = h[h.index("function renderWizard()"):h.index("\n", h.index("function renderWizard()") + 60) + 120]
        self.assertIn("tm.connected&&tm.live", fn)
        self.assertRegex(h, r'setText\(\$\("#side-status"\),[^\n]*\n\s*renderWizard\(\);')   # updated with telemetry
        m_ = re.search(r"function renderWizard\(\)\{[\s\S]*?\}\n", h)
        self.assertIn('t("js.status.live")', m_.group(0))

    def test_wizard_done_persisted(self):
        d = tempfile.mkdtemp()
        cfg = os.path.join(d, "config.json")
        with open(cfg, "w") as f:
            f.write("{}")
        ui = web.WebUI(types.SimpleNamespace(cfg={"ui_lang": "en"}), cfg, port=0)
        ui.start()
        base = f"http://127.0.0.1:{ui.server.server_address[1]}"
        req = urllib.request.Request(base + "/api/wizard", method="POST", data=b'{"done": true}',
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req).read()
        ui.server.shutdown()
        with open(cfg) as f:
            self.assertTrue(json.load(f)["wizard_done"])
        self.assertTrue(ui.ctl.cfg["wizard_done"])
        shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
