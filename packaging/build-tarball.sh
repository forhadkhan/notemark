#!/usr/bin/env bash
# Build dist/notemark-<version>-linux.tar.gz: a relocatable tree that runs in
# place, with an install.sh for a per-user or system-wide install.
set -euo pipefail
cd "$(dirname "$0")/.."

PKG=notemark
VERSION="$(make -s version)"
STAGE="build/tarball/${PKG}-${VERSION}"
OUT="dist/${PKG}-${VERSION}-linux.tar.gz"

rm -rf "$STAGE"
mkdir -p "$STAGE" dist
cp -r notemark.py ui data sample.md README.md LICENSE CHANGELOG.md "$STAGE/"

cat > "$STAGE/notemark" <<'LAUNCH'
#!/bin/sh
# Run Notemark from this directory without installing it.
exec python3 "$(dirname "$(readlink -f "$0")")/notemark.py" "$@"
LAUNCH
chmod 755 "$STAGE/notemark"

cat > "$STAGE/install.sh" <<'INSTALL'
#!/bin/sh
# Install Notemark. Default prefix is ~/.local; pass a prefix to change it:
#   ./install.sh                 -> ~/.local
#   sudo ./install.sh /usr/local -> system-wide
set -e
PREFIX="${1:-$HOME/.local}"
SRC="$(dirname "$(readlink -f "$0")")"
LIB="$PREFIX/lib/notemark"

mkdir -p "$LIB" "$PREFIX/bin" "$PREFIX/share/applications" \
         "$PREFIX/share/icons/hicolor/scalable/apps"
cp -r "$SRC/notemark.py" "$SRC/ui" "$LIB/"
printf '#!/bin/sh\nexec python3 "%s/notemark.py" "$@"\n' "$LIB" > "$PREFIX/bin/notemark"
chmod 755 "$PREFIX/bin/notemark"
sed "s|^Exec=notemark|Exec=$PREFIX/bin/notemark|" "$SRC/data/notemark.desktop" \
  > "$PREFIX/share/applications/notemark.desktop"
cp "$SRC/data/notemark.svg" "$PREFIX/share/icons/hicolor/scalable/apps/"

command -v update-desktop-database >/dev/null 2>&1 && \
  update-desktop-database -q "$PREFIX/share/applications" || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && \
  gtk-update-icon-cache -q -t -f "$PREFIX/share/icons/hicolor" || true

echo "Notemark installed to $PREFIX"
case ":$PATH:" in
  *":$PREFIX/bin:"*) ;;
  *) echo "Note: $PREFIX/bin is not on your PATH." ;;
esac
INSTALL
chmod 755 "$STAGE/install.sh"

cat > "$STAGE/uninstall.sh" <<'UNINSTALL'
#!/bin/sh
set -e
PREFIX="${1:-$HOME/.local}"
rm -rf "$PREFIX/lib/notemark"
rm -f "$PREFIX/bin/notemark" \
      "$PREFIX/share/applications/notemark.desktop" \
      "$PREFIX/share/icons/hicolor/scalable/apps/notemark.svg"
echo "Notemark removed from $PREFIX"
UNINSTALL
chmod 755 "$STAGE/uninstall.sh"

tar -czf "$OUT" -C "$(dirname "$STAGE")" "${PKG}-${VERSION}"
echo "Built $OUT ($(du -h "$OUT" | cut -f1))"
