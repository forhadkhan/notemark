#!/bin/sh
# Publish the documentation site (docs/) and the signed APT repository to the
# gh-pages branch, which GitHub Pages serves at
# https://notemark.forhad.me/ (APT repo under /apt).
#
#   packaging/publish-pages.sh           commit to a local gh-pages branch
#   packaging/publish-pages.sh --push    ...and push it to origin
#
# Needs dist/notemark_<version>_all.deb (make deb) and the signing key
# (packaging/build-apt-repo.sh --init-key). Older .deb versions already on the
# branch are kept, so `apt install notemark=<old version>` keeps working.
set -eu
cd "$(dirname "$0")/.."

VERSION=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' notemark.py)
DEB="dist/notemark_${VERSION}_all.deb"
[ -f "$DEB" ] || { echo "missing $DEB (run make deb)" >&2; exit 1; }
[ -f docs/index.html ] || { echo "missing docs/index.html" >&2; exit 1; }

WT=build/gh-pages
git worktree remove --force "$WT" 2>/dev/null || true
rm -rf "$WT"
trap 'git worktree remove --force "$WT" >/dev/null 2>&1 || true' EXIT

if git fetch -q origin gh-pages 2>/dev/null; then
  git worktree add -q -B gh-pages "$WT" origin/gh-pages
else
  git worktree add -q --detach "$WT"
  (cd "$WT" && git checkout -q --orphan gh-pages && git rm -rfq . && git clean -fdxq)
fi

rsync -a --delete --exclude apt --exclude .git docs/ "$WT/"   # docs/CNAME keeps the custom domain
touch "$WT/.nojekyll"
mkdir -p "$WT/apt/pool/main/n/notemark"
cp "$DEB" "$WT/apt/pool/main/n/notemark/"
packaging/build-apt-repo.sh "$WT/apt"

cd "$WT"
git add -A
if git diff --cached --quiet; then
  echo "gh-pages is already up to date"
else
  git commit -q -m "publish notemark ${VERSION}"
  echo "committed gh-pages: $(git log --oneline -1)"
fi
if [ "${1:-}" = "--push" ]; then
  git push origin gh-pages
fi
