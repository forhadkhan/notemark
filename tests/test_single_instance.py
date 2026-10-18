#!/usr/bin/env python3
"""Single-instance test for Notemark.

A second launch must hand its files to the running window and exit, instead of starting
another copy. The test starts real processes: instance A with one file, instance B with a
second file (A must show two tabs, B must exit quickly), B with no arguments (still one
window, same tabs), then closes A and starts a cold instance that restores the session.

It runs on a private D-Bus session bus (dbus-run-session) and a private X server (xvfb-run),
under its own app id and config folder, so it can never reach a Notemark you have open.
It skips (exit 0) when either tool is missing.

    python3 tests/test_single_instance.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRIVATE = "NOTEMARK_SINGLE_PRIVATE_BUS"
TEST_ID = "com.forhadkhan.Notemark.SingleTest"
OBJECT_PATH = "/" + TEST_ID.replace(".", "/")
FAST_SECOND_LAUNCH = 3.0        # seconds; a forwarded launch takes well under one


def skip(why):
    print("SKIP: " + why)
    sys.exit(0)


if os.environ.get(PRIVATE) != "1":
    runner = shutil.which("xvfb-run")
    if not (runner and shutil.which("dbus-run-session")):
        skip("dbus-run-session or xvfb-run not found; refusing to use your own session bus")
    env = dict(os.environ, **{PRIVATE: "1", "GDK_BACKEND": "x11", "WEBKIT_DISABLE_COMPOSITING_MODE": "1"})
    import private_bus
    private_bus.rerun([runner, "-a", sys.executable, os.path.abspath(__file__)], env)

from gi.repository import Gio, GLib  # noqa: E402

SANDBOX = tempfile.mkdtemp(prefix="notemark-single-")
CONFIG = os.path.join(SANDBOX, "config")
SESSION = os.path.join(CONFIG, "notemark", "session.json")
ENV = dict(os.environ, XDG_CONFIG_HOME=CONFIG, NOTEMARK_APP_ID=TEST_ID, GDK_BACKEND="x11",
           WEBKIT_DISABLE_COMPOSITING_MODE="1", NO_AT_BRIDGE="1")
procs = []
failures = []
bus_conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("  (" + detail + ")") if detail and not ok else ""))
    if not ok:
        failures.append(name)


def launch(*files):
    p = subprocess.Popen([sys.executable, os.path.join(HERE, "notemark.py"), *files], env=ENV)
    procs.append(p)
    return p


def wait_for(pred, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def session_files():
    try:
        with open(SESSION, "r", encoding="utf-8") as fh:
            return [f for w in json.load(fh)["windows"] for f in w["files"]]
    except (OSError, ValueError, KeyError):
        return None


def window_count():
    """Gtk.Application exports each of its windows as a child of <app path>/window."""
    try:
        res = bus_conn.call_sync(TEST_ID, OBJECT_PATH + "/window", "org.freedesktop.DBus.Introspectable",
                                 "Introspect", None, GLib.VariantType.new("(s)"), Gio.DBusCallFlags.NONE, 2000, None)
        return len([n for n in ET.fromstring(res.unpack()[0]).findall("node") if n.get("name")])
    except GLib.Error:
        return 0


def quit_instance():
    bus_conn.call_sync(TEST_ID, OBJECT_PATH, "org.gtk.Actions", "Activate",
                       GLib.Variant("(sava{sv})", ("quit", [], {})), None, Gio.DBusCallFlags.NONE, 2000, None)


try:
    one, two = os.path.join(SANDBOX, "one.md"), os.path.join(SANDBOX, "two.md")
    for path, text in ((one, "# one\n"), (two, "# two\n")):
        with open(path, "w") as fh:
            fh.write(text)

    a = launch(one)
    check("first launch opens its file", wait_for(lambda: session_files() == [one]), str(session_files()))
    check("first launch owns the app id", wait_for(lambda: window_count() == 1), str(window_count()))

    t0 = time.time()
    b = launch(two)
    try:
        code = b.wait(FAST_SECOND_LAUNCH)
    except subprocess.TimeoutExpired:
        code = None
    took = time.time() - t0
    check("second launch with a file exits quickly", code == 0, "exit %s after %.2fs" % (code, took))
    check("running instance gets the file as a second tab", wait_for(lambda: session_files() == [one, two], 5),
          str(session_files()))
    check("still one window", window_count() == 1, str(window_count()))
    check("first process is still running", a.poll() is None)
    print("      second launch took %.0f ms" % (took * 1000))

    c = launch(two)             # a file that is already open: no duplicate tab
    c.wait(FAST_SECOND_LAUNCH)
    time.sleep(0.3)
    check("same file again does not add a tab", session_files() == [one, two], str(session_files()))

    d = launch()
    try:
        code = d.wait(FAST_SECOND_LAUNCH)
    except subprocess.TimeoutExpired:
        code = None
    time.sleep(0.3)
    check("second launch without arguments exits", code == 0, "exit %s" % code)
    check("no duplicate window or lost tabs", window_count() == 1 and session_files() == [one, two],
          "%s windows, %s" % (window_count(), session_files()))

    quit_instance()
    try:
        a.wait(10)
    except subprocess.TimeoutExpired:
        pass
    check("closing the window ends the process", a.poll() is not None)
    check("the app id is released", wait_for(lambda: window_count() == 0, 3))

    e = launch()                # nothing running: a normal cold start that restores the session
    check("relaunch cold-starts and restores the session", wait_for(lambda: window_count() == 1, 15)
          and session_files() == [one, two], "%s windows, %s" % (window_count(), session_files()))
    quit_instance()
    try:
        e.wait(10)
    except subprocess.TimeoutExpired:
        pass
    check("second instance also exits on close", e.poll() is not None)
finally:
    for p in procs:
        if p.poll() is None:
            p.kill()
            p.wait()
    shutil.rmtree(SANDBOX, ignore_errors=True)

print("\n%s" % ("FAILED: " + ", ".join(failures) if failures else "all single-instance checks passed"))
sys.exit(1 if failures else 0)
