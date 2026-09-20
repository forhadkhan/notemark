#!/usr/bin/env bash
# Build dist/Notemark-<version>-x86_64.AppImage.
#
# The AppImage carries Notemark itself; GTK 3, WebKitGTK and PyGObject are used
# from the host system, which every mainstream desktop Linux ships. AppRun
# checks for them and explains what to install if they are missing.
set -euo pipefail
cd "$(dirname "$0")/.."

PKG=notemark
VERSION="$(make -s version)"
APPDIR="build/appimage/Notemark.AppDir"
OUT="dist/Notemark-${VERSION}-x86_64.AppImage"
TOOL="build/tools/appimagetool"

rm -rf "$APPDIR"
mkdir -p "$APPDIR" dist build/tools
make -s install DESTDIR="$APPDIR" PREFIX=/usr

cat > "$APPDIR/AppRun" <<'APPRUN'
#!/bin/sh
# Notemark AppImage entry point.
HERE="$(dirname "$(readlink -f "$0")")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Notemark needs python3, which was not found on this system." >&2
  exit 1
fi

if ! python3 - <<'PYCHECK' 2>/dev/null
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, WebKit2
PYCHECK
then
  cat >&2 <<'MSG'
Notemark needs PyGObject with GTK 3 and WebKitGTK 4.1, which this system does
not provide. Install them with one of:

  Debian/Ubuntu  sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1
  Fedora         sudo dnf install python3-gobject gtk3 webkit2gtk4.1
  Arch           sudo pacman -S python-gobject gtk3 webkit2gtk-4.1
  openSUSE       sudo zypper install python3-gobject gtk3 libwebkit2gtk-4_1-0
MSG
  exit 1
fi

exec python3 "$HERE/usr/lib/notemark/notemark.py" "$@"
APPRUN
chmod 755 "$APPDIR/AppRun"

cp "$APPDIR/usr/share/applications/$PKG.desktop" "$APPDIR/$PKG.desktop"
cp "$APPDIR/usr/share/icons/hicolor/scalable/apps/$PKG.svg" "$APPDIR/$PKG.svg"
mkdir -p "$APPDIR/usr/share/metainfo"

if [ ! -x "$TOOL" ]; then
  if command -v appimagetool >/dev/null 2>&1; then
    TOOL="$(command -v appimagetool)"
  else
    echo "Downloading appimagetool..."
    curl -fsSL -o "$TOOL" \
      https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
    chmod 755 "$TOOL"
  fi
fi

rm -f "$OUT"
ARCH=x86_64 "$TOOL" --no-appstream "$APPDIR" "$OUT" >/dev/null 2>&1 || \
ARCH=x86_64 "$TOOL" "$APPDIR" "$OUT"
chmod 755 "$OUT"
echo "Built $OUT ($(du -h "$OUT" | cut -f1))"
