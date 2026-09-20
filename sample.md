# Welcome to Notemark

A **lightweight**, Notion-like Markdown editor for Linux.
Just click anywhere and type — this page is the editor. `Ctrl+2` shows the Markdown source, `Ctrl+3` shows both side by side.

## Why it exists

- Opens instantly, no Electron, no accounts, no sync.
- Your notes stay as plain `.md` files on disk.
- Light and dark mode, zoom with `Ctrl` + scroll.

## Things to try

- [x] Toggle dark mode with `Ctrl+Shift+L`
- [ ] Click this checkbox — it updates the Markdown
- [ ] Press `Enter` twice below the last item, then type `## ` to start a heading
- [ ] Zoom in and out with `Ctrl` + `+` / `-`

> 💡 Quotes that start with an emoji become callouts.

> A plain quote looks like this.

### Code

```python
def hello(name: str) -> str:
    return f"Hello, {name}!"
```

### Table

| Shortcut | Action |
| --- | --- |
| `Ctrl+E` | Toggle page / source |
| `# ` `- ` `1. ` `[] ` `> ` ```` ``` ```` | Shortcuts at the start of a line |
| `Ctrl+B` / `Ctrl+I` / `` Ctrl+` `` | Bold / italic / code |
| `Ctrl+click` | Open a link |
| `Ctrl+S` | Save |
| `Ctrl+B` / `Ctrl+I` | Bold / italic |

1. Ordered lists continue automatically when you press Enter.
2. An empty item ends the list and starts a new block.

---

Made with GTK and WebKitGTK. Ctrl+click a link to open it: [forhadkhan.com](https://forhadkhan.com)
