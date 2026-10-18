#!/usr/bin/env python3
"""Tests for the window and tab life cycle in notemark.py: the saved session, a file forwarded while a close
prompt is open, a dead page process, and what "saved" means for keys typed during a save.

A real window with real tabs runs in this process. It runs on a private D-Bus session bus (dbus-run-session)
and a private X server (xvfb-run), under its own app id and a throwaway config folder, so it can never reach a
Notemark you have open. It skips (exit 0) when either tool is missing.

    python3 tests/test_session.py
"""
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRIVATE = "NOTEMARK_SESSION_PRIVATE_BUS"
TEST_ID = "com.forhadkhan.Notemark.SessionTest"


def skip(why):
    print("SKIP: " + why)
    sys.exit(0)


if os.environ.get(PRIVATE) != "1":
    runner = shutil.which("xvfb-run")
    if not (runner and shutil.which("dbus-run-session")):
        skip("dbus-run-session or xvfb-run not found; refusing to use your own session bus")
    env = dict(os.environ, **{PRIVATE: "1", "GDK_BACKEND": "x11", "WEBKIT_DISABLE_COMPOSITING_MODE": "1"})
    sys.path.insert(0, os.path.join(HERE, "tests"))
    import private_bus
    private_bus.rerun([runner, "-a", sys.executable, os.path.abspath(__file__)], env)

SANDBOX = tempfile.mkdtemp(prefix="notemark-session-")
CONFIG = os.path.join(SANDBOX, "config")
os.environ.update(XDG_CONFIG_HOME=CONFIG, NOTEMARK_APP_ID=TEST_ID, GDK_BACKEND="x11",
                  WEBKIT_DISABLE_COMPOSITING_MODE="1", NO_AT_BRIDGE="1")
sys.path.insert(0, HERE)
import notemark as nm  # noqa: E402
from gi.repository import Gio, GLib, Gtk, WebKit2  # noqa: E402

failures = []
ctx = GLib.MainContext.default()


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("  (" + str(detail) + ")") if detail and not ok else ""))
    if not ok:
        failures.append(name)


