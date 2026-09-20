#!/usr/bin/env python3
"""Notemark - a Notion-style Markdown editor for Linux.

The native chrome (header bar, dialogs, zoom, theme, shortcuts) is GTK 3. The
document itself lives in a WebKitGTK view loading ``ui/index.html``: in page
mode that page is directly editable and is converted back to Markdown, while
source mode edits the Markdown text itself. See ``ui/app.js``.
"""
import json
import os
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gdk, Gio, GLib, Gtk, WebKit2  # noqa: E402

APP_ID = "com.forhadkhan.Notemark"
APP_NAME = "Notemark"
VERSION = "0.1.0"

HERE = os.path.dirname(os.path.abspath(__file__))
UI_INDEX = os.path.join(HERE, "ui", "index.html")
CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "notemark")
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.json")

ZOOM_STEPS = [0.5, 0.67, 0.75, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]
MODES = ("page", "source", "split")
DEFAULT_SETTINGS = {"theme": "light", "zoom": 1.0, "mode": "page"}


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("theme") in ("light", "dark"):
            settings["theme"] = data["theme"]
        if data.get("mode") in MODES:
            settings["mode"] = data["mode"]
        zoom = float(data.get("zoom", 1.0))
        if ZOOM_STEPS[0] <= zoom <= ZOOM_STEPS[-1]:
            settings["zoom"] = zoom
    except (OSError, ValueError, TypeError):
        pass
    return settings


def save_settings(settings):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
            json.dump(settings, fh, indent=2)
    except OSError:
        pass


