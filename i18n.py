# SPDX-License-Identifier: Apache-2.0
"""UI translations: language resolution and string tables (ui/i18n/<lang>.json).

English is the source language; other languages fall back to English per key.
"""
import json
import logging
import os

HERE = os.path.dirname(os.path.abspath(__file__))
STRINGS_DIR = os.path.join(HERE, "ui", "i18n")
SUPPORTED = ("en", "fr")
SETTINGS = ("auto",) + SUPPORTED
DEFAULT = "en"

log = logging.getLogger(__name__)
_CACHE = {}
_current = DEFAULT


class _KeepMissing(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def clear_cache():
    _CACHE.clear()


def env_lang(environ=None):
    """System UI language: first entry of LANGUAGE (gettext priority list), else LC_ALL,
    LC_MESSAGES, LANG (first non-empty), or None."""
    env = os.environ if environ is None else environ
    first = (env.get("LANGUAGE") or "").split(":")[0]
    if first:
        return first
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        if env.get(name):
            return env[name]
    return None


def resolve_lang(setting, env_value):
    """Saved setting if supported, else the system language if supported, else English."""
    if setting in SUPPORTED:
        return setting
    code = (env_value or "")[:2].lower()
    return code if code in SUPPORTED else DEFAULT


def _read(lang):
    path = os.path.join(STRINGS_DIR, f"{lang}.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        log.warning("cannot read translations %s: %s", path, e)
        return {}


def load_strings(lang):
    """English strings overlaid with `lang`; cached."""
    if lang not in _CACHE:
        strings = dict(_read(DEFAULT))
        if lang != DEFAULT:
            strings.update(_read(lang))
        _CACHE[lang] = strings
    return _CACHE[lang]


def set_lang(lang):
    """Language used by tr() when none is given (texts the server builds for the UI)."""
    global _current
    _current = lang if lang in SUPPORTED else DEFAULT


def get_lang():
    return _current


def setting_from_file(path):
    """The saved "ui_lang" of a MousePods config file, "auto" if missing or unreadable."""
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f).get("ui_lang", "auto")
    except (OSError, ValueError, AttributeError):
        return "auto"
    return value if value in SETTINGS else "auto"


def apply_config(cfg, env_value=None):
    """Current language from the controller config ("ui_lang") and the system language."""
    set_lang(resolve_lang((cfg or {}).get("ui_lang", "auto"), env_lang() if env_value is None else env_value))
    return _current


def tr(key, lang=None, **variables):
    text = load_strings(lang or _current).get(key)
    if text is None:
        log.debug("missing translation key %s", key)
        return key
    if not variables:
        return text
    try:
        return text.format_map(_KeepMissing(variables))
    except (ValueError, IndexError, KeyError, AttributeError) as e:
        log.warning("malformed translation %s: %s", key, e)
        return text


def i18n_payload(cfg, env_value):
    setting = cfg.get("ui_lang", "auto")
    lang = resolve_lang(setting, env_value)
    return {"lang": lang, "setting": setting, "strings": load_strings(lang)}


class LazyText:
    """A translation rendered when it is turned into a string, in the language current at
    that moment. Store these for texts that stay on screen (statuses, prompts, results), so a
    language switch reaches them without regenerating anything."""
    __slots__ = ("key", "variables")

    def __init__(self, key, variables):
        self.key, self.variables = key, variables

    def __str__(self):
        return tr(self.key, None, **{k: str(v) if isinstance(v, LazyText) else v
                                     for k, v in self.variables.items()})

    def __format__(self, spec):
        return format(str(self), spec)

    def __eq__(self, other):
        return str(self) == str(other) if isinstance(other, (str, LazyText)) else NotImplemented

    def __hash__(self):
        return hash((self.key, tuple(sorted(self.variables.items(), key=lambda kv: kv[0]))))

    def __repr__(self):
        return f"LazyText({self.key!r})"

    def __add__(self, other):
        return str(self) + other

    def __radd__(self, other):
        return other + str(self)


def tr_lazy(key, **variables):
    return LazyText(key, variables)


def _json_default(obj):
    if isinstance(obj, LazyText):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def encode_json(obj, **kwargs):
    """json.dumps that renders LazyText in the current language."""
    kwargs.setdefault("ensure_ascii", False)
    return json.dumps(obj, default=_json_default, **kwargs)

