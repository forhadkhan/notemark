#!/usr/bin/env python3
"""Notemark - a Notion-style Markdown editor for Linux.

The native chrome (header bar, dialogs, zoom, theme, shortcuts) is GTK 3. The
document itself lives in a WebKitGTK view loading ``ui/index.html``: in page
mode that page is directly editable and is converted back to Markdown, while
source mode edits the Markdown text itself. See ``ui/app.js``.

Every open file is a tab (``Document``) with its own view, so undo history,
scroll position, cursor and mode are kept per file. ``.csv`` and ``.tsv`` files
open as an editable table (``ui/csv.js``).
"""
import collections
import errno
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
import unicodedata
from urllib.parse import quote

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gdk, Gio, GLib, Gtk, WebKit2  # noqa: E402

DEFAULT_APP_ID = "com.forhadkhan.Notemark"     # the desktop file, icon and dock grouping are named after this
# One process per app id owns the windows; later launches hand it their files and exit. Dev runs and tests set
# NOTEMARK_APP_ID so they never reach a user's real instance.
APP_ID = os.environ.get("NOTEMARK_APP_ID") or DEFAULT_APP_ID
if not Gio.Application.id_is_valid(APP_ID):
    APP_ID = DEFAULT_APP_ID
APP_NAME = "Notemark"
VERSION = "0.3.0"

HERE = os.path.dirname(os.path.abspath(__file__))
UI_INDEX = os.path.join(HERE, "ui", "index.html")
CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "notemark")
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.json")
SESSION_FILE = os.path.join(CONFIG_DIR, "session.json")

ZOOM_STEPS = [0.5, 0.67, 0.75, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]
MODES = ("page", "source", "split")
LAYOUTS = (("lr", "Editor left"), ("rl", "Editor right"), ("tb", "Editor top"), ("bt", "Editor bottom"))
CSV_EXTENSIONS = (".csv", ".tsv")
GNOME_FILES = "x-special/gnome-copied-files"   # what GNOME/Nautilus-style file managers paste
DEFAULT_SETTINGS = {
    "theme": "light", "zoom": 1.0, "mode": "page", "layout": "lr", "split": 50.0, "sync": True,
    "show_status": True, "restore_session": True, "editor_font": 14,
}


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
        if data.get("layout") in dict(LAYOUTS):
            settings["layout"] = data["layout"]
        for key in ("sync", "show_status", "restore_session"):
            if isinstance(data.get(key), bool):
                settings[key] = data[key]
        font = int(data.get("editor_font", 14))
        if 10 <= font <= 28:
            settings["editor_font"] = font
        split = float(data.get("split", 50.0))
        if 15.0 <= split <= 85.0:
            settings["split"] = split
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


def kind_for(path):
    """``csv`` for table files, ``md`` for everything else."""
    return "csv" if path and path.lower().endswith(CSV_EXTENSIONS) else "md"


# What a document last read from, or wrote to, its file: modification time, size and a hash of the bytes. The hash
# is what proves a change (touch, chmod and identical rewrites leave it alone); None past HASH_LIMIT, where
# time and size decide.
DiskState = collections.namedtuple("DiskState", "mtime size digest")
HASH_LIMIT = 64 * 1024 * 1024
CRASH_RELOAD_GAP = 60           # seconds: a page process that dies again sooner is not restarted again
WATCH_DEBOUNCE_MS = 250         # quiet time after the last file event before the file is looked at


def read_text_state(path):
    """The text of ``path`` (UTF-8, else Latin-1; any line ending becomes \\n) and its DiskState, from one read."""
    with open(path, "rb") as fh:
        mtime = os.fstat(fh.fileno()).st_mtime_ns
        data = fh.read()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    state = DiskState(mtime, len(data), hashlib.sha1(data).digest() if len(data) <= HASH_LIMIT else None)
    return text.replace("\r\n", "\n").replace("\r", "\n"), state


def read_text_file(path):
    return read_text_state(path)[0]


def disk_state(path):
    """The DiskState of the file as it is now (raises OSError)."""
    return read_text_state(path)[1]


def verify_disk(path, known, force=False):
    """Compare the file with ``known``: ("same", state), ("changed", state) or ("gone", None); raises OSError when
    it exists but cannot be read. Without ``force`` an unchanged time and size is trusted, which keeps polling cheap."""
    try:
        st = os.stat(path)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            return "gone", None
        raise
    same_stat = (st.st_mtime_ns, st.st_size) == (known.mtime, known.size)
    if st.st_size > HASH_LIMIT or known.digest is None:
        return ("same", known) if same_stat else ("changed", DiskState(st.st_mtime_ns, st.st_size, None))
    if same_stat and not force:
        return "same", known
    state = disk_state(path)
    return ("same" if state.digest == known.digest else "changed"), state


def load_session():
    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        windows = []
        for win in data.get("windows", []):
            files = [p for p in win.get("files", []) if isinstance(p, str) and os.path.isfile(p)]
            if files:
                windows.append({"files": files, "active": int(win.get("active", 0))})
        return windows
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def save_session(windows):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(SESSION_FILE, "w", encoding="utf-8") as fh:
            json.dump({"windows": windows}, fh, indent=2)
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
# One open file
# --------------------------------------------------------------------------- #
# Reload: Notemark.load puts the scroll and the caret back at the top, so note them first and restore them after.
RELOAD_JS = """(function (text, opts) {
  var N = window.Notemark; if (!N) return;
  var ed = document.getElementById("editor"), pane = document.getElementById("page-pane");
  var edTop = ed.scrollTop, paneTop = pane.scrollTop, a = ed.selectionStart, b = ed.selectionEnd;
  N.load(text, opts);
  var n = ed.value.length;
  if (N.getMode() !== "page") ed.setSelectionRange(Math.min(a, n), Math.min(b, n));
  function put() { ed.scrollTop = edTop; pane.scrollTop = paneTop; }
  put(); requestAnimationFrame(put);
})(%s, %s);"""


