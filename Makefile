# Notemark - build, install and packaging
#
#   make run                 launch from the source tree
#   make test                run the editor test suite (needs an X display)
#   make install             install into $(DESTDIR)$(PREFIX)
#   make deb appimage tarball
#   make dist                build all three release artefacts
#   make clean

APP     := notemark
VERSION := $(shell sed -n 's/^VERSION = "\(.*\)"/\1/p' notemark.py)
PREFIX  ?= /usr
DESTDIR ?=

BINDIR  := $(DESTDIR)$(PREFIX)/bin
LIBDIR  := $(DESTDIR)$(PREFIX)/lib/$(APP)
DATADIR := $(DESTDIR)$(PREFIX)/share
DOCDIR  := $(DATADIR)/doc/$(APP)
ICONDIR := $(DATADIR)/icons/hicolor/scalable/apps

.PHONY: all run test install uninstall deb appimage tarball dist clean version

all: dist

version:
	@echo $(VERSION)

run:
	@python3 notemark.py $(FILE)

test:
	@python3 tests/test_editor.py

install:
	install -d $(LIBDIR) $(LIBDIR)/ui $(LIBDIR)/ui/vendor
	install -m 644 notemark.py $(LIBDIR)/notemark.py
	install -m 644 ui/index.html ui/app.css ui/app.js $(LIBDIR)/ui/
	install -m 644 ui/vendor/*.js ui/vendor/LICENSE-* $(LIBDIR)/ui/vendor/
	install -d $(BINDIR)
	sed 's|@LIBDIR@|$(PREFIX)/lib/$(APP)|g' bin/notemark.in > $(BINDIR)/$(APP)
	chmod 755 $(BINDIR)/$(APP)
	install -d $(DATADIR)/applications $(ICONDIR) $(DOCDIR)
	install -m 644 data/$(APP).desktop $(DATADIR)/applications/
	install -m 644 data/$(APP).svg $(ICONDIR)/
	install -m 644 README.md LICENSE CHANGELOG.md $(DOCDIR)/
	install -m 644 sample.md $(DOCDIR)/

uninstall:
	rm -rf $(LIBDIR) $(DOCDIR)
	rm -f $(BINDIR)/$(APP) $(DATADIR)/applications/$(APP).desktop $(ICONDIR)/$(APP).svg

deb:
	@packaging/build-deb.sh

appimage:
	@packaging/build-appimage.sh

tarball:
	@packaging/build-tarball.sh

dist: deb tarball appimage
	@echo
	@ls -lh dist/

clean:
	rm -rf build dist
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
