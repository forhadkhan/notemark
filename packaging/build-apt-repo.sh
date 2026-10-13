#!/bin/sh
# Build a signed APT repository for Notemark.
#
#   packaging/build-apt-repo.sh <apt-dir>
#
# <apt-dir>/pool/main/ must already hold the .deb files to publish. This script
# writes dists/stable/ (Packages, Release, InRelease, Release.gpg) next to it and
# exports the public key as notemark.gpg (for signed-by) and notemark.asc.
#
# The signing key lives outside the repository, in its own GnuPG home
# (default ~/.config/notemark-apt/gnupg). Create it once with:
#   packaging/build-apt-repo.sh --init-key
set -eu

GNUPGHOME="${NOTEMARK_APT_GNUPGHOME:-$HOME/.config/notemark-apt/gnupg}"
KEY_UID="Notemark Apt Repository <forhadkhan@tuta.io>"
export GNUPGHOME

if [ "${1:-}" = "--init-key" ]; then
  if gpg --list-secret-keys "$KEY_UID" >/dev/null 2>&1; then
    echo "signing key already exists in $GNUPGHOME"; exit 0
  fi
  mkdir -p "$GNUPGHOME"; chmod 700 "$GNUPGHOME"
  gpg --batch --pinentry-mode loopback --passphrase '' \
      --quick-generate-key "$KEY_UID" rsa4096 sign 5y
  echo "created signing key in $GNUPGHOME (back this directory up; it is not in git)"
  exit 0
fi

APT="${1:?usage: build-apt-repo.sh <apt-dir> | --init-key}"
cd "$APT"
[ -d pool/main ] || { echo "no pool/main in $APT" >&2; exit 1; }
gpg --list-secret-keys "$KEY_UID" >/dev/null 2>&1 || {
  echo "signing key missing: run packaging/build-apt-repo.sh --init-key" >&2; exit 1; }

DIST=dists/stable
BIN=$DIST/main/binary-all
rm -rf dists; mkdir -p "$BIN"

dpkg-scanpackages --multiversion pool/main /dev/null > "$BIN/Packages" 2>/dev/null
gzip -9kn "$BIN/Packages"
xz -9k "$BIN/Packages"

cat > "$DIST/.release.conf" <<CONF
APT::FTPArchive::Release::Origin "Notemark";
APT::FTPArchive::Release::Label "Notemark";
APT::FTPArchive::Release::Suite "stable";
APT::FTPArchive::Release::Codename "stable";
APT::FTPArchive::Release::Architectures "all";
APT::FTPArchive::Release::Components "main";
APT::FTPArchive::Release::Description "Notemark: a Notion-style Markdown editor";
CONF
apt-ftparchive -c "$DIST/.release.conf" release "$DIST" > "$DIST/Release.new"
mv "$DIST/Release.new" "$DIST/Release"
rm -f "$DIST/.release.conf"

gpg --batch --yes --armor --detach-sign --output "$DIST/Release.gpg" "$DIST/Release"
gpg --batch --yes --clearsign --output "$DIST/InRelease" "$DIST/Release"

gpg --batch --yes --armor --export "$KEY_UID" > notemark.asc
gpg --batch --yes --export "$KEY_UID" > notemark.gpg
echo "apt repository ready in $APT"