class Document:
    """A tab: one file (or an unsaved note) with its own WebKit view."""

    def __init__(self, window, path=None, text=None, kind=None, disk=None):
        self.window = window
        self.path = os.path.abspath(path) if path else None
        self.kind = kind or kind_for(self.path)
        self.mode = window.settings["mode"]
        self.text = text            # content waiting for the view; None = read from disk
        self.saved_text = None      # baseline for the dirty marker when it differs from ``text``
        self.dirty = False
        self.words = self.chars = 0
        self.cursor = ""
        self.view = None
        self.ready = False
        self.pending_action = None
        self._crashed = -CRASH_RELOAD_GAP   # when the page process last died and was reloaded (time.monotonic)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)   # the notebook page
        self.label = None
        self.tab = None
        self.disk = disk            # DiskState of the file as last read or written; None until it has been read
        self.monitor = None         # Gio.FileMonitor on ``path``
        self.bar = None             # Gtk.InfoBar at the top of ``box``: the file changed or vanished
        self._bar_key = None        # (kind, dirty) the bar was built for
        self._dismissed = None      # what the user waved away, so a focus check does not raise it again
        self._seen = None           # what the last check found: ("changed", digest) or ("gone",)
        self._timer = 0             # pending debounce of file events
        self._stale = False         # an event said the file was replaced: watch it again
        self._unreadable = False
        self._disposed = False
        if self.path and text is not None:
            if self.disk is None:
                try:
                    self.disk = disk_state(self.path)
                except OSError:
                    pass
            self.watch()

    # ----- naming -------------------------------------------------------- #
    @property
    def name(self):
        if self.path:
            return os.path.basename(self.path)
        return "Untitled" + (".csv" if self.kind == "csv" else "")

    # ----- the view ------------------------------------------------------ #
    def ensure_view(self):
        """Create the WebKit view on first use, so restored background tabs cost nothing."""
        if self.view is not None:
            return
        if self.text is None:
            try:
                self.text, self.disk = read_text_state(self.path) if self.path else ("", None)
            except OSError as exc:
                self.window.error("Could not open file", str(exc))
                self.text = ""
        if self.path and self.monitor is None:
            self.watch()
        ucm = WebKit2.UserContentManager()
        ucm.register_script_message_handler("app")
        ucm.connect("script-message-received::app", self._on_script_message)
        self.view = WebKit2.WebView.new_with_user_content_manager(ucm)
        ws = self.view.get_settings()
        debug = bool(os.environ.get("NOTEMARK_DEBUG"))
        ws.set_enable_developer_extras(debug)
        ws.set_enable_javascript(True)
        ws.set_allow_file_access_from_file_urls(True)
        ws.set_enable_write_console_messages_to_stdout(debug)
        self.view.set_zoom_level(self.window.settings["zoom"])
        self.view.connect("load-changed", self._on_load_changed)
        self.view.connect("scroll-event", self.window.on_scroll)
        self.view.connect("decide-policy", self._on_decide_policy)
        self.view.connect("context-menu", lambda *a: not debug)
        self.window.paint_background(self.view)
        # Stay hidden until the page has loaded: drawing a view whose web process is not ready
        # blocks the main thread for about half a second, twice. show_all() must not reveal it.
        self.view.set_no_show_all(True)
        self.box.pack_start(self.view, True, True, 0)
        self.view.connect("load-failed", self._on_load_failed)
        self.view.connect("web-process-terminated", self._on_process_terminated)
        self.view.load_uri(GLib.filename_to_uri(UI_INDEX, None))

    def run_js(self, script):
        if self.view is not None:
            self.view.evaluate_javascript(script, -1, None, None, None, None, None)

    def call_js(self, func, *args):
        payload = ", ".join(json.dumps(a) for a in args)
        self.run_js("window.Notemark && Notemark.%s(%s);" % (func, payload))

    def _reveal(self):
        self.view.set_no_show_all(False)
        self.view.show()
        if self.window.current is self:
            self.view.grab_focus()

    def _on_load_failed(self, view, event, uri, error):
        self._reveal()
        return False

    def _on_process_terminated(self, view, reason):
        """The page process died: say so instead of leaving a blank tab, and bring the page back, once a minute at most."""
        self._reveal()
        self.pending_action = None          # an answer from the dead page will never come
        if self.ready:                      # it had the document: only the file on disk (or nothing) is left
            self.ready = False
            self.text, self.saved_text = "", None
            if self.path:
                try:
                    self.text, self.disk = read_text_state(self.path)
                except OSError:
                    pass
            self.dirty = False
            self.window.document_changed(self)
            note = "The page process crashed; unsaved edits in this tab may be lost"
        else:                               # it died while loading: the text is still waiting
            note = "The page process crashed while loading; trying again"
        now = time.monotonic()
        if now - self._crashed < CRASH_RELOAD_GAP:
            self.window.notify("The page process crashed again. Close this tab and open the file again.", 8000)
            return
        self._crashed = now
        self.window.notify(note, 8000)
        view.reload()

    def _on_load_changed(self, view, event):
        if event != WebKit2.LoadEvent.FINISHED:
            return
        self._reveal()
        self.ready = True
        s = self.window.settings
        self.call_js("init", {
            "theme": s["theme"], "mode": self.mode, "layout": s["layout"], "split": s["split"],
            "sync": s["sync"], "editor_font": s["editor_font"],
        })
        opts = {"kind": self.kind}
        if self.saved_text is not None:
            opts["saved"] = self.saved_text
        self.call_js("load", self.text or "", opts)
        self.text = self.saved_text = None

    def _on_decide_policy(self, view, decision, dtype):
        # Keep the view on our own index page; external navigation is refused.
        if dtype == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri()
            if uri and not uri.startswith("file://") and uri != "about:blank":
                decision.ignore()
                return True
        return False

    def _on_script_message(self, ucm, result):
        if self._disposed:              # a report that was on its way when the tab or window closed
            return
        try:
            msg = json.loads(result.get_js_value().to_string())
        except Exception:
            return
        kind = msg.get("type")
        if kind == "changed":
            changed = bool(msg.get("dirty")) != self.dirty
            self.dirty = bool(msg.get("dirty"))
            if "words" in msg:                  # the first keystroke reports the dirty flag alone
                self.words, self.chars = msg.get("words", 0), msg.get("chars", 0)
            if "line" in msg:
                self.cursor = "Ln %s, Col %s" % (msg["line"], msg.get("col", 1))
            self.window.document_changed(self, retitle=changed)
            if changed and self.bar is not None:
                self.set_bar(self._bar_key[0])          # the wording depends on unsaved changes
        elif kind == "link":
            href = msg.get("href", "")
            if href.startswith(("http://", "https://", "mailto:")):
                Gtk.show_uri_on_window(self.window, href, Gdk.CURRENT_TIME)
        elif kind == "content":
            cb, self.pending_action = self.pending_action, None
            if cb:
                cb(msg.get("text", ""))
        elif kind == "split":
            try:
                self.window.settings["split"] = round(float(msg.get("pct", 50.0)), 1)
                save_settings(self.window.settings)
            except (TypeError, ValueError):
                pass
        elif kind == "cursor":
            self.cursor = "Ln %s, Col %s" % (msg.get("line", 1), msg.get("col", 1))
            self.window.document_changed(self)
        elif kind == "mode":
            if msg.get("mode") in MODES:
                self.mode = msg["mode"]
                self.window.mode_changed(self)
        elif kind == "copy":
            self.window.copy_text(msg.get("text", ""), msg.get("note") or "Copied")

    def request_content(self, callback):
        """Ask the page for the current text; callback(text) runs later."""
        if not self.ready:
            # The view has not finished loading: the text we hold is the content.
            callback(self.text or "")
            return
        self.pending_action = callback
        self.call_js("sendContent")

    # ----- the file changing underneath us ------------------------------- #
    def watch(self):
        """(Re)start the file monitor on ``path``. Replacing a file (write a temp file, rename it over) leaves a
        monitor on the old inode quiet, so this also runs after such events."""
        self._cancel_monitor()
        if self._disposed or not self.path:
            return
        try:
            mon = Gio.File.new_for_path(self.path).monitor_file(Gio.FileMonitorFlags.WATCH_MOVES, None)
        except GLib.Error:
            return              # not supported here (some network mounts): focus and tab checks still run
        mon.connect("changed", self._on_file_event)
        self.monitor = mon

    def _cancel_monitor(self):
        mon, self.monitor = self.monitor, None
        if mon is not None:
            mon.cancel()

    def dispose(self):
        """The tab is closing: nothing may call back into it any more."""
        self._disposed = True
        self._cancel_monitor()
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0

    def _on_file_event(self, monitor, file, other, event):
        if event in (Gio.FileMonitorEvent.RENAMED, Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.MOVED_IN,
                     Gio.FileMonitorEvent.MOVED_OUT, Gio.FileMonitorEvent.DELETED):
            self._stale = True
        if self._timer:
            GLib.source_remove(self._timer)
        self._timer = GLib.timeout_add(WATCH_DEBOUNCE_MS, self._events_settled)

    def _events_settled(self):
        self._timer = 0
        if self._stale:
            self._stale = False
            self.watch()                # the new monitor first, then look: nothing slips between the two
        self.check_disk(force=True)
        return False

    def check_disk(self, force=False):
        """Compare the file with what this tab last read or wrote, and show or drop the notice to match.
        Without ``force`` an unchanged time and size is trusted (focus and tab switches)."""
        if self._disposed or not self.path or self.disk is None:
            return
        try:
            verdict, state = verify_disk(self.path, self.disk, force)
        except OSError as exc:
            if not self._unreadable:
                self._unreadable = True
                self.window.notify("Cannot read %s: %s" % (self.name, exc.strerror or exc), 4000)
            return
        self._unreadable = False
        if verdict == "same":
            self.disk = state           # a touch moves the time only
            self._dismissed = self._seen = None
            self.set_bar(None)
            return
        self._seen = ("gone",) if verdict == "gone" else ("changed", state.digest or state[:2])
        if self._seen == self._dismissed:
            return
        self._dismissed = None
        self.set_bar(verdict)

    def disk_written(self):
        """We just wrote ``path``: that is the new baseline, and any notice about the old file is moot."""
        try:
            self.disk = disk_state(self.path)
        except OSError:
            self.disk = None
        self._dismissed = self._seen = None
        self._unreadable = False
        self.set_bar(None)
        self.watch()

    def set_bar(self, kind):
        """Show the notice for ``kind`` ("changed" or "gone"), or none for None. Rebuilt only when it differs."""
        key = (kind, self.dirty and kind == "changed") if kind else None
        if key == self._bar_key:
            return
        if self.bar is not None:
            self.bar.destroy()
            self.bar = None
        self._bar_key = key
        if not kind:
            return
        bar = Gtk.InfoBar()
        bar.set_message_type(Gtk.MessageType.WARNING)
        bar.set_show_close_button(True)
        if kind == "gone":
            text = "%s was deleted or moved on disk." % self.name
            bar.add_button("Save As…", Gtk.ResponseType.APPLY)
        else:
            text = "%s has changed on disk." % self.name
            if self.dirty:
                text = "%s has changed on disk, and you have unsaved changes. Reloading will discard them." % self.name
            btn = bar.add_button("Reload", Gtk.ResponseType.ACCEPT)
            btn.get_style_context().add_class("destructive-action" if self.dirty else "suggested-action")
        label = Gtk.Label(label=text)
        label.set_xalign(0.0)
        label.set_line_wrap(True)
        bar.get_content_area().add(label)
        bar.connect("response", self._on_bar_response)
        self.box.pack_start(bar, False, False, 0)
        self.box.reorder_child(bar, 0)          # above the view, which may be packed later or earlier
        bar.show_all()
        self.bar = bar

    @property
    def bar_kind(self):
        return self._bar_key[0] if self._bar_key else None

    @property
    def bar_text(self):
        if self.bar is None:
            return None
        return self.bar.get_content_area().get_children()[0].get_text()

    def _on_bar_response(self, bar, response):
        if response == Gtk.ResponseType.ACCEPT:
            self.window.reload_doc(self)
        elif response == Gtk.ResponseType.APPLY:
            self.window.save_as(self)
        else:
            self._dismissed = self._seen
            self.set_bar(None)

    def reload(self):
        """Replace the tab's content with the file as it is now. Unsaved edits go; mode, scroll and (in source
        mode) the caret stay where they were."""
        if self._disposed or not self.path:
            return
        try:
            text, state = read_text_state(self.path)
        except OSError as exc:
            self.window.notify("Could not reload %s: %s" % (self.name, exc.strerror or exc), 4000)
            return
        self.disk = state
        self._dismissed = self._seen = None
        self._unreadable = False
        self.set_bar(None)
        if self.ready:
            self.run_js(RELOAD_JS % (json.dumps(text), json.dumps({"kind": self.kind})))
        else:                                   # no view yet, or it is still loading: it will take this text
            self.text, self.saved_text = text, None
        self.dirty = False
        self.window.document_changed(self)


