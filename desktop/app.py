# SPDX-License-Identifier: Apache-2.0
"""MousePods desktop app: the web UI in its own window, a tray icon, and the controller kept
running in the background (closing the window only hides it).

    mousepods-app               start (or show the running instance)
    mousepods-app --background  start hidden (autostart at login)
"""
import json
import logging
import os
import sys
import threading
import urllib.parse
import urllib.request

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
from gi.repository import Gdk, Gio, GLib, Gtk, WebKit  # noqa: E402

import i18n  # noqa: E402
from desktop import controller, overlay, renderer  # noqa: E402

# GL renderer instead of Vulkan (see desktop/renderer.py). GTK reads GSK_RENDERER when it creates
# the first window's renderer, so setting it here, before any window, is early enough.
renderer.choose(os.environ, (Gtk.get_major_version(), Gtk.get_minor_version()))

APP_ID = "io.github.starman_tech.MousePods"
PORT = 8737
log = logging.getLogger("mousepods-app")


def data_dirs():
    """Config and log locations: XDG dirs (inside the Flatpak they map to ~/.var/app/<id>/)."""
    cfg_home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    state_home = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    cfg_dir, state_dir = os.path.join(cfg_home, "mousepods"), os.path.join(state_home, "mousepods")
    os.makedirs(cfg_dir, exist_ok=True)
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(cfg_dir, "config.json"), os.path.join(state_dir, "controller.log")


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.win = None
        self.proc = None
        self.tray = None
        self.refused = False
        self.error = None                             # (kind, detail) shown instead of the UI
        self.view = None
        self.overlays = []                            # calibration overlay windows, one per monitor
        self._closing_overlays = False
        self.cfg_path, self.log_path = data_dirs()

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.hold()                                   # keep running without a window
        i18n.apply_config({"ui_lang": i18n.setting_from_file(self.cfg_path)})
        try:
            self.proc = controller.ensure_running(self.cfg_path, PORT, self.log_path)
        except controller.ForeignServer as e:
            log.error("%s - not opening it in the MousePods window", e)
            self.refused = True
            self.error = ("foreign_server", f"127.0.0.1:{PORT}")
            return
        if controller.ping(PORT) != "ours":
            log.error("controller did not start, see %s", self.log_path)
            self.error = ("controller_failed", self.log_path)
            return
        try:
            from desktop.tray import Tray
            self.tray = Tray(self.on_action, self.cfg_path)
        except Exception as e:  # noqa: BLE001  (no session bus / watcher: run without tray)
            log.warning("tray unavailable: %s", e)

    def do_command_line(self, cmdline):
        if "--background" not in cmdline.get_arguments()[1:]:
            self.show_window()
        return 0

    def do_activate(self):
        self.show_window()

    def show_window(self):
        if self.win is None:
            self.win = Gtk.ApplicationWindow(application=self, title="MousePods")
            self.win.set_default_size(1280, 860)
            view = self._web_view()
            self.view = view
            if self.error:
                from desktop.errors import error_page
                view.load_html(error_page(*self.error), None)      # never load a foreign server
            else:
                view.load_uri(f"http://127.0.0.1:{PORT}/")
            self.win.set_child(view)
            self.win.connect("close-request", self._close)
        self.win.present()

    # ---- page <-> app messages (calibration overlay, see desktop/overlay.py)
    def _web_view(self):
        ucm = WebKit.UserContentManager()
        ucm.register_script_message_handler("mousepods", None)
        ucm.connect("script-message-received::mousepods", self._on_message)
        view = WebKit.WebView(user_content_manager=ucm)
        view.connect("create", self._on_create)
        return view

    def _on_create(self, view, action):
        # links with target="_blank": our own pages (e.g. the /demo cockpit) in an app window,
        # anything else in the system browser
        uri = action.get_request().get_uri() or ""
        # the 360° player embeds YouTube, which the app's WebKit cannot play: it goes to the browser
        browser_only = urllib.parse.urlsplit(uri).path.startswith("/vr")
        if overlay.is_own_page(uri, PORT) and not browser_only:
            win = Gtk.Window(application=self, title="MousePods")
            win.set_default_size(1280, 800)
            new = WebKit.WebView(related_view=view, user_content_manager=view.get_user_content_manager())
            win.set_child(new)
            win.present()
            return new
        if uri.startswith(("https://", "http://")):
            Gtk.UriLauncher.new(uri).launch(self.win, None, None, None)
        return None

    def _on_message(self, _ucm, value):
        msg = overlay.parse_message(value.to_json(0))
        if msg is None or self.error:
            return
        cmd = msg["cmd"]
        if cmd == "monitors":
            mons = self._monitors()
            present = [m["connector"] for m in mons]
            self._reply({"cmd": "monitors", "list": mons, "selection": overlay.resolve_monitors(msg["saved"], present)})
        elif cmd == "flash":
            self._flash(msg["connector"])
        elif cmd == "overlay":
            self._open_overlays(msg["monitors"])
        elif cmd == "overlay_close":
            self._close_overlays()

    def _reply(self, obj):
        if self.view is not None:
            js = "window.mpDesktop&&mpDesktop.onMessage(" + json.dumps(obj) + ")"
            self.view.evaluate_javascript(js, -1, None, None, None, None, None)

    @staticmethod
    def _gdk_monitors():
        model = Gdk.Display.get_default().get_monitors()
        return [model.get_item(i) for i in range(model.get_n_items())]

    def _monitors(self):
        out = []
        for i, m in enumerate(self._gdk_monitors()):
            g = m.get_geometry()
            out.append({"connector": overlay.connector_name(m.get_connector(), i), "model": m.get_model() or "",
                        "width": g.width, "height": g.height})
        return out

    def _monitor(self, connector):
        return next((m for i, m in enumerate(self._gdk_monitors())
                     if overlay.connector_name(m.get_connector(), i) == connector), None)

    def _flash(self, connector):
        mon = self._monitor(connector)
        if mon is None:
            return
        win = Gtk.Window(application=self, decorated=False)
        label = Gtk.Label()
        label.set_markup(f"<span size='64000' weight='bold'>{GLib.markup_escape_text(connector)}</span>\n"
                         f"<span size='24000'>{GLib.markup_escape_text(mon.get_model() or '')}</span>")
        label.set_justify(Gtk.Justification.CENTER)
        win.set_child(label)
        win.fullscreen_on_monitor(mon)
        win.present()
        GLib.timeout_add(1500, lambda: (win.destroy(), GLib.SOURCE_REMOVE)[1])

    def _open_overlays(self, monitors):
        self._close_overlays()
        present = [mon["connector"] for mon in self._monitors()]
        for connector, roles in overlay.plan_overlays(monitors, present):
            mon = self._monitor(connector) if connector else None
            win = Gtk.Window(application=self, decorated=False, title="MousePods")
            view = self._web_view()
            view.load_uri(overlay.overlay_url(PORT, roles))
            win.set_child(view)
            win.connect("close-request", self._overlay_closed)
            if mon is not None:
                win.fullscreen_on_monitor(mon)
            else:
                win.fullscreen()                  # monitor unknown: the one the app is on
            win.present()
            self.overlays.append(win)

    def _overlay_closed(self, _win):
        # closed some other way than through the page (Alt+F4...): the calibration is over
        if not self._closing_overlays:
            threading.Thread(target=self._cancel_calibration, daemon=True).start()
            GLib.idle_add(lambda: (self._close_overlays(), GLib.SOURCE_REMOVE)[1])
        return False

    @staticmethod
    def _cancel_calibration():
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/calibration", method="POST",
                                     data=b'{"action": "cancel"}', headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=2).read()
        except OSError as e:
            log.warning("calibration cancel failed: %s", e)

    def _close_overlays(self):
        wins, self.overlays = self.overlays, []
        self._closing_overlays = True
        try:
            for w in wins:
                w.destroy()
        finally:
            self._closing_overlays = False
        if wins and self.win is not None:
            self.win.present()

    def _close(self, win):
        if self.error:                                # nothing runs in the background: really quit
            self.release()
            self.quit()
            return False
        return self._hide(win)

    def _hide(self, win):
        win.set_visible(False)
        return True                                   # keep the window object; do not quit

    def on_action(self, action):
        if action == "open":
            self.show_window()
        elif action in ("recenter", "toggle"):
            import airpods_mouse_controller as m
            m.send_command(action)
        elif action == "quit":
            import airpods_mouse_controller as m
            m.send_command("quit")
            if self.proc is not None:
                try:
                    self.proc.wait(timeout=3)
                except Exception:  # noqa: BLE001  (still running: stop it)
                    self.proc.terminate()
            self.release()
            self.quit()
        return GLib.SOURCE_REMOVE


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return App().run(argv if argv is not None else sys.argv)
