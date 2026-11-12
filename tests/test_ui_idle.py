# SPDX-License-Identifier: Apache-2.0
"""Idle cost of the UI: the live stream stops while the page is hidden, telemetry only touches the
DOM when a value changes, and the desktop app picks GTK's GL renderer unless told otherwise.

Run:  python -m unittest tests.test_ui_idle -v
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def function_body(src, name):
    start = src.index("function %s(" % name)
    depth, i = 0, src.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        if depth == 0:
            return src[start:i + 1]
        i += 1


class StreamVisibilityTests(unittest.TestCase):
    def test_stream_closes_when_hidden_and_reopens(self):
        h = read("ui", "index.html")
        self.assertRegex(h, r'addEventListener\("visibilitychange",\(\)=>\{document\.hidden\?disconnect\(\):connect\(\)\}\)')
        conn = function_body(h, "connect")
        self.assertIn("document.hidden)return", conn)        # never opens a stream for a hidden page
        self.assertIn("if(ES||", conn)                        # never two streams at once
        self.assertIn("ES.close()", function_body(h, "disconnect"))

    def test_only_one_stream_constructor(self):
        self.assertEqual(read("ui", "index.html").count('new EventSource("/api/stream")'), 1)


class TelemetryWritesTests(unittest.TestCase):
    def test_render_telemetry_writes_through_change_checks(self):
        body = function_body(read("ui", "index.html"), "renderTelemetry")
        # sidebar and overview text/classes/html go through setText/setClass/setHtml
        for el in ("side-dot", "side-status", "side-rate", "ov-state", "ov-state-sub", "ov-toggle",
                   "ov-rate", "ov-gaps", "ov-src-hint", "ov-bat"):
            self.assertNotRegex(body, r'\$\("#%s"\)\.(textContent|className|innerHTML)=' % el, el)
        self.assertRegex(body, r"now-rateShownAt>=1000")

    def test_helpers_skip_unchanged_values(self):
        h = read("ui", "index.html")
        self.assertIn("function setText(el,v){if(el&&el.textContent!==v)el.textContent=v}", h)
        self.assertIn("function setClass(el,v){if(el&&el.className!==v)el.className=v}", h)
        self.assertIn("function setHtml(el,v){if(el&&el._html!==v){el._html=v;el.innerHTML=v}}", h)


class RendererTests(unittest.TestCase):
    def test_gl_renderer_name_follows_gtk_version(self):
        from desktop.renderer import gl_renderer
        self.assertEqual(gl_renderer((4, 14)), "ngl")
        self.assertEqual(gl_renderer((4, 17)), "ngl")
        self.assertEqual(gl_renderer((4, 18)), "gl")     # renamed; "ngl" only warns from here on
        self.assertEqual(gl_renderer((4, 22)), "gl")

    def test_users_own_choice_wins(self):
        from desktop.renderer import choose
        env = {"GSK_RENDERER": "vulkan"}
        self.assertEqual(choose(env, (4, 22)), "vulkan")
        env = {}
        self.assertEqual(choose(env, (4, 22)), "gl")
        self.assertEqual(env["GSK_RENDERER"], "gl")
        self.assertEqual(choose({"GSK_RENDERER": ""}, (4, 16)), "ngl")   # empty means unset

    def test_app_chooses_before_any_window(self):
        src = read("desktop", "app.py")
        call = src.index("renderer.choose(os.environ")
        self.assertLess(src.index("from gi.repository import"), call)
        self.assertLess(call, src.index("class App("))
        self.assertNotRegex(src, r'os\.environ\["GSK_RENDERER"\]\s*=')


if __name__ == "__main__":
    unittest.main()
