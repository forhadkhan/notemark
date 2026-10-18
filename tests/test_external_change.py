#!/usr/bin/env python3
"""Tests for "the open file changed on disk" (Document.watch / check_disk / reload in notemark.py).

A real window with real tabs runs in this process while the test edits the files behind its back, the way
another editor, git or a sync tool would: plain overwrite, atomic replace (write a temp file, rename it over),
touch, delete, and so on. The notice is the InfoBar at the top of the tab (``doc.bar_kind`` / ``doc.bar_text``).

It runs on a private D-Bus session bus (dbus-run-session) and a private X server (xvfb-run), under its own app
id and a throwaway config folder, so it can never reach a Notemark you have open. It skips (exit 0) when either
tool is missing.

    python3 tests/test_external_change.py
"""
import gc
import os
import shutil
import stat
import sys
import tempfile
import time
import weakref

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRIVATE = "NOTEMARK_EXTCHANGE_PRIVATE_BUS"
TEST_ID = "com.forhadkhan.Notemark.ExtChangeTest"


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

SANDBOX = tempfile.mkdtemp(prefix="notemark-extchange-")
os.environ.update(XDG_CONFIG_HOME=os.path.join(SANDBOX, "config"), NOTEMARK_APP_ID=TEST_ID, GDK_BACKEND="x11",
                  WEBKIT_DISABLE_COMPOSITING_MODE="1", NO_AT_BRIDGE="1")
sys.path.insert(0, HERE)
import notemark as nm  # noqa: E402
from gi.repository import GLib, Gtk  # noqa: E402

SETTLE = nm.WATCH_DEBOUNCE_MS / 1000 + 0.45     # long enough for a debounced check to have run
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


def replace_atomically(path, text):
    tmp = path + ".tmp"
    write(tmp, text)
    os.replace(tmp, path)


def content(doc):
    out = []
    doc.request_content(out.append)
    wait_for(lambda: out)
    return out[0] if out else None


def open_doc(win, name, text):
    path = os.path.join(SANDBOX, name)
    write(path, text)
    doc = win.open_path(path)
    wait_for(lambda: doc.ready)
    pump(0.3)                       # let the page finish its first load report
    return doc


def edit(win, doc, extra="X"):
    """Type into the tab like a user: source mode, one inserted character."""
    win.select_doc(doc)
    win.set_mode("source")
    wait_for(lambda: doc.mode == "source")
    doc.run_js("var e=document.getElementById('editor');e.focus();e.setSelectionRange(e.value.length,e.value.length);"
               "document.execCommand('insertText',false,%r);" % extra)
    return wait_for(lambda: doc.dirty)


def answer_dialog(response):
    """Answer the next modal question (Gtk's dialog.run() blocks the caller, so it has to be a timer's job)."""
    def poll():
        for w in Gtk.Window.list_toplevels():
            if isinstance(w, Gtk.MessageDialog) and w.get_visible():
                w.response(response)
                return False
        return True
    GLib.timeout_add(30, poll)


def close(win, doc):
    win.close_doc(doc, force=True)


