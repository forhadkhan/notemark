# Changelog

All notable changes to this project are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Four split layouts (editor left, right, top or bottom), a draggable divider that
  remembers its position, and a synchronised-scrolling toggle.
- Line and column in the status bar for source and split modes, and a reading-time
  estimate.

### Fixed

- Ctrl+Z, Ctrl+Shift+Z and Ctrl+Y now undo and redo in page, source and split
  modes (Ctrl+Y was not bound at all).
- The source editor now fills its pane, as a monospace plain-text editor with light
  and dark colours, in both source and split modes.

## [0.1.0] - 2026-09-20

First public release.

### Added

- Page mode: a WYSIWYG editor where the rendered document itself is editable.
  Markdown shortcuts convert while typing (`# `, `- `, `1. `, `[] `, `> `,
  ` ``` `, `---`), and Enter on an empty line leaves a list, quote or code block.
- Source mode and split mode, with two-way synchronisation in split.
- Light and dark themes, following the desktop preference on first launch.
- Page zoom from 50 to 300 percent, with Ctrl and the scroll wheel.
- Inline formatting shortcuts for bold, italic, code, link and strikethrough.
- Clickable task checkboxes that rewrite the underlying Markdown.
- Export to a standalone HTML file.
- Unsaved-changes guard, word and character count, and settings persisted to
  `~/.config/notemark/settings.json`.
- Packaging as a Debian package, an AppImage and a portable tarball.

[0.1.0]: https://github.com/forhadkhan/notemark/releases/tag/v0.1.0
