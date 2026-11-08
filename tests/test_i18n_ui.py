# SPDX-License-Identifier: Apache-2.0
"""Static checks that ui/index.html is fully translatable (no browser needed).

Run:  python -m unittest tests.test_i18n_ui -v
"""
import json
import os
import re
import unittest
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "ui", "index.html")
EN = os.path.join(ROOT, "ui", "i18n", "en.json")

ALLOWED_WORDS = {"AirPods", "OpenTrack", "Garmin", "Hz", "ms", "UDP", "MousePods", "X", "Y", "Z"}
NEUTRAL = re.compile(r"^[0-9\s.,:;/%°±+\-–—·×()\[\]]*$")
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


def load_en():
    with open(EN, encoding="utf-8") as f:
        return json.load(f)


def read_ui():
    with open(UI, encoding="utf-8") as f:
        return f.read()


FRENCH = re.compile(r"[éèêàâçùûôîœÉÈÀÊ]|\b(de|la|le|les|des|du|en|pas|une|un|est|avec|pour|sur|au|aux|ou|et|"
                    r"encore|aucun|aucune|attente|droite|gauche|haut|bas|reprendre|lancée|fermée)\b", re.I)

# French values sent by the backend that the script only compares, never shows
BACKEND_VALUES = set()


def script_source():
    html = read_ui()
    return html[html.index("<script>") + 8:html.index("</script>")]


def js_strings(js):
    """Yield (text, is_t_key) for every string literal / template static part; skips comments."""
    i, n = 0, len(js)
    out = []
    while i < n:
        c = js[i]
        if js.startswith("//", i):
            i = js.find("\n", i)
            i = n if i < 0 else i
            continue
        if js.startswith("/*", i):
            i = js.find("*/", i) + 2
            continue
        if c in "\"'":
            j, buf = i + 1, []
            while j < n and js[j] != c:
                if js[j] == "\\":
                    buf.append(js[j:j + 2])
                    j += 2
                    continue
                buf.append(js[j])
                j += 1
            # a bare t(...) call only: not post(, toast(, split(, obj.t( ...
            is_key = js[max(0, i - 2):i] == "t(" and not (i >= 3 and (js[i - 3].isalnum() or js[i - 3] in "_$."))
            out.append(("".join(buf), is_key))
            i = j + 1
            continue
        if c == "`":
            j, buf = i + 1, []
            while j < n and js[j] != "`":
                if js[j] == "\\":
                    buf.append(js[j:j + 2])
                    j += 2
                    continue
                if js.startswith("${", j):
                    out.append(("".join(buf), False))
                    buf = []
                    depth, j = 1, j + 2
                    inner_start = j
                    while j < n and depth:
                        if js[j] == "{":
                            depth += 1
                        elif js[j] == "}":
                            depth -= 1
                        elif js[j] == "`":
                            k = js.find("`", j + 1)
                            j = k if k > 0 else j
                        j += 1
                    out.extend(js_strings(js[inner_start:j - 1]))
                    continue
                buf.append(js[j])
                j += 1
            out.append(("".join(buf), False))
            i = j + 1
            continue
        i += 1
    return out


class _Scan(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []          # [(tag, translated)]
        self.keys = []
        self.untranslated = []
        self.in_body = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "body":
            self.in_body = True
        for name in ("data-i18n", "data-i18n-html"):
            if name in a:
                self.keys.append(a[name])
        if "data-i18n-attr" in a:
            for pair in a["data-i18n-attr"].split(";"):
                if "=" in pair:
                    self.keys.append(pair.split("=", 1)[1].strip())
        if tag in VOID:
            return
        parent_translated = any(t for _, t in self.stack)
        marked = "data-i18n" in a or "data-i18n-html" in a
        self.stack.append((tag, marked or parent_translated and tag not in ("script", "style")))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if not self.in_body:
            return
        if any(t in ("script", "style") for t, _ in self.stack):
            return
        text = data.strip()
        if not text or NEUTRAL.match(text) or text in ALLOWED_WORDS:
            return
        if any(tr for _, tr in self.stack):
            return
        self.untranslated.append(text)


def scan():
    s = _Scan()
    s.feed(read_ui())
    return s


class UiStaticTests(unittest.TestCase):
    def test_every_data_i18n_key_exists(self):
        en = load_en()
        missing = sorted({k for k in scan().keys if k not in en})
        self.assertEqual(missing, [])

    def test_text_keys_have_no_html_entities(self):
        # data-i18n sets textContent, so "&gt;" would show literally
        s = _Scan()
        s.text_keys = []
        orig = s.handle_starttag

        def grab(tag, attrs):
            a = dict(attrs)
            if "data-i18n" in a:
                s.text_keys.append(a["data-i18n"])
            orig(tag, attrs)
        s.handle_starttag = grab
        s.feed(read_ui())
        bad = []
        for lang in ("en", "fr"):
            with open(os.path.join(ROOT, "ui", "i18n", f"{lang}.json"), encoding="utf-8") as f:
                table = json.load(f)
            bad += [(lang, k) for k in s.text_keys if re.search(r"&#?\w+;", table.get(k, ""))]
        self.assertEqual(bad, [])

    def test_no_untranslated_text_nodes(self):
        self.assertEqual(scan().untranslated, [])

    def test_script_t_keys_exist(self):
        en = load_en()
        keys = {txt for txt, is_key in js_strings(script_source()) if is_key}
        self.assertTrue(keys, "no t() calls found")
        self.assertEqual(sorted(k for k in keys if k not in en), [])

    def test_script_has_no_french_literals(self):
        bad = []
        for txt, is_key in js_strings(script_source()):
            if is_key:
                continue
            txt = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), txt)
            if txt in BACKEND_VALUES:
                continue
            visible = re.sub(r"<[^>]*>", " ", txt)
            if re.search(r"[éèêàâçùûôîœÉÈÀÊ]", visible) or (" " in visible.strip() and FRENCH.search(visible)) \
                    or visible.strip() in {"Reprendre", "Enregistré"}:
                bad.append(txt)
        self.assertEqual(bad, [])

    def test_language_load_clears_render_caches(self):
        js = script_source()
        body = js[js.index("async function loadI18n"):]
        body = body[:body.index("\n}") if "\n}" in body else len(body)]
        self.assertIn("lbLastRes=null", body.split("catch")[0])

    def test_lab_phase_ids_are_mapped_to_gesture_names(self):
        import sys
        sys.path.insert(0, ROOT)
        import labo
        js = script_source()
        m = re.search(r"const LB_PHASE=\{([^}]*)\}", js)
        self.assertIsNotNone(m, "LB_PHASE map missing")
        mapping = dict(re.findall(r'(\w+):"(\w+)"', m.group(1)))
        for phase, gestures, _cue in labo.MOVES:
            self.assertEqual(mapping.get(phase), gestures[0], phase)
        self.assertNotIn("<td>${ph}</td>", js)


if __name__ == "__main__":
    unittest.main()
