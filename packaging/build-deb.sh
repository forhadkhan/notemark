#!/usr/bin/env bash
# Build dist/notemark_<version>_all.deb using dpkg-deb. No build tools required.
set -euo pipefail
cd "$(dirname "$0")/.."

PKG=notemark
VERSION="$(make -s version)"
ARCH=all
STAGE="build/deb/${PKG}_${VERSION}_${ARCH}"
OUT="dist/${PKG}_${VERSION}_${ARCH}.deb"

rm -rf "$STAGE"
mkdir -p "$STAGE" dist
make -s install DESTDIR="$STAGE" PREFIX=/usr

cat > "$STAGE/usr/share/doc/$PKG/copyright" <<'COPYRIGHT'
Notemark
Copyright (c) 2026 Forhad Khan
Released under the MIT License; see /usr/share/doc/notemark/LICENSE.

Bundled JavaScript libraries, all MIT licensed, with their licence texts in
/usr/lib/notemark/ui/vendor/:
  marked               https://github.com/markedjs/marked
  turndown             https://github.com/mixmark-io/turndown
  turndown-plugin-gfm  https://github.com/mixmark-io/turndown-plugin-gfm
COPYRIGHT

find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE" -type f -exec chmod 644 {} +
chmod 755 "$STAGE/usr/bin/$PKG"

mkdir -p "$STAGE/DEBIAN"
cat > "$STAGE/DEBIAN/control" <<CONTROL
Package: $PKG
Version: $VERSION
Section: editors
Priority: optional
Architecture: $ARCH
Installed-Size: $(du -sk "$STAGE/usr" | cut -f1)
Depends: python3 (>= 3.8), python3-gi, gir1.2-gtk-3.0, gir1.2-webkit2-4.1
Maintainer: Forhad Khan <forhadkhan@tuta.io>
Homepage: https://github.com/forhadkhan/notemark
Description: Notion-style Markdown editor
 Notemark is a small desktop editor that renders Markdown as a clean page and
 lets you type directly into it. Markdown shortcuts convert as you type, and
 files are saved as plain Markdown.
 .
 It has light and dark themes, zoom, a plain source view and a split view, and
 exports to standalone HTML. Built with GTK 3 and WebKitGTK.
CONTROL

for script in postinst postrm; do
  cat > "$STAGE/DEBIAN/$script" <<'HOOK'
#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database -q /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi
exit 0
HOOK
  chmod 755 "$STAGE/DEBIAN/$script"
done

dpkg-deb --root-owner-group --build "$STAGE" "$OUT" >/dev/null
echo "Built $OUT ($(du -h "$OUT" | cut -f1))"