# --------------------------------------------------------------------------- #
# Excel export: a CSV table becomes one worksheet of an .xlsx workbook
# --------------------------------------------------------------------------- #
# Limits of the .xlsx format. Past the row and column limits Excel cannot open the sheet,
# so those exports are refused rather than cut short.
XLSX_MAX_ROWS = 1_048_576
XLSX_MAX_COLS = 16_384
XLSX_MAX_CELL = 32_767          # characters in one cell
XLSX_MIN_WIDTH = 8              # auto-fitted column widths, in characters
XLSX_MAX_WIDTH = 60
CSV_DELIMITERS = (",", ";", "\t", "|")          # same order as ui/csv.js
_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")
_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
_XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'


class ExportError(Exception):
    """An export that cannot be written as asked. The message is shown to the user."""


def detect_delimiter(text):
    """The delimiter that appears most often on the first non-empty line, outside quotes (as ui/csv.js)."""
    first = next((ln for ln in re.split(r"\r?\n", text) if ln.strip()), "")
    best, best_count = ",", 0
    for delim in CSV_DELIMITERS:
        count, quoted = 0, False
        for ch in first:
            if ch == '"':
                quoted = not quoted
            elif not quoted and ch == delim:
                count += 1
        if count > best_count:
            best, best_count = delim, count
    return best


def parse_csv(text):
    """Rows of cells, read the way ui/csv.js reads them, so the workbook matches the table on screen.

    A quote only opens a cell at its start; ragged rows are padded to the widest row.
    """
    delim = detect_delimiter(text)
    if text.startswith("﻿"):
        text = text[1:]
    rows, row, cell, quoted = [], [], [], False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quoted:
            if ch == '"':
                if i + 1 < n and text[i + 1] == '"':
                    cell.append('"')
                    i += 1
                else:
                    quoted = False
            else:
                cell.append(ch)
        elif ch == '"' and not cell:
            quoted = True
        elif ch == delim:
            row.append("".join(cell))
            cell = []
        elif ch in "\r\n":
            if ch == "\r" and i + 1 < n and text[i + 1] == "\n":
                i += 1
            row.append("".join(cell))
            rows.append(row)
            row, cell = [], []
        else:
            cell.append(ch)
        i += 1
    if cell or row:
        row.append("".join(cell))
        rows.append(row)
    width = max((len(r) for r in rows), default=0)
    for r in rows:
        r.extend([""] * (width - len(r)))
    return rows


def _col_name(index):
    """0-based column index to its letters: 0 is A, 26 is AA."""
    name, number = "", index + 1
    while number:
        number, rem = divmod(number - 1, 26)
        name = chr(65 + rem) + name
    return name


def _display_width(text):
    """Width of the longest line in columns; East Asian wide characters take two."""
    return max((sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in line)
                for line in text.split("\n")), default=0)


def _sheet_name(title):
    """A worksheet name: no []:*?/\\, at most 31 characters, not 'History' (Excel reserves it)."""
    name = re.sub(r"[\[\]:*?/\\]", " ", title).strip().strip("'")[:31].strip()
    if not name or name.lower() == "history":
        return "Table" if name else "Sheet1"
    return name


