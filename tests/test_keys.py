#!/usr/bin/env python3
"""Real-keyboard test for Notemark's shortcuts.

test_editor.py drives the editor page with JavaScript. This one starts the whole
application and presses real keys through the X server's XTEST extension, so
the path is the one a user's keyboard takes: X event -> GTK accelerators ->
WebKit -> page script. It checks undo/redo, formatting, indent, mode, tab,
zoom and save shortcuts in page, source and split mode.

Fake key presses go to whatever has the keyboard focus, so this test never runs on
your own display: it starts a private Xvfb server through xvfb-run and runs inside
that, on a private D-Bus session bus (dbus-run-session) and under its own app id, so it can never join a
Notemark you have open. It skips (exit 0) when xvfb-run or libXtst is missing.

    python3 tests/test_keys.py
"""
import atexit
import ctypes
import ctypes.util
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

PRIVATE = "NOTEMARK_KEYS_PRIVATE_X"


def skip(why):
    print("SKIP: " + why)
    sys.exit(0)


if os.environ.get(PRIVATE) != "1":
    runner = shutil.which("xvfb-run")
    if not runner:
        skip("xvfb-run not found (install xvfb); refusing to send fake keys to your own display")
    env = dict(os.environ, **{PRIVATE: "1", "GDK_BACKEND": "x11", "WEBKIT_DISABLE_COMPOSITING_MODE": "1",
                              "NOTEMARK_APP_ID": "com.forhadkhan.Notemark.Test"})
    cmd = [runner, "-a", sys.executable, os.path.abspath(__file__)]
    import private_bus
    private_bus.rerun(cmd, env)
    os.execvpe(runner, cmd, env)        # no dbus-run-session: the private app id still keeps it apart

SANDBOX = tempfile.mkdtemp(prefix="notemark-keys-")
atexit.register(shutil.rmtree, SANDBOX, ignore_errors=True)
os.environ["XDG_CONFIG_HOME"] = os.path.join(SANDBOX, "config")     # never touch the real settings

xlib_name, xtst_name = ctypes.util.find_library("X11"), ctypes.util.find_library("Xtst")
if not (xlib_name and xtst_name):
    skip("libX11 / libXtst not found")

import gi  # noqa: E402

gi.require_version("GdkX11", "3.0")
import notemark as nm  # noqa: E402

assert nm.APP_ID != nm.DEFAULT_APP_ID, "tests must use a private app id, never the installed Notemark's"
from gi.repository import Gdk, GLib, GdkX11, Gtk  # noqa: E402,F401

X11 = ctypes.CDLL(xlib_name)
XT = ctypes.CDLL(xtst_name)
X11.XOpenDisplay.restype = ctypes.c_void_p
X11.XOpenDisplay.argtypes = [ctypes.c_char_p]
X11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
X11.XKeysymToKeycode.restype = ctypes.c_ubyte
X11.XSetInputFocus.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
X11.XFlush.argtypes = [ctypes.c_void_p]
X11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
XT.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]

DPY = X11.XOpenDisplay(None)
if not DPY:
    skip("cannot open the X display")

MODIFIERS = {"ctrl": "Control_L", "shift": "Shift_L", "alt": "Alt_L"}
KEYNAMES = {" ": "space", "`": "grave", "+": "plus", "-": "minus", "=": "equal", ",": "comma", "\n": "Return"}
results = []


def check(name, condition, detail=""):
    results.append(bool(condition))
    print(("PASS  " if condition else "FAIL  ") + name + ("" if condition else "   -> %r" % (detail,)), flush=True)


def pump(ms):
    end = time.monotonic() + ms / 1000.0
    ctx = GLib.MainContext.default()
    while time.monotonic() < end:
        while ctx.iteration(False):
            pass
        time.sleep(0.004)


def keycode(name):
    sym = Gdk.keyval_from_name(name)
    return X11.XKeysymToKeycode(DPY, sym), name


def send(code, down):
    XT.XTestFakeKeyEvent(DPY, code, 1 if down else 0, 0)


