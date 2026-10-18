"""Run a test inside a private D-Bus session bus, so it can never reach the user's own Notemark.

The bus has no service activation: Gtk would otherwise wait ~25 s for xdg-desktop-portal, which cannot
start here (no keyring on a bare bus). Portal and accessibility lookups fail at once instead.
"""
import os
import shutil
import subprocess
import sys
import tempfile

CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:tmpdir=/tmp</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


def rerun(argv, env):
    """Run ``argv`` (under xvfb-run) on a fresh private session bus; exit with its status. None if no dbus-run-session."""
    bus = shutil.which("dbus-run-session")
    if not bus:
        return None
    tmp = tempfile.mkdtemp(prefix="notemark-bus-")
    try:
        conf = os.path.join(tmp, "session.conf")
        with open(conf, "w") as fh:
            fh.write(CONFIG)
        sys.exit(subprocess.call([bus, "--config-file=" + conf, "--"] + argv, env=env))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