def system_prefers_dark():
    """Best-effort detection of the desktop's dark preference on first run."""
    try:
        source = Gio.SettingsSchemaSource.get_default()
        schema = source.lookup("org.gnome.desktop.interface", True) if source else None
        if schema and schema.has_key("color-scheme"):
            gs = Gio.Settings.new("org.gnome.desktop.interface")
            return gs.get_string("color-scheme") == "prefer-dark"
    except Exception:
        pass
    gtk_settings = Gtk.Settings.get_default()
    theme = (gtk_settings.props.gtk_theme_name or "").lower()
    return gtk_settings.props.gtk_application_prefer_dark_theme or "dark" in theme


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #
class NotemarkWindow(Gtk.ApplicationWindow):
    def __init__(self, app, settings):
        super().__init__(application=app, title=APP_NAME)
        self.settings = settings
        self.file_path = None
        self.dirty = False
        self.web_ready = False
        self._pending_text = None
        self._pending_action = None

        self.set_default_size(1080, 760)
        self.set_icon_name("notemark")

        self._build_header()
        self._build_body()
        self.connect("delete-event", self._on_delete)

        self.apply_theme(self.settings["theme"], persist=False)
        self.apply_zoom(self.settings["zoom"], persist=False)

    # ----- UI construction ------------------------------------------------- #
    def _build_header(self):
        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        hb.props.title = APP_NAME
        self.set_titlebar(hb)
        self.header = hb

        # File actions (left)
        file_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        file_box.get_style_context().add_class("linked")
        for icon, tip, action in (
            ("document-new-symbolic", "New (Ctrl+N)", "app.new"),
            ("document-open-symbolic", "Open… (Ctrl+O)", "app.open"),
            ("document-save-symbolic", "Save (Ctrl+S)", "app.save"),
        ):
            btn = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
            btn.set_tooltip_text(tip)
            btn.set_action_name(action)
            file_box.pack_start(btn, False, False, 0)
        hb.pack_start(file_box)

        # Mode switcher (left, after file actions)
        mode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        mode_box.get_style_context().add_class("linked")
        self.mode_buttons = {}
        group = None
        for mode, label, tip in (
            ("page", "Page", "Notion-style page: type directly into the rendered document (Ctrl+1)"),
            ("source", "Source", "Plain Markdown source (Ctrl+2)"),
            ("split", "Split", "Source beside the page (Ctrl+3)"),
        ):
            btn = Gtk.RadioButton.new_with_label_from_widget(group, label)
            btn.set_mode(False)  # look like a toggle button, not a radio
            btn.set_tooltip_text(tip)
            btn.connect("toggled", self._on_mode_toggled, mode)
            mode_box.pack_start(btn, False, False, 0)
            self.mode_buttons[mode] = btn
            group = group or btn
        hb.pack_start(mode_box)

        # Menu (right)
        menu = Gio.Menu()
        menu.append("Save As…", "app.save-as")
        menu.append("Export HTML…", "app.export-html")
        section = Gio.Menu()
        section.append("Reset Zoom", "app.zoom-reset")
        section.append("Keyboard Shortcuts", "app.shortcuts")
        section.append("About Notemark", "app.about")
        menu.append_section(None, section)
        menu_btn = Gtk.MenuButton()
        menu_btn.set_image(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        menu_btn.set_menu_model(menu)
        menu_btn.set_tooltip_text("Menu")
        hb.pack_end(menu_btn)

        # Theme toggle (right)
        self.theme_btn = Gtk.Button()
        self.theme_btn.set_action_name("app.toggle-theme")
        hb.pack_end(self.theme_btn)

        # Zoom controls (right)
        zoom_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        zoom_box.get_style_context().add_class("linked")
        zoom_out = Gtk.Button.new_from_icon_name("zoom-out-symbolic", Gtk.IconSize.BUTTON)
        zoom_out.set_tooltip_text("Zoom out (Ctrl+-)")
        zoom_out.set_action_name("app.zoom-out")
        self.zoom_label_btn = Gtk.Button.new_with_label("100%")
        self.zoom_label_btn.set_tooltip_text("Reset zoom (Ctrl+0)")
        self.zoom_label_btn.set_action_name("app.zoom-reset")
        self.zoom_label_btn.set_size_request(64, -1)
        zoom_in = Gtk.Button.new_from_icon_name("zoom-in-symbolic", Gtk.IconSize.BUTTON)
        zoom_in.set_tooltip_text("Zoom in (Ctrl++)")
        zoom_in.set_action_name("app.zoom-in")
        for b in (zoom_out, self.zoom_label_btn, zoom_in):
            zoom_box.pack_start(b, False, False, 0)
        hb.pack_end(zoom_box)

    def _build_body(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(vbox)

        ucm = WebKit2.UserContentManager()
        ucm.register_script_message_handler("app")
        ucm.connect("script-message-received::app", self._on_script_message)

        self.web = WebKit2.WebView.new_with_user_content_manager(ucm)
        ws = self.web.get_settings()
        debug = bool(os.environ.get("NOTEMARK_DEBUG"))
        ws.set_enable_developer_extras(debug)
        ws.set_enable_javascript(True)
        ws.set_allow_file_access_from_file_urls(True)
        ws.set_enable_write_console_messages_to_stdout(debug)
        self.web.set_zoom_level(1.0)
        self.web.connect("load-changed", self._on_load_changed)
        self.web.connect("scroll-event", self._on_scroll)
        self.web.connect("decide-policy", self._on_decide_policy)
        self.web.connect("context-menu", lambda *a: not debug)
        vbox.pack_start(self.web, True, True, 0)

        # Status bar
        status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        status.set_border_width(4)
        status.get_style_context().add_class("notemark-status")
        self.status_left = Gtk.Label(label="")
        self.status_left.set_xalign(0.0)
        self.status_left.get_style_context().add_class("dim-label")
        self.status_left.set_ellipsize(3)  # Pango.EllipsizeMode.END
        self.status_right = Gtk.Label(label="0 words")
        self.status_right.get_style_context().add_class("dim-label")
        status.pack_start(self.status_left, True, True, 8)
        status.pack_end(self.status_right, False, False, 8)
        vbox.pack_start(status, False, False, 0)

        self.web.load_uri(GLib.filename_to_uri(UI_INDEX, None))

    # ----- WebKit plumbing ------------------------------------------------- #
    def run_js(self, script):
        self.web.evaluate_javascript(script, -1, None, None, None, None, None)

    def call_js(self, func, *args):
        payload = ", ".join(json.dumps(a) for a in args)
        self.run_js("window.Notemark && Notemark.%s(%s);" % (func, payload))

    def _on_load_changed(self, web, event):
        if event == WebKit2.LoadEvent.FINISHED:
            self.web_ready = True
            self.call_js("init", {
                "theme": self.settings["theme"],
                "mode": self.settings["mode"],
            })
            if self._pending_text is not None:
                self.call_js("load", self._pending_text)
                self._pending_text = None

    def _on_script_message(self, ucm, result):
        try:
            value = result.get_js_value()
            msg = json.loads(value.to_string())
        except Exception:
            return
        kind = msg.get("type")
        if kind == "changed":
            self.dirty = bool(msg.get("dirty"))
            self._update_status(msg.get("words", 0), msg.get("chars", 0))
            self._update_title()
        elif kind == "link":
            href = msg.get("href", "")
            if href.startswith(("http://", "https://", "mailto:")):
                Gtk.show_uri_on_window(self, href, Gdk.CURRENT_TIME)
        elif kind == "content":
            self._deliver_content(msg.get("text", ""))
        elif kind == "mode":
            mode = msg.get("mode")
            if mode in MODES:
                self._set_mode_buttons(mode)

    def _on_decide_policy(self, web, decision, dtype):
        # Keep the view on our own index page; external navigation is refused.
        if dtype == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri()
            if uri and not uri.startswith("file://") and uri != "about:blank":
                decision.ignore()
                return True
        return False

    def _on_scroll(self, web, event):
        if not (event.state & Gdk.ModifierType.CONTROL_MASK):
            return False
        direction = event.direction
        if direction == Gdk.ScrollDirection.SMOOTH:
            ok, dx, dy = event.get_scroll_deltas()
            if dy == 0:
                return True
            direction = Gdk.ScrollDirection.UP if dy < 0 else Gdk.ScrollDirection.DOWN
        if direction == Gdk.ScrollDirection.UP:
            self.zoom_step(+1)
        elif direction == Gdk.ScrollDirection.DOWN:
            self.zoom_step(-1)
        return True

    # ----- Content in / out ------------------------------------------------ #
    def set_text(self, text):
        if self.web_ready:
            self.call_js("load", text)
        else:
            self._pending_text = text

    def request_content(self, callback):
        """Ask the page for the current Markdown; callback(text) runs later."""
        self._pending_action = callback
        self.call_js("sendContent")

    def _deliver_content(self, text):
        cb, self._pending_action = self._pending_action, None
        if cb:
            cb(text)

    # ----- Title / status -------------------------------------------------- #
    def _update_title(self):
        name = os.path.basename(self.file_path) if self.file_path else "Untitled"
        mark = "• " if self.dirty else ""
        self.header.props.title = mark + name
        self.set_title("%s%s — %s" % (mark, name, APP_NAME))
        self.status_left.set_text(self.file_path or "Unsaved document")

    def _update_status(self, words, chars):
        self.status_right.set_text("{:,} words · {:,} characters".format(words, chars))

    # ----- Mode ------------------------------------------------------------ #
    def _on_mode_toggled(self, btn, mode):
        if btn.get_active() and self.settings["mode"] != mode:
            self.set_mode(mode)

    def _set_mode_buttons(self, mode):
        btn = self.mode_buttons[mode]
        if not btn.get_active():
            btn.set_active(True)

    def set_mode(self, mode):
        self.settings["mode"] = mode
        save_settings(self.settings)
        self._set_mode_buttons(mode)
        self.call_js("setMode", mode)

    def toggle_mode(self):
        self.set_mode("source" if self.settings["mode"] == "page" else "page")

    # ----- Theme ----------------------------------------------------------- #
    def apply_theme(self, theme, persist=True):
        dark = theme == "dark"
        self.settings["theme"] = theme
        Gtk.Settings.get_default().props.gtk_application_prefer_dark_theme = dark
        rgba = Gdk.RGBA()
        rgba.parse("#191919" if dark else "#ffffff")
        self.web.set_background_color(rgba)
        self.theme_btn.set_image(Gtk.Image.new_from_icon_name(
            "weather-clear-symbolic" if dark else "weather-clear-night-symbolic",
            Gtk.IconSize.BUTTON))
        self.theme_btn.set_tooltip_text(
            "Switch to light mode (Ctrl+Shift+L)" if dark else "Switch to dark mode (Ctrl+Shift+L)")
        self.call_js("setTheme", theme)
        if persist:
            save_settings(self.settings)

    def toggle_theme(self):
        self.apply_theme("light" if self.settings["theme"] == "dark" else "dark")

    # ----- Zoom ------------------------------------------------------------ #
    def apply_zoom(self, zoom, persist=True):
        zoom = max(ZOOM_STEPS[0], min(ZOOM_STEPS[-1], zoom))
        self.settings["zoom"] = zoom
        self.web.set_zoom_level(zoom)
        self.zoom_label_btn.set_label("%d%%" % round(zoom * 100))
        if persist:
            save_settings(self.settings)

    def zoom_step(self, direction):
        current = self.settings["zoom"]
        if direction > 0:
            nxt = next((z for z in ZOOM_STEPS if z > current + 1e-6), ZOOM_STEPS[-1])
        else:
            nxt = next((z for z in reversed(ZOOM_STEPS) if z < current - 1e-6), ZOOM_STEPS[0])
        self.apply_zoom(nxt)

    # ----- File operations ------------------------------------------------- #
    def new_document(self):
        def go():
            self.file_path = None
            self.dirty = False
            self.set_text("")
            self._update_title()
        self.confirm_discard(go)

    def open_dialog(self):
        def go():
            dlg = Gtk.FileChooserNative.new("Open Markdown", self, Gtk.FileChooserAction.OPEN, "_Open", "_Cancel")
            self._add_md_filters(dlg)
            if self.file_path:
                dlg.set_current_folder(os.path.dirname(self.file_path))
            if dlg.run() == Gtk.ResponseType.ACCEPT:
                self.open_path(dlg.get_filename())
            dlg.destroy()
        self.confirm_discard(go)

    def open_path(self, path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except UnicodeDecodeError:
            with open(path, "r", encoding="latin-1") as fh:
                text = fh.read()
        except OSError as exc:
            self.error("Could not open file", str(exc))
            return
        self.file_path = os.path.abspath(path)
        self.dirty = False
        self.set_text(text)
        self._update_title()

    def save(self, then=None, path=None):
        target = path or self.file_path
        if not target:
            self.save_as(then)
            return

        def write(text):
            try:
                with open(target, "w", encoding="utf-8") as fh:
                    fh.write(text)
            except OSError as exc:
                self.error("Could not save file", str(exc))
                return
            self.file_path = target
            self.dirty = False
            self.call_js("markClean")
            self._update_title()
            if then:
                then()
        self.request_content(write)

    def save_as(self, then=None):
        dlg = Gtk.FileChooserNative.new("Save Markdown", self, Gtk.FileChooserAction.SAVE, "_Save", "_Cancel")
        dlg.set_do_overwrite_confirmation(True)
        self._add_md_filters(dlg)
        if self.file_path:
            dlg.set_filename(self.file_path)
        else:
            dlg.set_current_name("Untitled.md")
        if dlg.run() == Gtk.ResponseType.ACCEPT:
            path = dlg.get_filename()
            if not os.path.splitext(path)[1]:
                path += ".md"
            dlg.destroy()
            self.save(then, path=path)
        else:
            dlg.destroy()

    def export_html(self):
        dlg = Gtk.FileChooserNative.new("Export HTML", self, Gtk.FileChooserAction.SAVE, "_Export", "_Cancel")
        dlg.set_do_overwrite_confirmation(True)
        base = os.path.splitext(os.path.basename(self.file_path))[0] if self.file_path else "Untitled"
        dlg.set_current_name(base + ".html")
        if self.file_path:
            dlg.set_current_folder(os.path.dirname(self.file_path))
        if dlg.run() != Gtk.ResponseType.ACCEPT:
            dlg.destroy()
            return
        path = dlg.get_filename()
        dlg.destroy()
        self._pending_action = lambda html: self._write_export(path, html)
        self.call_js("sendHtml", base)

    def _write_export(self, path, html):
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(html)
        except OSError as exc:
            self.error("Could not export", str(exc))

    @staticmethod
    def _add_md_filters(dlg):
        f = Gtk.FileFilter()
        f.set_name("Markdown files")
        for pat in ("*.md", "*.markdown", "*.mdown", "*.mkd", "*.txt"):
            f.add_pattern(pat)
        dlg.add_filter(f)
        allf = Gtk.FileFilter()
        allf.set_name("All files")
        allf.add_pattern("*")
        dlg.add_filter(allf)

    # ----- Dialogs --------------------------------------------------------- #
    def confirm_discard(self, proceed):
        """Run ``proceed`` now if clean, otherwise ask to save / discard first."""
        if not self.dirty:
            proceed()
            return
        name = os.path.basename(self.file_path) if self.file_path else "Untitled"
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                text="Save changes to “%s”?" % name)
        dlg.format_secondary_text("Your changes will be lost if you don't save them.")
        dlg.add_button("Discard", Gtk.ResponseType.REJECT)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dlg.add_button("Save", Gtk.ResponseType.ACCEPT)
        dlg.set_default_response(Gtk.ResponseType.ACCEPT)
        dlg.get_widget_for_response(Gtk.ResponseType.REJECT).get_style_context().add_class("destructive-action")
        resp = dlg.run()
        dlg.destroy()
        if resp == Gtk.ResponseType.REJECT:
            proceed()
        elif resp == Gtk.ResponseType.ACCEPT:
            self.save(then=proceed)

    def error(self, text, secondary):
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.ERROR,
                                buttons=Gtk.ButtonsType.OK, text=text)
        dlg.format_secondary_text(secondary)
        dlg.run()
        dlg.destroy()

    def _on_delete(self, *args):
        if not self.dirty:
            return False
        self.confirm_discard(self.destroy)
        return True  # we handle destruction ourselves after the dialog

    def show_shortcuts(self):
        rows = [
            ("Ctrl+N", "New document"), ("Ctrl+O", "Open"), ("Ctrl+S", "Save"),
            ("Ctrl+Shift+S", "Save as"), ("Ctrl+E", "Toggle page / source"),
            ("Ctrl+1 / 2 / 3", "Page / Source / Split"), ("Ctrl+Shift+L", "Toggle dark mode"),
            ("Ctrl++ / Ctrl+-", "Zoom in / out"), ("Ctrl+0", "Reset zoom"),
            ("Ctrl+Scroll", "Zoom"), ("# - 1. [] > ``` ---", "Type at line start, then space / Enter"),
            ("Enter on empty line", "Leave a list, quote or code block"),
            ("Ctrl+B / I / `", "Bold / italic / inline code"), ("Ctrl+K / Ctrl+Shift+X", "Link / strikethrough"),
            ("Tab / Shift+Tab", "Indent / outdent list item"), ("Ctrl+click", "Open a link"),
            ("Ctrl+Z / Ctrl+Shift+Z", "Undo / redo"), ("Ctrl+Q", "Quit"),
        ]
        dlg = Gtk.Dialog(title="Keyboard Shortcuts", transient_for=self, modal=True)
        dlg.add_button("Close", Gtk.ResponseType.CLOSE)
        grid = Gtk.Grid(column_spacing=24, row_spacing=6, border_width=18)
        for i, (keys, desc) in enumerate(rows):
            k = Gtk.Label(label=keys, xalign=0.0)
            k.get_style_context().add_class("monospace")
            grid.attach(k, 0, i, 1, 1)
            grid.attach(Gtk.Label(label=desc, xalign=0.0), 1, i, 1, 1)
        dlg.get_content_area().add(grid)
        dlg.show_all()
        dlg.run()
        dlg.destroy()

    def show_about(self):
        dlg = Gtk.AboutDialog(transient_for=self, modal=True)
        dlg.set_program_name(APP_NAME)
        dlg.set_version(VERSION)
        dlg.set_comments("A Notion-style Markdown editor.")
        dlg.set_logo_icon_name("notemark")
        dlg.set_license_type(Gtk.License.MIT_X11)
        dlg.set_website("https://github.com/forhadkhan/notemark")
        dlg.run()
        dlg.destroy()


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #
class NotemarkApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_OPEN | Gio.ApplicationFlags.NON_UNIQUE)
        self.window = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        GLib.set_application_name(APP_NAME)
        actions = {
            "new": (lambda: self.window.new_document(), ["<Primary>n"]),
            "open": (lambda: self.window.open_dialog(), ["<Primary>o"]),
            "save": (lambda: self.window.save(), ["<Primary>s"]),
            "save-as": (lambda: self.window.save_as(), ["<Primary><Shift>s"]),
            "export-html": (lambda: self.window.export_html(), []),
            "toggle-mode": (lambda: self.window.toggle_mode(), ["<Primary>e"]),
            "mode-page": (lambda: self.window.set_mode("page"), ["<Primary>1"]),
            "mode-source": (lambda: self.window.set_mode("source"), ["<Primary>2"]),
            "mode-split": (lambda: self.window.set_mode("split"), ["<Primary>3"]),
            "toggle-theme": (lambda: self.window.toggle_theme(), ["<Primary><Shift>l"]),
            "zoom-in": (lambda: self.window.zoom_step(+1), ["<Primary>plus", "<Primary>equal", "<Primary>KP_Add"]),
            "zoom-out": (lambda: self.window.zoom_step(-1), ["<Primary>minus", "<Primary>KP_Subtract"]),
            "zoom-reset": (lambda: self.window.apply_zoom(1.0), ["<Primary>0", "<Primary>KP_0"]),
            "shortcuts": (lambda: self.window.show_shortcuts(), ["<Primary>question"]),
            "about": (lambda: self.window.show_about(), []),
            "quit": (lambda: self.window.close(), ["<Primary>q"]),
        }
        for name, (fn, accels) in actions.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda a, p, fn=fn: fn())
            self.add_action(action)
            if accels:
                self.set_accels_for_action("app." + name, accels)

        css = Gtk.CssProvider()
        css.load_from_data(b"""
            .notemark-status { border-top: 1px solid alpha(currentColor, 0.12); font-size: 0.85em; }
            .monospace { font-family: monospace; }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _ensure_window(self):
        if self.window is None:
            settings = load_settings()
            if not os.path.exists(CONFIG_FILE) and system_prefers_dark():
                settings["theme"] = "dark"
            self.window = NotemarkWindow(self, settings)
            self.window.show_all()
            self.window._set_mode_buttons(settings["mode"])
            self.window._update_title()
        return self.window

    def do_activate(self):
        self._ensure_window().present()

    def do_open(self, files, n_files, hint):
        win = self._ensure_window()
        if files:
            win.open_path(files[0].get_path())
        win.present()


def main():
    app = NotemarkApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
