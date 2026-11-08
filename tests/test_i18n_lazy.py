# SPDX-License-Identifier: Apache-2.0
"""Texts stored on the server follow a language switch: they are translated when served.

Run:  python -m unittest tests.test_i18n_lazy -v
"""
import inspect
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import i18n  # noqa: E402


class LazyTextTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_lang("en")

    def test_lazy_text_follows_current_language(self):
        msg = i18n.tr_lazy("lang.auto")
        i18n.set_lang("fr")
        self.assertEqual(str(msg), "Automatique")
        self.assertEqual(f"[{msg}]", "[Automatique]")
        i18n.set_lang("en")
        self.assertEqual(str(msg), "Automatic")

    def test_lazy_variables_are_rendered_late_too(self):
        msg = i18n.tr_lazy("srv.status.searching", error=i18n.tr_lazy("lang.auto"))
        i18n.set_lang("fr")
        self.assertIn("Automatique", str(msg))

    def test_lazy_compares_like_its_text(self):
        i18n.set_lang("fr")
        self.assertEqual(i18n.tr_lazy("lang.auto"), "Automatique")
        self.assertEqual(i18n.tr_lazy("lang.auto"), i18n.tr("lang.auto"))

    def test_encode_json_renders_lazy_in_current_language(self):
        payload = {"status": i18n.tr_lazy("lang.auto"), "n": 1}
        i18n.set_lang("fr")
        self.assertEqual(json.loads(i18n.encode_json(payload)), {"status": "Automatique", "n": 1})

    def test_encode_json_still_rejects_unknown_objects(self):
        with self.assertRaises(TypeError):
            i18n.encode_json({"x": object()})

    def test_web_serializers_use_encode_json(self):
        import mousepods_web as web
        src = inspect.getsource(web.WebUI.start)
        self.assertGreaterEqual(src.count("i18n.encode_json("), 2)   # _json and the SSE stream

    def test_lab_steps_follow_language(self):
        import labo
        steps = labo.build_protocol("mouvements", reps=1, seed=1)
        i18n.set_lang("fr")
        de = str(steps[0]["title"])
        i18n.set_lang("en")
        en = str(steps[0]["title"])
        self.assertNotEqual(de, en)
        i18n.set_lang("fr")
        self.assertTrue(all(isinstance(str(s["title"]), str) for s in steps))

    def test_web_status_follows_language(self):
        import types
        import mousepods_web as web
        ui = web.WebUI(types.SimpleNamespace(cfg={"ui_lang": "auto"}), "/tmp/unused.json", port=0)
        i18n.set_lang("fr")
        de = json.loads(i18n.encode_json({"s": ui.status}))["s"]
        i18n.set_lang("en")
        en = json.loads(i18n.encode_json({"s": ui.status}))["s"]
        self.assertNotEqual(de, en)


if __name__ == "__main__":
    unittest.main()