def build_xlsx(text, title="Sheet1"):
    """Return (xlsx bytes, notes) for CSV text. notes counts what had to change to fit the format."""
    import zipfile                              # only needed for an export, so not paid for at startup
    from xml.sax.saxutils import escape
    rows = parse_csv(text) or [[""]]
    if len(rows) > XLSX_MAX_ROWS:
        raise ExportError("The table has {:,} rows. An Excel sheet holds {:,}.".format(len(rows), XLSX_MAX_ROWS))
    width = len(rows[0])
    if width > XLSX_MAX_COLS:
        raise ExportError("The table has {:,} columns. An Excel sheet holds {:,}.".format(width, XLSX_MAX_COLS))

    notes = {"cut": 0, "cleaned": 0}
    widths = [XLSX_MIN_WIDTH] * width
    sheet_rows = []
    for r_index, row in enumerate(rows, 1):
        cells = []
        for c_index, value in enumerate(row):
            # Line endings are normalised; characters XML cannot carry are dropped; long cells are cut.
            clean = _XML_BAD.sub("", value.replace("\r\n", "\n").replace("\r", "\n"))
            if clean != value:
                notes["cleaned"] += 1
            if len(clean) > XLSX_MAX_CELL:
                clean = clean[:XLSX_MAX_CELL]
                notes["cut"] += 1
            if not clean:
                continue
            style = ' s="1"' if r_index == 1 else ""   # bold header row
            # Every value is written as text, never as a formula, so "=1+1" stays as typed.
            # Text also keeps "007" and long IDs exactly as they were.
            cells.append('<c r="{}{}"{} t="inlineStr"><is><t xml:space="preserve">{}</t></is></c>'.format(
                _col_name(c_index), r_index, style, escape(clean)))
            widths[c_index] = max(widths[c_index], _display_width(clean) + 2)
        sheet_rows.append('<row r="{}">{}</row>'.format(r_index, "".join(cells)))

    cols = "".join('<col min="{0}" max="{0}" width="{1}" customWidth="1"/>'.format(
        i, min(w, XLSX_MAX_WIDTH)) for i, w in enumerate(widths, 1))
    sheet = (
        _XML_HEAD + '<worksheet xmlns="{}" xmlns:r="{}">'.format(_NS_MAIN, _NS_REL) +
        '<dimension ref="A1:{}{}"/>'.format(_col_name(width - 1), len(rows)) +
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/></sheetView></sheetViews>'
        '<sheetFormatPr defaultRowHeight="15"/><cols>' + cols + "</cols>" +
        "<sheetData>" + "".join(sheet_rows) + "</sheetData></worksheet>")
    name = escape(_sheet_name(title), {'"': "&quot;"})
    parts = {
        "[Content_Types].xml": _XML_HEAD +
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            "</Types>",
        "_rels/.rels": _XML_HEAD + '<Relationships xmlns="{}"><Relationship Id="rId1" Type="{}/officeDocument" '
            'Target="xl/workbook.xml"/></Relationships>'.format(_NS_PKG, _NS_REL),
        "xl/workbook.xml": _XML_HEAD + '<workbook xmlns="{}" xmlns:r="{}"><sheets>'
            '<sheet name="{}" sheetId="1" r:id="rId1"/></sheets></workbook>'.format(_NS_MAIN, _NS_REL, name),
        "xl/_rels/workbook.xml.rels": _XML_HEAD + '<Relationships xmlns="{}">'
            '<Relationship Id="rId1" Type="{}/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="{}/styles" Target="styles.xml"/></Relationships>'.format(
                _NS_PKG, _NS_REL, _NS_REL),
        "xl/styles.xml": _XML_HEAD + '<styleSheet xmlns="{}">'
            '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
            '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="2"><fill><patternFill patternType="none"/></fill>'
            '<fill><patternFill patternType="gray125"/></fill></fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            "</styleSheet>".format(_NS_MAIN),
        "xl/worksheets/sheet1.xml": sheet,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for part, body in parts.items():
            zf.writestr(part, body.encode("utf-8"))
    return buf.getvalue(), notes


def write_xlsx_file(path, text, title):
    """Build the workbook and write it to path; returns the notes. Safe to call off the main thread."""
    data, notes = build_xlsx(text, title)
    with open(path, "wb") as fh:
        fh.write(data)
    return notes


def run_in_thread(work, done, *args):
    """Run work() on a worker thread, then done(*args, result, exc) on the GTK main loop.
    exc is None on success, else the exception work raised. Not a daemon thread: closing the
    last window must not cut a file half written."""
    def target():
        result = exc = None
        try:
            result = work()
        except Exception as e:                  # delivered to done(), which decides what to show
            exc = e

        def deliver():
            done(*args, result, exc)
            return False                        # run once
        GLib.idle_add(deliver)
    threading.Thread(target=target, name="notemark-worker").start()


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #
def first_icon(*names):
    theme = Gtk.IconTheme.get_default()
    return next((n for n in names if theme.has_icon(n)), names[-1])


class NotemarkWindow(Gtk.ApplicationWindow):
    def __init__(self, app, settings):
        super().__init__(application=app, title=APP_NAME)
        self.settings = settings
        self.docs = []
        self._active = None         # set before GTK's switch-page signal reports the new page
        self.restoring = False      # while True, adding pages does not create their views
        self._toast_id = 0
        self._owner = None          # invisible widget that owns the clipboard for "Copy File"
        self._exporting = False     # an Excel export is running on its worker thread
        self._closed = False

        self.set_default_size(1080, 760)
        self.set_icon_name("notemark")

        self._build_header()
        self._build_body()
        self.connect("delete-event", self._on_delete)
        self.connect("destroy", lambda *_: self._on_destroy(app))
        self.connect("notify::is-active", self._on_active)

        self.apply_theme(self.settings["theme"])
        self.apply_zoom(self.settings["zoom"])
        self.apply_status_visibility()

    @property
    def current(self):
        if self._active in self.docs:
            return self._active
        page = self.notebook.get_current_page()
        return self.docs[page] if 0 <= page < len(self.docs) else None

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
            ("document-new-symbolic", "New tab (Ctrl+N)", "app.new"),
            ("document-open-symbolic", "Open… (Ctrl+O)", "app.open"),
            (first_icon("media-floppy-symbolic", "document-save-symbolic"), "Save (Ctrl+S)", "app.save"),
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
            ("source", "Source", "Plain text source (Ctrl+2)"),
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
        window_section = Gio.Menu()
        window_section.append("New Window", "app.new-window")
        window_section.append("New CSV Table", "app.new-csv")
        window_section.append("Open in New Window…", "app.open-new-window")
        menu.append_section(None, window_section)
        copy_section = Gio.Menu()
        copy_section.append("Copy Contents", "app.copy-contents")
        copy_section.append("Copy File", "app.copy-file")
        copy_section.append("Copy File Path", "app.copy-path")
        menu.append_section(None, copy_section)
        save_section = Gio.Menu()
        save_section.append("Save As…", "app.save-as")
        save_section.append("Reload from Disk", "app.reload")
        save_section.append("Export HTML…", "app.export-html")
        save_section.append("Export Excel…", "app.export-excel")
        menu.append_section(None, save_section)
        section = Gio.Menu()
        section.append("Settings", "app.settings")
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

        # Tabs: the notebook only draws the tab strip; each page is a document's box.
        self.notebook = Gtk.Notebook()
        self.notebook.set_scrollable(True)
        self.notebook.set_show_border(False)
        self.notebook.set_show_tabs(False)
        new_tab = Gtk.Button.new_from_icon_name(first_icon("tab-new-symbolic", "list-add-symbolic"), Gtk.IconSize.BUTTON)
        new_tab.set_relief(Gtk.ReliefStyle.NONE)
        new_tab.set_tooltip_text("New tab (Ctrl+T)")
        new_tab.set_action_name("app.new")
        new_tab.show()
        self.notebook.set_action_widget(new_tab, Gtk.PackType.END)
        self.notebook.connect("switch-page", self._on_switch_page)
        self.notebook.connect("page-reordered", self._on_page_reordered)

        # A transient message ("Copied") floats over the document.
        overlay = Gtk.Overlay()
        overlay.add(self.notebook)
        self.toast = Gtk.Revealer()
        self.toast.set_transition_type(Gtk.RevealerTransitionType.CROSSFADE)
        self.toast.set_halign(Gtk.Align.CENTER)
        self.toast.set_valign(Gtk.Align.END)
        self.toast.set_margin_bottom(18)
        self.toast_label = Gtk.Label()
        self.toast_label.get_style_context().add_class("notemark-toast")
        self.toast.add(self.toast_label)
        overlay.add_overlay(self.toast)
        overlay.set_overlay_pass_through(self.toast, True)
        vbox.pack_start(overlay, True, True, 0)

        # Status bar
        self.status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.status.set_border_width(4)
        self.status.get_style_context().add_class("notemark-status")
        self.status_left = Gtk.Label(label="")
        self.status_left.set_xalign(0.0)
        self.status_left.get_style_context().add_class("dim-label")
        self.status_left.set_ellipsize(3)  # Pango.EllipsizeMode.END
        self.status_left.set_selectable(True)
        self.status_right = Gtk.Label(label="0 words")
        self.status_right.get_style_context().add_class("dim-label")
        self.status.pack_start(self.status_left, True, True, 8)
        self.status.pack_end(self.status_right, False, False, 8)
        self.status_left.show()
        self.status_right.show()
        self.status.set_no_show_all(True)        # the Settings switch shows and hides it as a whole
        vbox.pack_start(self.status, False, False, 0)

    def paint_background(self, view):
        rgba = Gdk.RGBA()
        rgba.parse("#191919" if self.settings["theme"] == "dark" else "#ffffff")
        view.set_background_color(rgba)

    # ----- Tabs ------------------------------------------------------------ #
    def _make_tab_label(self, doc):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        label = Gtk.Label(label=doc.name)
        label.set_ellipsize(3)
        label.set_max_width_chars(22)
        label.set_width_chars(8)
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_focus_on_click(False)
        close.set_tooltip_text("Close tab (Ctrl+W)")
        close.get_style_context().add_class("notemark-tab-close")
        close.connect("clicked", lambda *_: self.close_doc(doc))
        box.pack_start(label, True, True, 0)
        box.pack_start(close, False, False, 0)
        ebox = Gtk.EventBox()
        ebox.set_visible_window(False)
        ebox.add(box)
        ebox.connect("button-press-event", self._on_tab_click, doc)
        ebox.show_all()
        doc.label = label
        doc.tab = ebox
        self._refresh_tab(doc)
        return ebox

    def _refresh_tab(self, doc):
        doc.label.set_text(("• " if doc.dirty else "") + doc.name)
        doc.tab.set_tooltip_text(doc.path or "Not saved yet")

    def _on_tab_click(self, widget, event, doc):
        if event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        if event.button == 2:
            self.close_doc(doc)
            return True
        if event.button == 3:
            self.notebook.set_current_page(self.docs.index(doc))
            self._tab_menu(doc).popup_at_pointer(event)
            return True
        return False

    def _tab_menu(self, doc):
        menu = Gtk.Menu()

        def item(label, fn, sensitive=True):
            mi = Gtk.MenuItem.new_with_label(label)
            mi.set_sensitive(sensitive)
            mi.connect("activate", lambda *_: fn())
            menu.append(mi)

        item("Close", lambda: self.close_doc(doc))
        item("Close Other Tabs", lambda: self.close_others(doc), len(self.docs) > 1)
        menu.append(Gtk.SeparatorMenuItem())
        item("Copy File Path", lambda: self.copy_path(doc), bool(doc.path))
        item("Copy File", lambda: self.copy_file(doc), bool(doc.path))
        menu.append(Gtk.SeparatorMenuItem())
        item("Move to New Window", lambda: self.move_to_new_window(doc), len(self.docs) > 1)
        menu.show_all()
        return menu

    def new_tab(self, path=None, text=None, kind=None, activate=True, disk=None):
        doc = Document(self, path=path, text=text, kind=kind, disk=disk)
        self.docs.append(doc)
        self.notebook.append_page(doc.box, self._make_tab_label(doc))
        self.notebook.set_tab_reorderable(doc.box, True)
        doc.box.show()
        self.notebook.set_show_tabs(len(self.docs) > 1)
        if activate:
            self.notebook.set_current_page(len(self.docs) - 1)
            self._activate(doc)
        if not self.restoring:      # the restore saves the whole session once, when every window is back
            self.app.session_changed()
        return doc

    def _activate(self, doc):
        self._active = doc
        doc.ensure_view()
        doc.check_disk()                # it may have changed while the tab was in the background
        self._set_mode_buttons(doc.mode)
        self._refresh_chrome()

    def _on_switch_page(self, nb, page, index):
        if self.restoring:
            return
        if 0 <= index < len(self.docs):
            self._activate(self.docs[index])
            self.app.session_changed()
            GLib.idle_add(self._focus_current)

    def _on_page_reordered(self, nb, page, index):
        self.docs.sort(key=lambda d: nb.page_num(d.box))
        self.app.session_changed()

    def _focus_current(self):
        doc = self.current
        if doc and doc.view is not None:
            doc.view.grab_focus()
        return False

    def select_doc(self, doc):
        self.notebook.set_current_page(self.docs.index(doc))

    def next_tab(self, step):
        if len(self.docs) > 1:
            self.notebook.set_current_page((self.notebook.get_current_page() + step) % len(self.docs))

    def goto_tab(self, number):
        if self.docs:
            self.notebook.set_current_page(len(self.docs) - 1 if number == 9 else min(number, len(self.docs)) - 1)

    def find_doc(self, path):
        path = os.path.abspath(path)
        return next((d for d in self.docs if d.path == path), None)

    def close_doc(self, doc, force=False):
        if not force and doc.dirty:
            self.select_doc(doc)
            self.confirm_discard(doc, lambda: self.close_doc(doc, force=True))
            return
        index = self.docs.index(doc)
        self.docs.remove(doc)
        doc.dispose()
        self.notebook.remove_page(index)
        doc.box.destroy()
        if not self.docs:
            if len(self.app.get_windows()) > 1:
                self.destroy()
                return
            self.new_tab()
        self.notebook.set_show_tabs(len(self.docs) > 1)
        self._refresh_chrome()
        self.app.session_changed()

    def close_current(self):
        if self.current:
            self.close_doc(self.current)

    def close_others(self, keep):
        for doc in [d for d in self.docs if d is not keep]:
            if doc in self.docs:
                self.close_doc(doc)

    def move_to_new_window(self, doc):
        if len(self.docs) < 2:
            return

        def go(text):
            win = self.app.new_window()
            moved = win.new_tab(path=doc.path, text=text, kind=doc.kind)
            if doc.dirty and doc.path and os.path.isfile(doc.path):
                try:
                    moved.saved_text = read_text_file(doc.path)
                except OSError:
                    pass
            elif doc.dirty:
                moved.saved_text = ""
            win.present()
            self.close_doc(doc, force=True)
        doc.request_content(go)

    # ----- Chrome: title, status, mode buttons ----------------------------- #
    def document_changed(self, doc, retitle=True):
        if retitle:
            self._refresh_tab(doc)
        if doc is self.current:
            self._refresh_chrome(titles=retitle)

    def _refresh_chrome(self, titles=True):
        doc = self.current
        if doc is None:
            return
        if titles:
            mark = "• " if doc.dirty else ""
            self.header.props.title = mark + doc.name
            self.set_title("%s%s — %s" % (mark, doc.name, APP_NAME))
            self.status_left.set_text(doc.path or "Unsaved document")
        self.mode_buttons["page"].set_label("Table" if doc.kind == "csv" else "Page")
        self._update_status(doc)

    def _update_status(self, doc):
        minutes = max(1, round(doc.words / 220)) if doc.words else 0
        parts = [doc.cursor] if doc.cursor and doc.mode != "page" else []
        label = ("{:,} words" if doc.kind == "md" else "{:,} words").format(doc.words)
        parts += [label, "{:,} characters".format(doc.chars)]
        if minutes and doc.kind == "md":
            parts.append("~%d min read" % minutes)
        self.status_right.set_text(" · ".join(parts))

    def apply_status_visibility(self):
        self.status.set_visible(self.settings["show_status"])

    def notify(self, text, ms=2200):
        self.toast_label.set_text(text)
        self.toast.set_reveal_child(True)
        if self._toast_id:
            GLib.source_remove(self._toast_id)

        def hide():
            self._toast_id = 0
            self.toast.set_reveal_child(False)
            return False
        self._toast_id = GLib.timeout_add(ms, hide)

    # ----- Mode ------------------------------------------------------------ #
    def _on_mode_toggled(self, btn, mode):
        doc = self.current
        if btn.get_active() and doc and doc.mode != mode:
            self.set_mode(mode)

    def _set_mode_buttons(self, mode):
        btn = self.mode_buttons[mode]
        if not btn.get_active():
            btn.set_active(True)

    def mode_changed(self, doc):
        if doc is self.current:
            self._set_mode_buttons(doc.mode)
            self._update_status(doc)

    def set_mode(self, mode):
        doc = self.current
        if doc is None:
            return
        doc.mode = mode
        self._set_mode_buttons(mode)
        doc.call_js("setMode", mode)
        self._update_status(doc)

    def toggle_mode(self):
        doc = self.current
        if doc:
            self.set_mode("source" if doc.mode == "page" else "page")

    # ----- Settings that every view shares --------------------------------- #
    def each_view(self, fn):
        for doc in self.docs:
            if doc.view is not None:
                fn(doc)

    def set_layout(self, layout):
        self.each_view(lambda d: d.call_js("setLayout", layout))

    def set_sync(self, on):
        self.each_view(lambda d: d.call_js("setSync", on))

    def set_editor_font(self, px):
        self.each_view(lambda d: d.call_js("setEditorFont", px))

    def apply_theme(self, theme):
        dark = theme == "dark"
        gtk_settings = Gtk.Settings.get_default()
        if gtk_settings.props.gtk_application_prefer_dark_theme != dark:   # a set always reloads the theme
            gtk_settings.props.gtk_application_prefer_dark_theme = dark
        self.theme_btn.set_image(Gtk.Image.new_from_icon_name(
            "weather-clear-symbolic" if dark else "weather-clear-night-symbolic",
            Gtk.IconSize.BUTTON))
        self.theme_btn.set_tooltip_text(
            "Switch to light mode (Ctrl+Shift+L)" if dark else "Switch to dark mode (Ctrl+Shift+L)")

        def one(doc):
            self.paint_background(doc.view)
            doc.call_js("setTheme", theme)
        self.each_view(one)

    def apply_zoom(self, zoom):
        self.zoom_label_btn.set_label("%d%%" % round(zoom * 100))
        self.each_view(lambda d: d.view.set_zoom_level(zoom))

    def on_scroll(self, web, event):
        if not (event.state & Gdk.ModifierType.CONTROL_MASK):
            return False
        direction = event.direction
        if direction == Gdk.ScrollDirection.SMOOTH:
            ok, dx, dy = event.get_scroll_deltas()
            if dy == 0:
                return True
            direction = Gdk.ScrollDirection.UP if dy < 0 else Gdk.ScrollDirection.DOWN
        if direction == Gdk.ScrollDirection.UP:
            self.app.zoom_step(+1)
        elif direction == Gdk.ScrollDirection.DOWN:
            self.app.zoom_step(-1)
        return True

    @property
    def app(self):
        return self.get_application()

    # ----- File operations ------------------------------------------------- #
    def new_document(self, kind="md"):
        text = "Column 1,Column 2,Column 3\n,,\n,,\n" if kind == "csv" else ""
        doc = self.new_tab(text=text, kind=kind)
        if kind == "csv":
            doc.saved_text = ""          # a fresh table counts as unsaved
        return doc

    def open_dialog(self, new_window=False):
        dlg = Gtk.FileChooserNative.new("Open files", self, Gtk.FileChooserAction.OPEN, "_Open", "_Cancel")
        dlg.set_select_multiple(True)
        self._add_filters(dlg)
        doc = self.current
        if doc and doc.path:
            dlg.set_current_folder(os.path.dirname(doc.path))
        paths = dlg.get_filenames() if dlg.run() == Gtk.ResponseType.ACCEPT else []
        dlg.destroy()
        if not paths:
            return
        if new_window:
            win = self.app.new_window()
            for path in paths:
                win.open_path(path)
            win.present()
        else:
            for path in paths:
                self.open_path(path)

    def open_path(self, path):
        """Open ``path`` in a tab: switch to it if open, reuse a blank tab, else add one."""
        existing = self.find_doc(path)
        if existing:
            self.select_doc(existing)
            return existing
        try:
            text, state = read_text_state(path)
        except OSError as exc:
            self.error("Could not open file", str(exc))
            return None
        cur = self.current
        if cur and not cur.path and not cur.dirty and not cur.words and not cur.chars and cur.kind == "md":
            # The window still holds its untouched blank tab: open into it.
            cur.path = os.path.abspath(path)
            cur.kind = kind_for(cur.path)
            cur.disk = state
            cur.watch()
            if cur.ready:
                cur.call_js("load", text, {"kind": cur.kind})
            else:
                cur.text = text
            self._refresh_tab(cur)
            self._refresh_chrome()
            self.app.session_changed()
            return cur
        return self.new_tab(path=path, text=text, disk=state)

    def save(self, doc=None, then=None, path=None):
        doc = doc or self.current
        if doc is None:
            return
        target = path or doc.path
        if not target:
            self.save_as(doc, then)
            return

        def write(text):
            try:
                with open(target, "w", encoding="utf-8") as fh:
                    fh.write(text)
            except OSError as exc:
                self.error("Could not save file", str(exc))
                return
            doc.path = os.path.abspath(target)
            doc.dirty = False
            doc.disk_written()          # our own write is not an outside change
            new_kind = kind_for(doc.path)
            if new_kind != doc.kind:
                doc.kind = new_kind
                doc.call_js("load", text, {"kind": new_kind})
            else:
                doc.call_js("markClean", text)      # what was written: keys typed since the page sent it stay unsaved
            self._refresh_tab(doc)
            self._refresh_chrome()
            self.app.session_changed()
            if then:
                then()
        doc.request_content(write)

    def save_as(self, doc=None, then=None):
        doc = doc or self.current
        if doc is None:
            return
        dlg = Gtk.FileChooserNative.new("Save As", self, Gtk.FileChooserAction.SAVE, "_Save", "_Cancel")
        dlg.set_do_overwrite_confirmation(True)
        self._add_filters(dlg)
        if doc.path:
            dlg.set_filename(doc.path)
        else:
            dlg.set_current_name(doc.name if doc.kind == "csv" else "Untitled.md")
        if dlg.run() == Gtk.ResponseType.ACCEPT:
            path = dlg.get_filename()
            if not os.path.splitext(path)[1]:
                path += ".csv" if doc.kind == "csv" else ".md"
            dlg.destroy()
            self.save(doc, then, path=path)
        else:
            dlg.destroy()

    def reload_current(self):
        """Menu: read the file again; unsaved edits are only dropped after a yes."""
        doc = self.current
        if doc is None:
            return
        if not doc.path:
            self.notify("Not saved yet: nothing to reload")
            return
        self.reload_doc(doc)

    def reload_doc(self, doc):
        """Read the file again; unsaved edits are only dropped after a yes."""
        if not doc.dirty:
            doc.reload()
            return
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                text="Reload “%s” from disk?" % doc.name)
        dlg.format_secondary_text("Your unsaved changes will be lost.")
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dlg.add_button("Reload", Gtk.ResponseType.ACCEPT)
        dlg.get_widget_for_response(Gtk.ResponseType.ACCEPT).get_style_context().add_class("destructive-action")
        resp = dlg.run()
        dlg.destroy()
        if resp == Gtk.ResponseType.ACCEPT:
            doc.reload()

    def export_html(self):
        doc = self.current
        if doc is None:
            return
        dlg = Gtk.FileChooserNative.new("Export HTML", self, Gtk.FileChooserAction.SAVE, "_Export", "_Cancel")
        dlg.set_do_overwrite_confirmation(True)
        base = os.path.splitext(doc.name)[0]
        dlg.set_current_name(base + ".html")
        if doc.path:
            dlg.set_current_folder(os.path.dirname(doc.path))
        if dlg.run() != Gtk.ResponseType.ACCEPT:
            dlg.destroy()
            return
        path = dlg.get_filename()
        dlg.destroy()
        doc.pending_action = lambda html: self._write_export(path, html)
        doc.call_js("sendHtml", base)

    def _write_export(self, path, html):
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(html)
        except OSError as exc:
            self.error("Could not export", str(exc))

    def export_excel(self):
        doc = self.current
        if doc is None:
            return
        if doc.kind != "csv":
            self.notify("Excel export works on CSV tables")
            return
        dlg = Gtk.FileChooserNative.new("Export Excel", self, Gtk.FileChooserAction.SAVE, "_Export", "_Cancel")
        dlg.set_do_overwrite_confirmation(True)
        base = os.path.splitext(doc.name)[0]
        dlg.set_current_name(base + ".xlsx")
        if doc.path:
            dlg.set_current_folder(os.path.dirname(doc.path))
        f = Gtk.FileFilter()
        f.set_name("Excel workbook")
        f.add_pattern("*.xlsx")
        dlg.add_filter(f)
        if dlg.run() != Gtk.ResponseType.ACCEPT:
            dlg.destroy()
            return
        path = dlg.get_filename()
        dlg.destroy()
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        doc.request_content(lambda text: self._write_xlsx(path, text, base))

    def _write_xlsx(self, path, text, title):
        # Building a big sheet takes seconds, so it runs on a worker thread and the window stays live.
        if self._exporting:
            self.notify("An Excel export is already running")
            return
        self._exporting = True
        self.notify("Exporting " + os.path.basename(path) + "...", 60000)
        run_in_thread(lambda: write_xlsx_file(path, text, title), self._xlsx_done, path)

    def _xlsx_done(self, path, notes, exc):
        self._exporting = False
        if self._closed:
            return
        if isinstance(exc, (ExportError, OSError)):
            self.toast.set_reveal_child(False)
            self.error("Could not export", str(exc))
            return
        if exc is not None:
            raise exc
        done = "Exported " + os.path.basename(path)
        if notes["cut"]:
            done += ". {} cell{} cut to Excel's {:,}-character limit".format(
                notes["cut"], "" if notes["cut"] == 1 else "s", XLSX_MAX_CELL)
        if notes["cleaned"]:
            done += ". {} cell{} had characters Excel cannot store, which were removed".format(
                notes["cleaned"], "" if notes["cleaned"] == 1 else "s")
        self.notify(done, 4000)

    @staticmethod
    def _add_filters(dlg):
        f = Gtk.FileFilter()
        f.set_name("Markdown and CSV files")
        for pat in ("*.md", "*.markdown", "*.mdown", "*.mkd", "*.txt", "*.csv", "*.tsv"):
            f.add_pattern(pat)
        dlg.add_filter(f)
        allf = Gtk.FileFilter()
        allf.set_name("All files")
        allf.add_pattern("*")
        dlg.add_filter(allf)

    # ----- Clipboard -------------------------------------------------------- #
    def copy_text(self, text, note="Copied"):
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)
        self.notify(note)

    def copy_contents(self, doc=None):
        doc = doc or self.current
        if doc:
            doc.request_content(lambda text: self.copy_text(text, "Copied the document text"))

    def copy_path(self, doc=None):
        doc = doc or self.current
        if doc is None:
            return
        if not doc.path:
            self.notify("Save the document first: it has no file path yet")
            return
        self.copy_text(doc.path, "Copied the file path")

    def copy_file(self, doc=None):
        """Put the file itself on the clipboard, so a file manager can paste it."""
        doc = doc or self.current
        if doc is None:
            return
        if not doc.path or not os.path.isfile(doc.path):
            self.notify("Save the document first: it has no file on disk yet")
            return
        uri = "file://" + quote(doc.path)
        if self._owner is None:
            self._owner = Gtk.Invisible()
            self._owner.connect("selection-get", self._on_selection_get)
        owner = self._owner
        owner.file_uri, owner.file_path = uri, doc.path
        clipboard = Gdk.SELECTION_CLIPBOARD
        Gtk.selection_clear_targets(owner, clipboard)
        for target, info in ((GNOME_FILES, 0), ("text/uri-list", 1), ("text/plain", 2), ("UTF8_STRING", 2)):
            Gtk.selection_add_target(owner, clipboard, Gdk.Atom.intern(target, False), info)
        if not Gtk.selection_owner_set_for_display(owner.get_display(), owner, clipboard, Gdk.CURRENT_TIME):
            self.notify("Could not copy the file")
            return
        self.notify("Copied the file" + (" (the saved version)" if doc.dirty else ""))

    def _on_selection_get(self, owner, data, info, time):
        """Hand the file to whoever pastes: a file manager wants a URI, a text field the path."""
        if info == 0:
            data.set(data.get_target(), 8, ("copy\n" + owner.file_uri).encode())
        elif info == 1:
            data.set(data.get_target(), 8, (owner.file_uri + "\r\n").encode())
        else:
            data.set_text(owner.file_path, -1)

    # ----- Dialogs --------------------------------------------------------- #
    def confirm_discard(self, doc, proceed):
        """Run ``proceed`` now if the tab is clean, otherwise ask to save / discard first."""
        if not doc.dirty:
            proceed()
            return
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                text="Save changes to “%s”?" % doc.name)
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
            self.save(doc, then=proceed)

    def error(self, text, secondary):
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.ERROR,
                                buttons=Gtk.ButtonsType.OK, text=text)
        dlg.format_secondary_text(secondary)
        dlg.run()
        dlg.destroy()

    def _on_active(self, *args):
        """Back in front: files on mounts that send no change events are caught here."""
        if self.is_active():
            for doc in self.docs:
                doc.check_disk()

    def _on_destroy(self, app):
        self._closed = True
        for doc in self.docs:
            doc.dispose()
        app.window_closed(self)

    def under_modal(self):
        """Whether a dialog is open over this window, so that nothing added to it can be seen or kept."""
        return any(w is not self and w.get_modal() and w.get_visible() and w.get_transient_for() is self
                   for w in Gtk.Window.list_toplevels())

    def _on_delete(self, *args):
        """Ask about every unsaved tab, one by one, before the window goes."""
        dirty = [d for d in self.docs if d.dirty]
        if not dirty:
            return False
        before = list(self.docs)

        def step(rest):
            if not rest:
                if any(d not in before for d in self.docs):
                    return              # a file was opened into this window while we asked: closing would take it along
                for d in self.docs:
                    d.dirty = False
                self.destroy()
                return
            doc = rest[0]
            self.select_doc(doc)
            self.confirm_discard(doc, lambda: step(rest[1:]))
        step(dirty)
        return True  # we handle destruction ourselves after the dialogs

    def session_state(self):
        files = [d.path for d in self.docs if d.path]
        cur = self.current
        active = files.index(cur.path) if cur and cur.path in files else 0
        return {"files": files, "active": active}

    def show_shortcuts(self):
        rows = [
            ("Ctrl+N / Ctrl+T", "New tab"), ("Ctrl+Shift+N", "New window"), ("Ctrl+O", "Open (several files at once)"),
            ("Ctrl+S / Ctrl+Shift+S", "Save / Save as"), ("Ctrl+W", "Close tab"),
            ("Ctrl+Tab / Ctrl+Shift+Tab", "Next / previous tab"), ("Alt+1 … 9", "Go to tab (9 = last)"),
            ("Ctrl+E", "Toggle page / source"), ("Ctrl+1 / 2 / 3", "Page / Source / Split"),
            ("Ctrl+Shift+L", "Toggle dark mode"), ("Ctrl+,", "Settings"), ("Ctrl+Shift+C", "Copy file path"),
            ("Ctrl++ / Ctrl+-", "Zoom in / out"), ("Ctrl+0", "Reset zoom"), ("Ctrl+Scroll", "Zoom"),
            ("# - 1. [] > ``` ---", "Type at line start, then space / Enter"),
            ("Enter on empty line", "Leave a list, quote or code block"),
            ("Ctrl+B / I / `", "Bold / italic / inline code"), ("Ctrl+K / Ctrl+Shift+X", "Link / strikethrough"),
            ("Tab / Shift+Tab", "Indent / outdent list item, next / previous table cell"),
            ("Ctrl+click", "Open a link"),
            ("Ctrl+Z / Ctrl+Shift+Z / Ctrl+Y", "Undo / redo"), ("Ctrl+Q", "Quit"),
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

    def show_settings(self):
        s = self.settings
        app = self.app
        dlg = Gtk.Dialog(title="Settings", transient_for=self, modal=True)
        dlg.add_button("Close", Gtk.ResponseType.CLOSE)
        dlg.set_default_size(420, -1)
        grid = Gtk.Grid(column_spacing=24, row_spacing=12, border_width=18)
        row = [0]

        def heading(text):
            lbl = Gtk.Label(xalign=0.0)
            lbl.set_markup("<b>%s</b>" % text)
            grid.attach(lbl, 0, row[0], 2, 1)
            row[0] += 1

        def add(label, widget, tip=None):
            lbl = Gtk.Label(label=label, xalign=0.0)
            widget.set_halign(Gtk.Align.END)
            widget.set_valign(Gtk.Align.CENTER)
            if tip:
                widget.set_tooltip_text(tip)
            grid.attach(lbl, 0, row[0], 1, 1)
            grid.attach(widget, 1, row[0], 1, 1)
            lbl.set_hexpand(True)
            row[0] += 1

        def combo(options, current, on_change):
            cb = Gtk.ComboBoxText()
            for key, text in options:
                cb.append(key, text)
            cb.set_active_id(current)
            cb.connect("changed", lambda c: on_change(c.get_active_id()))
            return cb

        def switch(current, on_change):
            sw = Gtk.Switch()
            sw.set_active(current)
            sw.connect("notify::active", lambda w, _p: on_change(w.get_active()))
            return sw

        heading("Appearance")
        add("Theme", combo((("light", "Light"), ("dark", "Dark")), s["theme"], lambda v: app.set_theme(v)))
        add("Source editor font size", self._font_spin(s["editor_font"], app))
        add("Show status bar", switch(s["show_status"], lambda v: app.set_setting("show_status", v)),
            "File path, word and character counts along the bottom of the window")
        heading("Editing")
        add("View for new tabs", combo((("page", "Page"), ("source", "Source"), ("split", "Split")), s["mode"],
                                       lambda v: app.set_setting("mode", v)))
        add("Split layout", combo(LAYOUTS, s["layout"], lambda v: app.set_setting("layout", v)),
            "Where the source editor sits in split view")
        add("Synchronised scrolling", switch(s["sync"], lambda v: app.set_setting("sync", v)),
            "Keep the source and the page scrolled together in split view")
        heading("Startup")
        add("Reopen files from last time", switch(s["restore_session"], lambda v: app.set_setting("restore_session", v)),
            "Restore the tabs that were open when Notemark closed")
        dlg.get_content_area().add(grid)
        dlg.show_all()
        dlg.run()
        dlg.destroy()

    @staticmethod
    def _font_spin(current, app):
        spin = Gtk.SpinButton.new_with_range(10, 28, 1)
        spin.set_value(current)
        spin.connect("value-changed", lambda w: app.set_setting("editor_font", int(w.get_value())))
        return spin


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #
class NotemarkApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.settings = None
        self.quitting = False

    # ----- helpers ---------------------------------------------------------- #
    def active(self):
        win = self.get_active_window()
        if win is None:
            wins = self.get_windows()
            win = wins[-1] if wins else self.new_window()
        return win

    def new_window(self):
        win = NotemarkWindow(self, self.settings)
        win.show_all()
        win.apply_status_visibility()
        return win

    def _new_blank_window(self):
        win = self.new_window()
        win.new_tab()
        win.present()

    def for_all(self, fn):
        for win in self.get_windows():
            fn(win)

    def window_closed(self, win):
        if not self.quitting and len(self.get_windows()) > 1:
            self.session_changed(exclude=win)

    def session_changed(self, exclude=None):
        if self.quitting:
            return
        wins = [w.session_state() for w in self.get_windows() if w is not exclude and w.docs]
        save_session([w for w in wins if w["files"]])

    # ----- global settings -------------------------------------------------- #
    def set_theme(self, theme):
        self.settings["theme"] = theme
        self.for_all(lambda w: w.apply_theme(theme))
        save_settings(self.settings)

    def set_zoom(self, zoom):
        zoom = max(ZOOM_STEPS[0], min(ZOOM_STEPS[-1], zoom))
        self.settings["zoom"] = zoom
        self.for_all(lambda w: w.apply_zoom(zoom))
        save_settings(self.settings)

    def zoom_step(self, direction):
        current = self.settings["zoom"]
        if direction > 0:
            nxt = next((z for z in ZOOM_STEPS if z > current + 1e-6), ZOOM_STEPS[-1])
        else:
            nxt = next((z for z in reversed(ZOOM_STEPS) if z < current - 1e-6), ZOOM_STEPS[0])
        self.set_zoom(nxt)

    def set_setting(self, key, value):
        self.settings[key] = value
        if key == "show_status":
            self.for_all(lambda w: w.apply_status_visibility())
        elif key == "layout":
            self.for_all(lambda w: w.set_layout(value))
        elif key == "sync":
            self.for_all(lambda w: w.set_sync(value))
        elif key == "editor_font":
            self.for_all(lambda w: w.set_editor_font(value))
        save_settings(self.settings)

    def quit_all(self):
        self.session_changed()
        self.quitting = True
        for win in list(self.get_windows()):
            win.close()
        self.quitting = False

    # ----- lifecycle -------------------------------------------------------- #
    def do_startup(self):
        Gtk.Application.do_startup(self)
        GLib.set_application_name(APP_NAME)
        Gtk.Window.set_default_icon_name("notemark")
        self.settings = load_settings()
        if not os.path.exists(CONFIG_FILE) and system_prefers_dark():
            self.settings["theme"] = "dark"

        def on_doc(fn):
            return lambda: (lambda w: fn(w) if w else None)(self.active())

        actions = {
            "new": (lambda: self.active().new_document(), ["<Primary>n", "<Primary>t"]),
            "new-csv": (lambda: self.active().new_document("csv"), []),
            "new-window": (self._new_blank_window, ["<Primary><Shift>n"]),
            "open": (lambda: self.active().open_dialog(), ["<Primary>o"]),
            "open-new-window": (lambda: self.active().open_dialog(new_window=True), []),
            "save": (lambda: self.active().save(), ["<Primary>s"]),
            "save-as": (lambda: self.active().save_as(), ["<Primary><Shift>s"]),
            "reload": (lambda: self.active().reload_current(), []),
            "export-html": (lambda: self.active().export_html(), []),
            "export-excel": (lambda: self.active().export_excel(), []),
            "close-tab": (lambda: self.active().close_current(), ["<Primary>w"]),
            "next-tab": (lambda: self.active().next_tab(+1), ["<Primary>Tab", "<Primary>Page_Down"]),
            "prev-tab": (lambda: self.active().next_tab(-1), ["<Primary><Shift>Tab", "<Primary>Page_Up", "<Primary><Shift>ISO_Left_Tab"]),
            "copy-contents": (lambda: self.active().copy_contents(), []),
            "copy-file": (lambda: self.active().copy_file(), []),
            "copy-path": (lambda: self.active().copy_path(), ["<Primary><Shift>c"]),
            "toggle-mode": (lambda: self.active().toggle_mode(), ["<Primary>e"]),
            "mode-page": (lambda: self.active().set_mode("page"), ["<Primary>1"]),
            "mode-source": (lambda: self.active().set_mode("source"), ["<Primary>2"]),
            "mode-split": (lambda: self.active().set_mode("split"), ["<Primary>3"]),
            "toggle-theme": (lambda: self.set_theme("light" if self.settings["theme"] == "dark" else "dark"),
                             ["<Primary><Shift>l"]),
            "zoom-in": (lambda: self.zoom_step(+1), ["<Primary>plus", "<Primary>equal", "<Primary>KP_Add"]),
            "zoom-out": (lambda: self.zoom_step(-1), ["<Primary>minus", "<Primary>KP_Subtract"]),
            "zoom-reset": (lambda: self.set_zoom(1.0), ["<Primary>0", "<Primary>KP_0"]),
            "settings": (lambda: self.active().show_settings(), ["<Primary>comma"]),
            "shortcuts": (lambda: self.active().show_shortcuts(), ["<Primary>question"]),
            "about": (lambda: self.active().show_about(), []),
            "quit": (self.quit_all, ["<Primary>q"]),
        }
        for name, (fn, accels) in actions.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda a, p, fn=fn: fn())
            self.add_action(action)
            if accels:
                self.set_accels_for_action("app." + name, accels)
        goto = Gio.SimpleAction.new("goto-tab", GLib.VariantType.new("i"))
        goto.connect("activate", lambda a, p: self.active().goto_tab(p.get_int32()))
        self.add_action(goto)
        for n in range(1, 10):
            self.set_accels_for_action("app.goto-tab(%d)" % n, ["<Alt>%d" % n])

        css = Gtk.CssProvider()
        css.load_from_data(b"""
            .notemark-status { border-top: 1px solid alpha(currentColor, 0.12); font-size: 0.85em; }
            .monospace { font-family: monospace; }
            .notemark-toast { background: alpha(@theme_fg_color, 0.88); color: @theme_bg_color;
                              padding: 6px 14px; border-radius: 6px; }
            .notemark-tab-close { padding: 0; min-width: 20px; min-height: 20px; }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _restore(self):
        """Reopen last session's windows; True if anything came back."""
        restored = False
        if self.settings["restore_session"]:
            for state in load_session():
                win = self.new_window()
                # GTK switches to the first page it gets; only the saved active tab gets a view.
                win.restoring = True
                try:
                    for path in state["files"]:
                        win.new_tab(path=path, activate=False)
                    win.notebook.set_current_page(min(state["active"], len(win.docs) - 1))
                finally:
                    win.restoring = False
                win._activate(win.docs[win.notebook.get_current_page()])
                win.present()
                restored = True
        if restored:
            self.session_changed()      # switch-page was ignored above: write which tab each window is on
        return restored

    # do_activate and do_open run in the primary process only: a second launch forwards its request over D-Bus
    # (with the startup-notification / activation token, so present() is allowed to raise the window) and exits.
    def do_activate(self):
        if self.get_windows():
            self.active().present()
            return
        if not self._restore():
            self.new_window().new_tab()
            self.active().present()

    def do_open(self, files, n_files, hint):
        win = self.active()         # the focused window, else the newest; open_path reuses a tab already on the file
        if win.under_modal():       # a prompt (say, about unsaved tabs of a window being closed) hides tabs added behind it
            win = self.new_window()
        for f in files:
            if f.get_path():
                win.open_path(f.get_path())
        if not win.docs:            # only non-local URIs were given
            win.new_tab()
        win.present()


def main():
    GLib.set_prgname(APP_ID)    # window class and app id must match the desktop file for the dock icon
    app = NotemarkApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