def press(combo, settle=120):
    """Press e.g. "ctrl+shift+z" as one chord, then let the app react."""
    *mods, key = combo.split("+") if combo != "+" else ["+"]
    mod_codes = [keycode(MODIFIERS[m])[0] for m in mods]
    code = keycode(KEYNAMES.get(key, key))[0]
    if combo.endswith("+plus"):  # a "+" needs shift to be typed on a US keyboard
        mod_codes.append(keycode("Shift_L")[0])
        code = keycode("equal")[0]
    for m in mod_codes:
        send(m, True)
    send(code, True)
    send(code, False)
    for m in reversed(mod_codes):
        send(m, False)
    X11.XFlush(DPY)
    X11.XSync(DPY, 0)
    pump(settle)


def type_text(text):
    for ch in text:
        shifted = ch.isupper()
        if shifted:
            send(keycode("Shift_L")[0], True)
        code = keycode(KEYNAMES.get(ch, ch.lower() if shifted else ch))[0]
        send(code, True)
        send(code, False)
        if shifted:
            send(keycode("Shift_L")[0], False)
        X11.XFlush(DPY)
        X11.XSync(DPY, 0)
        pump(8)
    pump(350)


app = nm.NotemarkApp()
app.register(None)
app.activate()
pump(300)
win = app.get_windows()[0]
path = os.path.join(SANDBOX, "notes.md")
with open(path, "w") as fh:
    fh.write("")
doc = win.open_path(path)       # reuses the blank first tab
pump(1500)
xid = win.get_window().get_xid()


def focus_window():
    X11.XSetInputFocus(DPY, xid, 1, 0)
    X11.XSync(DPY, 0)
    pump(150)


def content():
    box = []
    win.current.request_content(box.append)
    deadline = time.monotonic() + 3
    while not box and time.monotonic() < deadline:
        pump(20)
    return box[0] if box else ""


def reset(text, mode=None):
    """Put known text in the current tab, clean, and give the page the keyboard."""
    d = win.current
    if mode:
        win.set_mode(mode)
        pump(150)
    d.call_js("load", text, {"kind": "md", "saved": text})
    pump(300)
    if mode == "split":
        d.run_js("document.getElementById('editor').focus()")
    focus_window()


def undo_redo_cycle(label):
    typed = content()
    press("ctrl+z", 250)
    undone = content()
    check(label + ": ctrl+z undoes", undone != typed and len(undone) < len(typed), (typed, undone))
    press("ctrl+shift+z", 250)
    check(label + ": ctrl+shift+z redoes", content() == typed, (typed, content()))
    press("ctrl+z", 250)
    press("ctrl+y", 250)
    check(label + ": ctrl+y redoes", content() == typed, (typed, content()))


focus_window()
check("window is up with a file tab", win.current is doc and doc.ready and doc.path == path, (win.current, doc.ready))

# ---- Undo / redo, three modes ------------------------------------------------
reset("", "page")
type_text("hello world")
check("page: real typing reaches the document", "hello world" in (content() or ""), content())
undo_redo_cycle("page")

reset("", "source")
type_text("hello world")
check("source: real typing reaches the document", "hello world" in (content() or ""), content())
undo_redo_cycle("source")

reset("", "split")
type_text("hello world")
check("split: real typing reaches the document", "hello world" in (content() or ""), content())
undo_redo_cycle("split")

# ---- Formatting --------------------------------------------------------------
for mode in ("page", "source"):
    for combo, expect in (("ctrl+b", "**word**"), ("ctrl+i", "*word*"), ("ctrl+`", "`word`")):
        reset("word", mode)
        press("ctrl+a")
        press(combo, 250)
        got = (content() or "").strip()
        check("%s: %s -> %s" % (mode, combo, expect), got == expect, got)
    reset("word", mode)
    press("ctrl+a")
    press("ctrl+k", 250)
    check("%s: ctrl+k makes a link" % mode, "](" in (content() or ""), content())
reset("word", "page")
press("ctrl+a")
press("ctrl+shift+x", 250)
check("page: ctrl+shift+x strikes through", "~word~" in (content() or ""), content())