def pump(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ctx.iteration(False)
        time.sleep(0.005)


def wait_for(cond, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        ctx.iteration(False)
        time.sleep(0.005)
    return cond()


def write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def session():
    with open(nm.SESSION_FILE, encoding="utf-8") as fh:
        return json.load(fh)["windows"]


def content(doc):
    out = []
    doc.request_content(out.append)
    wait_for(lambda: out)
    return out[0] if out else None


def type_text(doc, extra):
    """Type into the tab like a user: source mode, inserted at the end."""
    doc.run_js("var e=document.getElementById('editor');e.focus();e.setSelectionRange(e.value.length,e.value.length);"
               "document.execCommand('insertText',false,%r);" % extra)


def edit(win, doc, extra="X"):
    win.select_doc(doc)
    win.set_mode("source")
    wait_for(lambda: doc.mode == "source")
    type_text(doc, extra)
    return wait_for(lambda: doc.dirty)


def answer_dialog(response, before=None):
    """Answer the next modal question; ``before`` runs just before the answer (Gtk's run() blocks the caller)."""
    def poll():
        for w in Gtk.Window.list_toplevels():
            if isinstance(w, Gtk.MessageDialog) and w.get_visible():
                if before:
                    before()
                w.response(response)
                return False
        return True
    GLib.timeout_add(30, poll)


def open_doc(win, name, text):
    path = os.path.join(SANDBOX, name)
    write(path, text)
    doc = win.open_path(path)
    wait_for(lambda: doc.ready)
    pump(0.3)
    return doc


def main():
    app = nm.NotemarkApp()
    app.register(None)

    # ----- restore: the tab that was active stays the saved one -------------------------------------------- #
    paths = []
    for i in range(3):
        paths.append(os.path.join(SANDBOX, "r%d.md" % i))
        write(paths[-1], "restored %d\n" % i)
    os.makedirs(os.path.dirname(nm.SESSION_FILE), exist_ok=True)
    with open(nm.SESSION_FILE, "w", encoding="utf-8") as fh:
        json.dump({"windows": [{"files": paths, "active": 2}]}, fh)
    check("the session is restored", app._restore())
    win = app.get_windows()[0]
    check("restored: three tabs, the saved one in front", len(win.docs) == 3 and win.notebook.get_current_page() == 2)
    check("restored: the session file still says tab 2 is active", session() == [{"files": paths, "active": 2}],
          session())
    check("restored: the restore flag is cleared", win.restoring is False)
    wait_for(lambda: win.docs[2].ready)
    win.close()                     # the window close path, as the X button takes it
    pump(0.3)
    check("closing the only window keeps tab 2 as the active one", session() == [{"files": paths, "active": 2}],
          session())

    # a restore that fails half way still clears the flag
    win = app.new_window()
    orig = win.new_tab
    win.restoring = True
    try:
        win.new_tab = lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))
        app.new_window = lambda: win
        with open(nm.SESSION_FILE, "w", encoding="utf-8") as fh:
            json.dump({"windows": [{"files": paths, "active": 0}]}, fh)
        try:
            app._restore()
        except RuntimeError:
            pass
    finally:
        del app.new_window
        win.new_tab = orig
    check("a failing restore does not leave the window in restoring mode", win.restoring is False)
    win.destroy()
    pump(0.2)

    # ----- a file forwarded while a close prompt is open ---------------------------------------------------- #
    app.settings["restore_session"] = True
    win = app.new_window()
    win.present()
    pump(0.3)
    d1 = open_doc(win, "dirty.md", "dirty\n")
    edit(win, d1, "!")
    fwd = os.path.join(SANDBOX, "forwarded.md")
    write(fwd, "forwarded\n")
    before_wins = set(app.get_windows())
    forward = lambda: app.do_open([Gio.File.new_for_path(fwd)], 1, "")      # noqa: E731
    answer_dialog(Gtk.ResponseType.REJECT, before=forward)
    win.close()                     # asks about the unsaved tab; the forwarded launch arrives meanwhile
    pump(0.5)
    new = [w for w in app.get_windows() if w not in before_wins]
    check("a file forwarded during a close prompt opens in a new window", len(new) == 1 and
          [d.path for d in new[0].docs] == [fwd], [[d.path for d in w.docs] for w in new])
    check("...and the window that was closing went away on Discard", win not in app.get_windows())
    for w in new:
        w.destroy()
    pump(0.2)

    # the backstop: whatever lands in a closing window while it asks must keep it open
    win = app.new_window()
    win.present()
    pump(0.3)
    d1 = open_doc(win, "dirty2.md", "dirty\n")
    edit(win, d1, "!")
    answer_dialog(Gtk.ResponseType.REJECT, before=lambda: win.open_path(fwd))
    win.close()
    pump(0.3)
    check("a tab added while the close prompt is open keeps the window open",
          win in app.get_windows() and fwd in [d.path for d in win.docs])
    for d in win.docs:
        d.dirty = False
    win.destroy()
    pump(0.2)

    # ----- Cancel on the prompt keeps everything ------------------------------------------------------------ #
    win = app.new_window()
    win.present()
    pump(0.3)
    d1 = open_doc(win, "dirty3.md", "dirty\n")
    edit(win, d1, "!")
    answer_dialog(Gtk.ResponseType.CANCEL)
    win.close()
    pump(0.2)
    check("Cancel keeps the window and its unsaved tab", win in app.get_windows() and d1.dirty)
    for d in win.docs:
        d.dirty = False
    win.destroy()
    pump(0.2)

    # ----- the page process dies --------------------------------------------------------------------------- #
    win = app.new_window()
    win.present()
    pump(0.3)
    doc = open_doc(win, "crash.md", "on disk\n")
    edit(win, doc, "unsaved")
    check("before the crash: the tab holds unsaved text", doc.dirty and "unsaved" in (content(doc) or ""))
    doc.view.terminate_web_process()
    check("a crash after load reloads the tab from disk", wait_for(lambda: doc.ready and doc.view.get_visible()) and
          wait_for(lambda: content(doc) == "on disk\n"), content(doc))
    check("...marks it clean", not doc.dirty and not doc.label.get_text().startswith("•"))
    check("...and says what happened", "crashed" in win.toast_label.get_text() and
          "unsaved edits" in win.toast_label.get_text(), win.toast_label.get_text())
    pump(0.3)
    doc.view.terminate_web_process()
    pump(1.0)
    check("a second crash within a minute is not reloaded again", not doc.ready, doc.ready)
    check("...but is reported, and the view is not left hidden", "again" in win.toast_label.get_text() and
          doc.view.get_visible(), win.toast_label.get_text())
    got = []
    doc.request_content(got.append)
    check("...and Save on the dead tab cannot hang", got == ["on disk\n"], got)
    doc._crashed = -nm.CRASH_RELOAD_GAP         # a minute later
    doc.view.emit("web-process-terminated", WebKit2.WebProcessTerminationReason.CRASHED)
    check("a crash after the minute reloads again", wait_for(lambda: doc.ready) and
          wait_for(lambda: content(doc) == "on disk\n"))

    # while it was still loading: the text is still held, so a plain reload brings it back
    doc2 = win.new_tab(path=os.path.join(SANDBOX, "crash.md"))
    check("a new tab is still loading", not doc2.ready)
    doc2.view.emit("web-process-terminated", WebKit2.WebProcessTerminationReason.CRASHED)
    check("a crash while loading reloads the page", wait_for(lambda: doc2.ready) and
          wait_for(lambda: content(doc2) == "on disk\n"), content(doc2))
    for d in list(win.docs):
        d.dirty = False
    win.destroy()
    pump(0.2)

    # ----- keys typed while a save is under way stay unsaved -------------------------------------------------- #
    win = app.new_window()
    win.present()
    pump(0.3)
    doc = open_doc(win, "race.md", "base\n")
    edit(win, doc, "A")
    sent = content(doc)
    orig_request = doc.request_content

    def request_then_type(cb):
        # the page answers; before Python writes the file, one more key reaches the page
        def late(text):
            type_text(doc, "B")
            cb(text)
        orig_request(late)
    doc.request_content = request_then_type
    win.save(doc)
    wait_for(lambda: read(doc.path) == sent)
    doc.request_content = orig_request
    pump(0.6)
    now = content(doc)
    check("the key typed during the save is in the page and not on disk", "B" in now and "B" not in read(doc.path),
          (now, read(doc.path)))
    check("...and the tab is still marked unsaved", doc.dirty)
    win.save(doc)
    check("a later save writes it and cleans the tab", wait_for(lambda: read(doc.path) == now) and
          wait_for(lambda: not doc.dirty), (read(doc.path), now, doc.dirty))
    win.destroy()
    pump(0.2)


try:
    main()
finally:
    shutil.rmtree(SANDBOX, ignore_errors=True)

print("\n%d failed" % len(failures) if failures else "\nall passed")
sys.exit(1 if failures else 0)
