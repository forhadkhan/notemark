# Changelog

All notable changes to this project are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-10-13

### Added

- Tabs: every file opens in its own tab with its own view, cursor, scroll
  position and undo history. Open several files at once, reorder tabs, close with
  the button, middle-click or Ctrl+W, switch with Ctrl+Tab or Alt+1 to 9, and a
  tab with unsaved changes asks before it closes.
- New windows (Ctrl+Shift+N), opening files in a new window, and moving a tab to
  a new window.
- CSV and TSV files open as an editable table with sorting, filtering, row and
  column editing, pasting a block of cells and a raw-text view; "New CSV Table"
  in the menu.
- Copy the document text, the file path (Ctrl+Shift+C) or the file itself, from
  the menu and the tab's context menu.
- A Settings dialog (Ctrl+,): theme, source editor font size, default view, split
  layout, synchronised scrolling, showing the status bar, and reopening your files
  on launch. The layout and scroll options moved here from the menu.
- Files that were open when Notemark closed are reopened on the next launch.
- `tests/test_keys.py`, which presses real keys through the X server to test the
  shortcuts end to end.
- Four split layouts (editor left, right, top or bottom), a draggable divider that
  remembers its position, and a synchronised-scrolling toggle.
- Line and column in the status bar for source and split modes, and a reading-time
  estimate.

### Changed

- The Save button uses a floppy-disk icon.
- The view (page, source or split) belongs to each tab. New tabs start in the view
  chosen under Settings rather than in whichever one you used last.

### Fixed

- Tab nests a list item in page mode again. It produced a list inside a list that
  Markdown could not express, so the nesting was lost on save.
- Shift+Tab was ignored in page and source mode and moved the focus out of the
  document, because WebKitGTK reports that key as "Unidentified".
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

[0.2.0]: https://github.com/forhadkhan/notemark/releases/tag/v0.2.0
[0.1.0]: https://github.com/forhadkhan/notemark/releases/tag/v0.1.0