def main():
    app = nm.NotemarkApp()
    app.register(None)              # runs do_startup: settings, actions, styles
    win = app.new_window()
    win.new_tab()                   # the blank tab open_path reuses for the first file
    win.present()
    pump(0.3)

    # ----- plain overwrite, atomic replace, no-ops ------------------------------------------------------ #
    doc = open_doc(win, "a.md", "one\n")
    check("fresh tab has no notice", doc.bar_kind is None)
    check("fresh tab is watched", doc.monitor is not None and not doc.monitor.is_cancelled())
    write(doc.path, "two\n")
    check("overwrite shows the notice", wait_for(lambda: doc.bar_kind == "changed"))
    check("clean wording", doc.bar_text == "a.md has changed on disk.", doc.bar_text)
    check("clean doc is not reloaded behind your back", content(doc) == "one\n" and not doc.dirty)
    check("notice sits above the page in the tab", doc.box.get_children()[0] is doc.bar)

    write(doc.path, "three\n")
    pump(SETTLE)
    check("several changes in a row keep one notice", doc.bar_kind == "changed" and
          sum(isinstance(c, Gtk.InfoBar) for c in doc.box.get_children()) == 1)

    # ----- Reload ---------------------------------------------------------------------------------------- #
    doc.bar.response(Gtk.ResponseType.ACCEPT)
    check("Reload loads the latest content", wait_for(lambda: content(doc) == "three\n"), content(doc))
    check("Reload removes the notice and keeps the tab clean", doc.bar_kind is None and not doc.dirty)
    pump(SETTLE)
    check("Reload does not trigger a new notice", doc.bar_kind is None)

    replace_atomically(doc.path, "four\n")
    check("atomic replace shows the notice", wait_for(lambda: doc.bar_kind == "changed"))
    replace_atomically(doc.path, "five\n")
    pump(SETTLE)
    check("a second atomic replace is still seen (monitor re-created)", doc.bar_kind == "changed")
    doc.bar.response(Gtk.ResponseType.ACCEPT)
    check("Reload after atomic replaces", wait_for(lambda: content(doc) == "five\n"), content(doc))
    replace_atomically(doc.path, "six\n")
    pump(SETTLE)
    check("changes after a Reload are still seen", doc.bar_kind == "changed")
    doc.bar.response(Gtk.ResponseType.ACCEPT)
    wait_for(lambda: content(doc) == "six\n")

    # touch, chmod, identical rewrite: nothing to say
    os.utime(doc.path, None)
    os.chmod(doc.path, 0o600)
    write(doc.path, "six\n")
    pump(SETTLE)
    check("touch, chmod and an identical rewrite show nothing", doc.bar_kind is None)

    # ----- revert before the user reacts ------------------------------------------------------------------ #
    write(doc.path, "seven\n")
    wait_for(lambda: doc.bar_kind == "changed")
    write(doc.path, "six\n")
    check("change reverted: the notice goes away", wait_for(lambda: doc.bar_kind is None))

    # ----- dismiss ---------------------------------------------------------------------------------------- #
    write(doc.path, "eight\n")
    wait_for(lambda: doc.bar_kind == "changed")
    doc.bar.response(Gtk.ResponseType.CLOSE)
    doc.check_disk()
    win._on_active()
    pump(0.2)
    check("dismissed notice does not come back on focus", doc.bar_kind is None)
    write(doc.path, "nine\n")
    check("a further change shows it again", wait_for(lambda: doc.bar_kind == "changed"))
    write(doc.path, "six\n")
    wait_for(lambda: doc.bar_kind is None)

    # ----- our own save ------------------------------------------------------------------------------------ #
    check("typing marks the tab dirty", edit(win, doc, "X"))
    win.save(doc)
    check("save completes", wait_for(lambda: not doc.dirty))
    pump(SETTLE)
    check("our own save shows nothing", doc.bar_kind is None)
    with open(doc.path, encoding="utf-8") as fh:
        saved = fh.read()
    check("our save wrote the edit", saved.endswith("X") or "X" in saved, saved)

    # ----- unsaved changes ----------------------------------------------------------------------------------- #
    edit(win, doc, "Y")
    write(doc.path, "outside\n")
    wait_for(lambda: doc.bar_kind == "changed")
    want = "a.md has changed on disk, and you have unsaved changes. Reloading will discard them."
    check("dirty wording", doc.bar_text == want, doc.bar_text)
    check("dirty Reload button is destructive",
          any(b.get_style_context().has_class("destructive-action") for b in doc.bar.get_action_area().get_children()))
    pump(SETTLE)
    check("a dirty tab is never reloaded on its own", doc.dirty and "Y" in (content(doc) or ""))
    # the user saves over it: our save wins, the notice goes
    win.save(doc)
    wait_for(lambda: not doc.dirty)
    pump(SETTLE)
    check("saving after an outside change clears the notice", doc.bar_kind is None)
    with open(doc.path, encoding="utf-8") as fh:
        check("our save won", "Y" in fh.read())

    # the user starts editing only after the notice is up: wording follows
    write(doc.path, "later\n")
    wait_for(lambda: doc.bar_kind == "changed")
    clean_text = doc.bar_text
    edit(win, doc, "Z")
    check("typing after the notice switches to the unsaved-changes wording",
          wait_for(lambda: doc.bar_text and "unsaved changes" in doc.bar_text) and "unsaved" not in clean_text)
    answer_dialog(Gtk.ResponseType.CANCEL)
    doc.bar.response(Gtk.ResponseType.ACCEPT)       # same question as the menu's Reload from Disk
    check("the notice's Reload asks first and Cancel keeps the unsaved edit", "Z" in (content(doc) or "") and doc.dirty)
    answer_dialog(Gtk.ResponseType.ACCEPT)
    doc.bar.response(Gtk.ResponseType.ACCEPT)
    check("Reload discards the unsaved edit", wait_for(lambda: content(doc) == "later\n" and not doc.dirty))
    check("tab title loses the unsaved dot", not doc.label.get_text().startswith("•"))

    # ----- deleted / moved away ---------------------------------------------------------------------------- #
    edit(win, doc, "K")
    os.unlink(doc.path)
    check("delete shows the deleted notice", wait_for(lambda: doc.bar_kind == "gone"))
    check("deleted wording", doc.bar_text == "a.md was deleted or moved on disk.", doc.bar_text)
    labels = [b.get_label() for b in doc.bar.get_action_area().get_children()]
    check("deleted notice offers Save As and no Reload", labels == ["Save As…"], labels)
    check("content is untouched after a delete", "K" in (content(doc) or "") and doc.dirty)
    write(doc.path, "back\n")
    check("file reappears: changed notice replaces it", wait_for(lambda: doc.bar_kind == "changed"))
    os.rename(doc.path, doc.path + ".moved")
    check("rename away shows the deleted notice", wait_for(lambda: doc.bar_kind == "gone"))
    os.rename(doc.path + ".moved", doc.path)
    check("moved back unchanged from what we have: changed notice", wait_for(lambda: doc.bar_kind == "changed"))
    close(win, doc)

    doc = open_doc(win, "b.md", "same\n")
    os.rename(doc.path, doc.path + ".moved")
    wait_for(lambda: doc.bar_kind == "gone")
    os.rename(doc.path + ".moved", doc.path)
    check("file back with the same content: the notice goes away", wait_for(lambda: doc.bar_kind is None))
    close(win, doc)

    # ----- CSV tabs -------------------------------------------------------------------------------------------- #
    doc = open_doc(win, "t.csv", "a,b\n1,2\n")
    write(doc.path, "a,b\n1,3\n")
    wait_for(lambda: doc.bar_kind == "changed")
    doc.bar.response(Gtk.ResponseType.ACCEPT)
    check("csv Reload", wait_for(lambda: content(doc) == "a,b\n1,3\n") and doc.kind == "csv", content(doc))
    close(win, doc)

    # ----- CRLF files read like the first load does -------------------------------------------------------- #
    doc = open_doc(win, "crlf.md", "x\n")
    with open(doc.path, "wb") as fh:
        fh.write(b"x\r\ny\r\n")
    wait_for(lambda: doc.bar_kind == "changed")
    doc.bar.response(Gtk.ResponseType.ACCEPT)
    check("Reload normalises line endings", wait_for(lambda: content(doc) == "x\ny\n"), repr(content(doc)))
    close(win, doc)

    # ----- background tab ------------------------------------------------------------------------------------ #
    front = open_doc(win, "front.md", "front\n")
    back = open_doc(win, "back.md", "back\n")
    win.select_doc(front)
    wait_for(lambda: win.current is front)
    write(back.path, "back, changed\n")
    check("a background tab collects the notice", wait_for(lambda: back.bar_kind == "changed"))
    check("the other tab shows none", front.bar_kind is None)
    win.select_doc(back)
    pump(0.2)
    check("notice is visible after switching", back.bar.get_mapped() and back.bar.get_child_visible())
    close(win, back)

    # a tab that never got a view just reads the new file when it is shown
    path = os.path.join(SANDBOX, "lazy.md")
    write(path, "lazy\n")
    win.restoring = True
    lazy = win.new_tab(path=path, activate=False)
    win.restoring = False
    write(path, "lazy, changed\n")
    pump(SETTLE)
    check("a tab without a view shows no notice", lazy.bar_kind is None and lazy.view is None)
    win.select_doc(lazy)
    wait_for(lambda: lazy.ready)
    check("and loads the new file when shown", wait_for(lambda: content(lazy) == "lazy, changed\n"), content(lazy))
    check("with no notice", lazy.bar_kind is None)
    close(win, lazy)

    # ----- Save As to a new path ------------------------------------------------------------------------------- #
    doc = open_doc(win, "old.md", "o\n")
    old_monitor = doc.monitor
    new_path = os.path.join(SANDBOX, "new.md")
    win.save(doc, path=new_path)
    wait_for(lambda: doc.path == new_path)
    pump(0.3)
    check("Save As cancels the old monitor and watches the new path",
          old_monitor.is_cancelled() and doc.monitor is not old_monitor and doc.monitor is not None)
    write(os.path.join(SANDBOX, "old.md"), "old changed\n")
    pump(SETTLE)
    check("the old path no longer matters", doc.bar_kind is None)
    write(new_path, "new changed\n")
    check("the new path is watched", wait_for(lambda: doc.bar_kind == "changed"))
    close(win, doc)

    # ----- unreadable ---------------------------------------------------------------------------------------------- #
    if os.geteuid() != 0:
        doc = open_doc(win, "perm.md", "p\n")
        write(doc.path, "p changed\n")
        os.chmod(doc.path, 0)
        pump(SETTLE)
        check("an unreadable file does not crash", win.toast_label.get_text().startswith("Cannot read perm.md") or
              doc.bar_kind == "changed", win.toast_label.get_text())
        os.chmod(doc.path, 0o600)
        wait_for(lambda: doc.bar_kind == "changed")
        os.chmod(doc.path, 0)
        doc.reload()
        check("Reload of an unreadable file reports and keeps the tab",
              win.toast_label.get_text().startswith("Could not reload") and not doc.dirty,
              win.toast_label.get_text())
        os.chmod(doc.path, 0o600)
        close(win, doc)

    # ----- closing --------------------------------------------------------------------------------------------------- #
    doc = open_doc(win, "c.md", "c\n")
    monitor = doc.monitor
    mon_ref = weakref.ref(monitor)
    write(doc.path, "c changed\n")
    pump(0.05)                      # an event is pending: its timer must not outlive the tab
    close(win, doc)
    check("closing the tab cancels its monitor", monitor.is_cancelled() and doc.monitor is None and not doc._timer)
    doc = monitor = None
    pump(SETTLE)
    gc.collect()
    check("the monitor is released with the tab", mon_ref() is None)

    doc_a = open_doc(win, "w1.md", "w1\n")
    doc_b = open_doc(win, "w2.md", "w2\n")
    monitors = [d.monitor for d in win.docs if d.monitor is not None]
    win.destroy()
    pump(0.3)
    check("closing the window cancels every monitor", len(monitors) >= 2 and all(m.is_cancelled() for m in monitors))
    del doc_a, doc_b


try:
    main()
finally:
    for top, dirs, files in os.walk(SANDBOX):       # a test made some of these unwritable; rmtree needs them open
        for name in dirs + files:
            p = os.path.join(top, name)
            try:
                os.chmod(p, stat.S_IRWXU if os.path.isdir(p) else stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
    shutil.rmtree(SANDBOX, ignore_errors=True)

print("\n%d failed" % len(failures) if failures else "\nall passed")
sys.exit(1 if failures else 0)
