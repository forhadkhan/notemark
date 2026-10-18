# Notemark - build, install and packaging
#
#   make run                 launch from the source tree (own app id: does not join an installed Notemark)
#   make test                run the editor, keyboard, xlsx, session and chunked-parse tests (X display for most; node for the last)
#   make install             install into $(DESTDIR)$(PREFIX)
#   make deb appimage tarball
#   make dist                build all three release artefacts
#   make pages               publish docs/ and the signed apt repo to gh-pages (PUSH=1 to push)
#   make clean

APP     := notemark
VERSION := $(shell sed -n 's/^VERSION = "\(.*\)"/\1/p' notemark.py)
PREFIX  ?= /usr
DESTDIR ?=
# make run and the tests use their own app id, so they never hand their files to an installed Notemark
APP_ID_DEV := com.forhadkhan.Notemark.Dev

BINDIR  := $(DESTDIR)$(PREFIX)/bin
LIBDIR  := $(DESTDIR)$(PREFIX)/lib/$(APP)
DATADIR := $(DESTDIR)$(PREFIX)/share
DOCDIR  := $(DATADIR)/doc/$(APP)
ICONDIR := $(DATADIR)/icons/hicolor/scalable/apps

.PHONY: all run test install uninstall deb appimage tarball dist clean version pages

all: dist

version:
	@echo $(VERSION)

run:
	@NOTEMARK_APP_ID=$(APP_ID_DEV) python3 notemark.py $(FILE)

test:
	@WEBKIT_DISABLE_COMPOSITING_MODE=1 python3 tests/test_editor.py
	@python3 tests/test_keys.py
	@python3 tests/test_xlsx.py
	@python3 tests/test_single_instance.py
	@python3 tests/test_external_change.py
	@python3 tests/test_session.py
	@python3 tests/test_sync.py
	@if command -v node >/dev/null; then node tests/test_chunk.js; else echo "skip chunked parse test: node not installed"; fi

install:
	install -d $(LIBDIR) $(LIBDIR)/ui $(LIBDIR)/ui/vendor
	install -m 644 notemark.py $(LIBDIR)/notemark.py
	install -m 644 ui/index.html ui/app.css ui/app.js ui/csv.js ui/mdchunk.js $(LIBDIR)/ui/
	install -m 644 ui/vendor/*.js ui/vendor/LICENSE-* $(LIBDIR)/ui/vendor/
	install -d $(BINDIR)
	sed 's|@LIBDIR@|$(PREFIX)/lib/$(APP)|g' bin/notemark.in > $(BINDIR)/$(APP)
	chmod 755 $(BINDIR)/$(APP)
	install -d $(DATADIR)/applications $(ICONDIR) $(DOCDIR)
	install -m 644 data/com.forhadkhan.Notemark.desktop $(DATADIR)/applications/
	install -m 644 data/$(APP).svg $(ICONDIR)/
	install -m 644 README.md LICENSE CHANGELOG.md $(DOCDIR)/
	install -m 644 sample.md $(DOCDIR)/

uninstall:
	rm -rf $(LIBDIR) $(DOCDIR)
	rm -f $(BINDIR)/$(APP) $(DATADIR)/applications/com.forhadkhan.Notemark.desktop $(ICONDIR)/$(APP).svg

deb:
	@packaging/build-deb.sh

appimage:
	@packaging/build-appimage.sh

tarball:
	@packaging/build-tarball.sh

dist: deb tarball appimage
	@echo
	@ls -lh dist/

pages: deb
	packaging/publish-pages.sh $(if $(PUSH),--push)

clean:
	rm -rf build dist
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