# ---- Tab / Shift+Tab ---------------------------------------------------------
for mode in ("page", "source", "split"):
    reset("- a\n- b\n", mode)
    press("ctrl+End")
    press("Up")             # the page keeps an empty paragraph after a list: step back into "b"
    press("Tab", 250)
    indented = content() or ""
    check("%s: tab indents the list item" % mode, "  - b" in indented or "    - b" in indented, indented)
    press("shift+Tab", 250)
    check("%s: shift+tab outdents it again" % mode, "\n- b" in (content() or ""), content())

# ---- Mode, tabs, zoom, theme, save --------------------------------------------
reset("text", "page")
press("ctrl+e")
check("ctrl+e: page -> source", win.current.mode == "source", win.current.mode)
press("ctrl+e")
check("ctrl+e: source -> page", win.current.mode == "page", win.current.mode)
for combo, mode in (("ctrl+2", "source"), ("ctrl+3", "split"), ("ctrl+1", "page")):
    press(combo)
    check(combo + " selects " + mode, win.current.mode == mode and win.mode_buttons[mode].get_active(), win.current.mode)

before = len(win.docs)
press("ctrl+n", 600)
check("ctrl+n opens a tab", len(win.docs) == before + 1, len(win.docs))
focus_window()
press("ctrl+t", 600)
check("ctrl+t opens a tab", len(win.docs) == before + 2, len(win.docs))
focus_window()
first = win.docs[0]
press("alt+1", 300)
check("alt+1 goes to the first tab", win.current is first)
press("ctrl+Tab", 300)
check("ctrl+tab goes to the next tab", win.current is win.docs[1], win.docs.index(win.current))
press("ctrl+shift+Tab", 300)
check("ctrl+shift+tab goes back", win.current is first, win.docs.index(win.current))
press("alt+9", 300)
check("alt+9 goes to the last tab", win.current is win.docs[-1])
press("ctrl+w", 400)
press("ctrl+w", 400)
check("ctrl+w closes the tabs it opened", len(win.docs) == before, len(win.docs))
check("the original tab is current again", win.current is first)
focus_window()

zoom0 = app.settings["zoom"]
press("ctrl+plus")
check("ctrl++ zooms in", app.settings["zoom"] > zoom0, app.settings["zoom"])
press("ctrl+minus")
press("ctrl+minus")
check("ctrl+- zooms out", app.settings["zoom"] < zoom0, app.settings["zoom"])
press("ctrl+0")
check("ctrl+0 resets the zoom", app.settings["zoom"] == 1.0, app.settings["zoom"])

theme0 = app.settings["theme"]
press("ctrl+shift+l")
check("ctrl+shift+l flips the theme", app.settings["theme"] != theme0)
press("ctrl+shift+l")

reset("", "page")
type_text("saved by keyboard")
press("ctrl+s", 700)
with open(path) as fh:
    saved = fh.read()
check("ctrl+s writes the file", "saved by keyboard" in saved, saved)
check("ctrl+s clears the unsaved mark", not win.current.dirty)

calls = []
win.open_dialog = lambda *a, **k: calls.append("open")
win.show_settings = lambda *a, **k: calls.append("settings")
press("ctrl+o")
press("ctrl+comma")
check("ctrl+o runs Open", "open" in calls, calls)
check("ctrl+, opens Settings", "settings" in calls, calls)

press("ctrl+shift+c", 300)
clip = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).wait_for_text()
check("ctrl+shift+c copies the file path", clip == path, clip)

windows = len(app.get_windows())
press("ctrl+shift+n", 1200)
check("ctrl+shift+n opens a window", len(app.get_windows()) == windows + 1, len(app.get_windows()))
for extra in app.get_windows():
    if extra is not win:
        extra.destroy()
pump(200)
focus_window()

for d in win.docs:
    d.dirty = False
press("ctrl+q", 800)
check("ctrl+q closes the application", not app.get_windows(), len(app.get_windows()))

failed = results.count(False)
print("\n%d passed, %d failed" % (len(results) - failed, failed))
sys.exit(1 if failed else 0)
