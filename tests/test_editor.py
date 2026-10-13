#!/usr/bin/env python3
"""Editor test suite for Notemark.

The editor lives in a WebKit page, so the tests drive that page directly: an
offscreen WebView loads ui/index.html, each case runs JavaScript against it
(typing through execCommand, the same path the browser uses for real input) and
asserts on the Markdown that comes back out.

Requires an X display. On a headless machine run it under Xvfb:

    xvfb-run -a python3 tests/test_editor.py
"""
import json
import os
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import GLib, Gtk, WebKit2  # noqa: E402

HERE = Path(__file__).resolve().parent.parent
UI_INDEX = HERE / "ui" / "index.html"
SAMPLE = (HERE / "sample.md").read_text()
TIMEOUT_MS = 40000
# Delay between cases. Must exceed the editor's own debounces: 150 ms for the
# page-to-Markdown sync and 120 ms for the split-mode re-render.
STEP_DELAY_MS = 200

# JavaScript helpers injected into the page: caret placement and typing.
HELPERS = """
window.T = {
  page: document.getElementById('page'),
  caretEnd(el) {
    const r = document.createRange(); r.selectNodeContents(el); r.collapse(false);
    const s = getSelection(); s.removeAllRanges(); s.addRange(r);
  },
  caretStart(el) {
    const r = document.createRange(); r.selectNodeContents(el); r.collapse(true);
    const s = getSelection(); s.removeAllRanges(); s.addRange(r);
  },
  select(node, from, to) {
    const r = document.createRange(); r.setStart(node, from); r.setEnd(node, to);
    const s = getSelection(); s.removeAllRanges(); s.addRange(r);
  },
  type(str) { for (const ch of str) document.execCommand('insertText', false, ch); },
  enter() { document.execCommand('insertParagraph'); },
  blockTag() {
    let n = getSelection().anchorNode;
    if (n && n.nodeType === 3) n = n.parentNode;
    while (n && n !== T.page) {
      if (/^(P|DIV|H[1-6]|LI|BLOCKQUOTE|PRE)$/.test(n.nodeName)) return n.nodeName;
      n = n.parentNode;
    }
    return 'none';
  },
  last() { return T.page.lastElementChild; },
  paste(text) {
    const dt = new DataTransfer(); dt.setData('text/plain', text);
    T.page.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
  },
};
'ready'
"""

MD = "Notemark.toMarkdown()"


def build_cases():
    """Return [(name, javascript, check)]; check receives the JS result as a string."""
    c = []

    def case(name, js, check):
        c.append((name, js, check))

    # --- Setup and rendering -------------------------------------------------
    case("helpers load", HELPERS, lambda o: o == "ready")
    case("init page mode", "Notemark.init({theme:'light', mode:'page'}); document.documentElement.dataset.mode",
         lambda o: o == "page")
    case("sample renders into an editable page",
         "Notemark.load(%s); T.page.isContentEditable + '|' + document.querySelector('#page h1').textContent" % json.dumps(SAMPLE),
         lambda o: o == "true|Welcome to Notemark")
    case("an untouched document round-trips byte for byte",
         "%s === %s" % (MD, json.dumps(SAMPLE)), lambda o: o == "true")

    # --- Typing into the rendered page ---------------------------------------
    case("typing into a paragraph reaches the Markdown",
         "var p = document.querySelector('#page p'); T.page.focus(); T.caretEnd(p); T.type(' PLUS'); " + MD,
         lambda o: "side by side. PLUS" in o)
    case("the page is not re-rendered while typing",
         "document.querySelector('#page h1').textContent + '|' + document.querySelectorAll('#page h1').length",
         lambda o: o == "Welcome to Notemark|1")

    # --- Markdown shortcuts --------------------------------------------------
    case("'# ' becomes a level 1 heading",
         "Notemark.load(''); T.caretStart(T.last()); T.type('# '); T.blockTag()", lambda o: o == "H1")
    case("Enter after a heading returns to a paragraph",
         "T.type('Hello'); T.enter(); T.blockTag() + '|' + " + MD, lambda o: o.startswith("P|# Hello"))
    case("'- ' starts a bullet list", "T.type('- '); T.blockTag()", lambda o: o == "LI")
    case("a list continues on Enter and ends on an empty item",
         "T.type('one'); T.enter(); T.type('two'); T.enter(); T.enter(); T.blockTag() + '|' + " + MD,
         lambda o: o.startswith("P|") and "- one\n- two" in o)
    case("'[] ' starts a task list",
         "T.type('[] '); document.querySelector('#page li.task input[type=checkbox]') ? 'task' : T.blockTag()",
         lambda o: o == "task")
    case("a task list continues with fresh checkboxes",
         "T.type('todo'); T.enter(); T.type('todo2'); T.enter(); T.enter(); " + MD,
         lambda o: "- [ ] todo\n- [ ] todo2" in o)
    case("toggling a checkbox writes [x] into the Markdown",
         "var cb = document.querySelector('#page li.task input'); cb.checked = true;"
         " cb.dispatchEvent(new Event('change', {bubbles: true})); " + MD,
         lambda o: "- [x] todo\n- [ ] todo2" in o)
    case("'> ' starts a quote", "T.type('> '); T.blockTag()", lambda o: o == "BLOCKQUOTE")
    case("a quote ends on an empty line",
         "T.type('quoted'); T.enter(); T.enter(); T.blockTag() + '|' + " + MD,
         lambda o: o.startswith("P|") and "> quoted" in o)
    case("'1. ' starts an ordered list",
         "T.type('1. '); T.blockTag() + '|' + (getSelection().anchorNode.parentElement.closest('ol') ? 'OL' : 'no')",
         lambda o: o == "LI|OL")
    case("'```js ' opens a code block",
         "T.type('first'); T.enter(); T.enter(); T.type('```js '); T.blockTag()", lambda o: o == "PRE")
    case("a code block keeps its line breaks and ends on an empty line",
         "T.type('x()'); T.enter(); T.type('y()'); T.enter(); T.enter(); T.blockTag() + '|' + " + MD,
         lambda o: o.startswith("P|") and "```js\nx()\ny()\n```" in o)
    case("Markdown punctuation typed as text is escaped",
         "T.type('some **bold** text'); " + MD, lambda o: "some \\*\\*bold\\*\\* text" in o)

    # --- Inline formatting ---------------------------------------------------
    case("bold applied to a selection becomes **",
         "Notemark.load('some bold text\\n'); var p = document.querySelector('#page p');"
         " T.select(p.firstChild, 5, 9); document.execCommand('bold');"
         " T.page.dispatchEvent(new Event('input')); " + MD,
         lambda o: o == "some **bold** text\n")
    case("italic nests inside bold",
         "var b = document.querySelector('#page strong, #page b'); T.select(b.firstChild, 0, b.firstChild.length);"
         " document.execCommand('italic'); T.page.dispatchEvent(new Event('input')); " + MD,
         lambda o: o == "some ***bold*** text\n")

    # --- Structural blocks loaded from a file --------------------------------
    case("Enter inside a loaded code block adds a line, an empty line exits",
         "Notemark.load('```py\\na()\\nb()\\n```\\n\\n> q1\\n');"
         " var code = document.querySelector('#page pre code'); T.caretEnd(code);"
         " T.enter(); T.type('c()'); T.enter(); T.enter(); T.type('after'); " + MD,
         lambda o: o == "```py\na()\nb()\nc()\n```\n\nafter\n\n> q1\n")
    case("Enter inside a loaded quote adds a line, an empty line exits",
         "var q = document.querySelector('#page blockquote'); T.caretEnd(q);"
         " T.enter(); T.type('q2'); T.enter(); T.enter(); T.type('out'); " + MD,
         lambda o: "> q1" in o and "q2" in o and o.rstrip().endswith("out"))

    # --- Document level ------------------------------------------------------
    case("pasted Markdown is rendered, not inserted literally",
         "Notemark.load(''); T.caretStart(T.last()); T.paste('## Pasted\\n\\n- a\\n- b');"
         " (document.querySelector('#page h2') || {}).textContent + '|' + document.querySelectorAll('#page li').length",
         lambda o: o == "Pasted|2")
    case("an empty document offers a paragraph to type into",
         "Notemark.load(''); document.querySelector('#page p') ? 'p' : 'none'", lambda o: o == "p")
    case("re-serialising is idempotent",
         "Notemark.load(%s); var md = %s; Notemark.load(md); (%s === md)" % (json.dumps(SAMPLE), MD, MD),
         lambda o: o == "true")
    case("every construct in the sample survives a round trip",
         "var md = %s; ['# Welcome to Notemark', '- [x] Toggle dark mode', '```python',"
         " '| `Ctrl+E` |', '> \\u{1F4A1} Quotes', '---', '[forhadkhan.com](https://forhadkhan.com)']"
         ".filter(s => md.indexOf(s) < 0).join(', ')" % MD,
         lambda o: o == "")
    case("an edited document keeps its shape",
         "Notemark.load(%s); var p = document.querySelector('#page p'); T.caretEnd(p); T.type('!');"
         " var md = %s; Notemark.load(md);"
         " (%s === md) + '|' + md.split('\\n').length + '|' + %s.split('\\n').length"
         % (json.dumps(SAMPLE), MD, MD, json.dumps(SAMPLE)),
         lambda o: o.startswith("true|") and abs(int(o.split("|")[1]) - int(o.split("|")[2])) <= 3)

    # --- Source and split modes ---------------------------------------------
    case("split mode renders source edits into the page",
         "Notemark.setMode('split'); var ed = document.getElementById('editor');"
         " ed.value = '# From source'; ed.dispatchEvent(new Event('input', {bubbles: true})); 1",
         lambda o: o == "1")
    case("split mode shows the source heading",
         "(document.querySelector('#page h1') || {}).textContent", lambda o: o == "From source")
    case("split mode writes page edits back to the source",
         "var h = document.querySelector('#page h1'); T.caretEnd(h); T.type('!');"
         " Notemark.toMarkdown(); document.getElementById('editor').value",
         lambda o: o == "# From source!\n")
    case("source mode hides the page", "Notemark.setMode('source'); document.documentElement.dataset.mode",
         lambda o: o == "source")

    # --- Source pane fills its space; split layouts --------------------------
    case("source editor fills its pane",
         "var ed = document.getElementById('editor'), pane = document.getElementById('editor-pane');"
         " var e = ed.getBoundingClientRect(), p = pane.getBoundingClientRect();"
         " (Math.abs(e.width - p.width) < 2) + '|' + (Math.abs(e.height - p.height) < 2) + '|' + (e.width > 400)",
         lambda o: o == "true|true|true")
    case("source editor is monospace and theme-coloured",
         "var cs = getComputedStyle(document.getElementById('editor'));"
         " Notemark.setTheme('dark'); var d = getComputedStyle(document.getElementById('editor')).backgroundColor;"
         " Notemark.setTheme('light'); var l = getComputedStyle(document.getElementById('editor')).backgroundColor;"
         " /mono/i.test(cs.fontFamily) + '|' + (d !== l)",
         lambda o: o == "true|true")
    case("split puts the editor left and the preview right",
         "Notemark.setMode('split'); Notemark.setLayout('lr');"
         " var e = document.getElementById('editor-pane').getBoundingClientRect(),"
         " p = document.getElementById('page-pane').getBoundingClientRect();"
         " (e.right <= p.left + 1) + '|' + (Math.abs(e.width - p.width) < 4) + '|' + (p.width > 300)",
         lambda o: o == "true|true|true")
    case("layout rl swaps the panes",
         "Notemark.setLayout('rl');"
         " document.getElementById('page-pane').getBoundingClientRect().right <="
         " document.getElementById('editor-pane').getBoundingClientRect().left + 1",
         lambda o: o == "true")
    case("layout tb stacks editor above preview",
         "Notemark.setLayout('tb');"
         " var e = document.getElementById('editor-pane').getBoundingClientRect(),"
         " p = document.getElementById('page-pane').getBoundingClientRect();"
         " (e.bottom <= p.top + 1) + '|' + (Math.abs(e.width - p.width) < 2) + '|' + (e.height > 100 && p.height > 100)",
         lambda o: o == "true|true|true")
    case("layout bt puts the editor below",
         "Notemark.setLayout('bt');"
         " document.getElementById('page-pane').getBoundingClientRect().bottom <="
         " document.getElementById('editor-pane').getBoundingClientRect().top + 1",
         lambda o: o == "true")
    case("setSplit resizes the panes and clamps",
         "Notemark.setLayout('lr'); Notemark.setSplit(30);"
         " var w = document.getElementById('workspace').getBoundingClientRect().width,"
         " e = document.getElementById('editor-pane').getBoundingClientRect().width;"
         " Notemark.setSplit(2); var c = document.getElementById('editor-pane').getBoundingClientRect().width;"
         " (Math.abs(e / w - 0.30) < 0.02) + '|' + (Math.abs(c / w - 0.15) < 0.02)",
         lambda o: o == "true|true")
    case("dragging the divider resizes and reports the ratio",
         "var dv = document.getElementById('divider'), r = document.getElementById('workspace').getBoundingClientRect();"
         " dv.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0}));"
         " document.dispatchEvent(new MouseEvent('mousemove', {clientX: r.left + r.width * 0.65, clientY: r.top + 10}));"
         " document.dispatchEvent(new MouseEvent('mouseup'));"
         " var e = document.getElementById('editor-pane').getBoundingClientRect().width;"
         " Notemark.setMode('page'); Math.abs(e / r.width - 0.65) < 0.02",
         lambda o: o == "true")

    # --- Undo / redo (Ctrl+Z, Ctrl+Shift+Z, Ctrl+Y) --------------------------
    key = ("function K(el, k, shift) { el.dispatchEvent(new KeyboardEvent('keydown',"
           " {key: k, ctrlKey: true, shiftKey: !!shift, bubbles: true, cancelable: true})); } ")
    case("Ctrl+Z undoes typing in the page",
         key + "Notemark.setMode('page'); Notemark.load('hello\\n'); T.page.focus(); T.caretEnd(document.querySelector('#page p'));"
         " T.type(' world'); var before = " + MD + "; K(T.page, 'z'); before.trim() + '|' + " + MD,
         lambda o: o == "hello world|hello\n")
    case("Ctrl+Y redoes it", "K(T.page, 'y'); " + MD, lambda o: o == "hello world\n")
    case("Ctrl+Shift+Z redoes too",
         "K(T.page, 'z'); var u = " + MD + "; K(T.page, 'Z', true); u + '|' + " + MD,
         lambda o: o == "hello\n|hello world\n")
    case("Ctrl+Z undoes typing in the source editor",
         key + "Notemark.setMode('source'); var ed = document.getElementById('editor'); ed.focus();"
         " ed.setSelectionRange(ed.value.length, ed.value.length); document.execCommand('insertText', false, 'abc');"
         " var typed = ed.value; K(ed, 'z'); typed.length - ed.value.length",
         lambda o: o == "3")
    case("Ctrl+Y redoes in the source editor",
         "var ed = document.getElementById('editor'); K(ed, 'y'); ed.value.endsWith('abc')", lambda o: o == "true")

    # --- CSV documents -------------------------------------------------------
    case("CSV parsing handles quotes, embedded delimiters and newlines",
         "JSON.stringify(NotemarkCsv.parse('a,b\\n\"x,1\",\"he said \"\"hi\"\"\\nthere\"\\n').rows)",
         lambda o: json.loads(o) == [["a", "b"], ["x,1", 'he said "hi"\nthere']])
    case("the delimiter is detected (semicolon, tab, pipe)",
         "['a;b;c', 'a\\tb\\tc', 'a|b|c', 'a,b'].map(NotemarkCsv.detectDelimiter).join('')",
         lambda o: o == ";\t|,")
    case("stringify quotes only where needed and round-trips",
         "var t = 'a,b\\n\"x,1\",\"q\"\"q\"\\n'; (NotemarkCsv.stringify(NotemarkCsv.parse(t).rows) === t)",
         lambda o: o == "true")
    case("a CSV document shows a table and keeps its text",
         "Notemark.setMode('page'); Notemark.load('name,age\\nAda,36\\nLin,41\\n', {kind: 'csv'});"
         " document.documentElement.dataset.kind + '|' + document.querySelectorAll('#grid tbody tr').length"
         " + '|' + document.querySelectorAll('#grid thead .cell').length + '|' + JSON.stringify(Notemark.toMarkdown())",
         lambda o: o == 'csv|2|2|"name,age\\nAda,36\\nLin,41\\n"')
    case("editing a cell updates the CSV text",
         "var c = document.querySelector('#grid tbody .cell[data-r=\"1\"][data-c=\"1\"]'); c.focus(); c.textContent = '37';"
         " c.dispatchEvent(new Event('input', {bubbles: true})); Notemark.toMarkdown()",
         lambda o: o == "name,age\nAda,37\nLin,41\n")
    case("Ctrl+Z undoes a table edit",
         "var c = document.querySelector('#grid tbody .cell[data-r=\"1\"][data-c=\"1\"]'); c.blur();"
         " c.focus(); c.dispatchEvent(new KeyboardEvent('keydown', {key: 'z', ctrlKey: true, bubbles: true, cancelable: true}));"
         " Notemark.toMarkdown()",
         lambda o: o == "name,age\nAda,36\nLin,41\n")
    case("adding a row and a column grows the table",
         "document.querySelector('[data-act=row-add]').click(); document.querySelector('[data-act=col-add]').click();"
         " JSON.stringify(Notemark.toMarkdown())",
         lambda o: json.loads(o) == "name,age,\nAda,36,\n,,\nLin,41,\n" or json.loads(o).count("\n") == 4)
    case("source edits re-render the table in split mode",
         "Notemark.setMode('split'); var ed = document.getElementById('editor'); ed.value = 'x,y\\n1,2\\n3,4\\n5,6\\n';"
         " ed.dispatchEvent(new Event('input', {bubbles: true})); 1", lambda o: o == "1")
    case("the table follows the source", "document.querySelectorAll('#grid tbody tr').length",
         lambda o: o == "3")
    case("Markdown documents are unaffected afterwards",
         "Notemark.setMode('page'); Notemark.load('# Back\\n'); document.documentElement.dataset.kind + '|' +"
         " (document.querySelector('#page h1') || {}).textContent", lambda o: o == "md|Back")

    # --- Safety --------------------------------------------------------------
    case("script tags in Markdown are stripped",
         "Notemark.setMode('page');"
         " Notemark.load('# Safe\\n\\n<script>document.title=\"pwned\"</script>\\n\\n<img src=x onerror=\"alert(1)\">\\n');"
         " var h = T.page.innerHTML;"
         " (h.indexOf('<script') < 0) + '|' + (h.indexOf('onerror') < 0) + '|' + document.title",
         lambda o: o == "true|true|Notemark")

    return c


def main():
    cases = build_cases()
    results = []

    manager = WebKit2.UserContentManager()
    web = WebKit2.WebView.new_with_user_content_manager(manager)
    web.get_settings().set_allow_file_access_from_file_urls(True)
    if os.environ.get("NOTEMARK_DEBUG"):
        web.get_settings().set_enable_write_console_messages_to_stdout(True)

    window = Gtk.OffscreenWindow()
    window.set_default_size(1000, 800)
    window.add(web)
    window.show_all()

    pending = list(cases)

    def run_next():
        if not pending:
            Gtk.main_quit()
            return
        name, js, check = pending.pop(0)

        def done(view, result):
            try:
                value = view.evaluate_javascript_finish(result)
                out = value.to_string() if value else None
            except GLib.Error as exc:
                out = "javascript error: %s" % exc.message
            try:
                ok = bool(check(out))
            except Exception as exc:  # a check that raises is a failure, not a crash
                ok = False
                out = "%s (check raised %s)" % (out, exc)
            results.append((name, ok, out))
            GLib.timeout_add(STEP_DELAY_MS, run_next)

        web.evaluate_javascript(js, -1, None, None, None, done)

    def on_load(view, event):
        if event == WebKit2.LoadEvent.FINISHED:
            GLib.timeout_add(250, run_next)

    web.connect("load-changed", on_load)
    web.load_uri(GLib.filename_to_uri(str(UI_INDEX), None))
    GLib.timeout_add(TIMEOUT_MS, lambda: (results.append(("suite", False, "timed out")), Gtk.main_quit()))
    Gtk.main()

    failed = 0
    for name, ok, out in results:
        if ok:
            print("  ok   %s" % name)
        else:
            failed += 1
            print("  FAIL %s\n         got: %r" % (name, str(out)[:400]))

    missing = len(cases) - len(results)
    if missing > 0:
        failed += missing
        print("  FAIL %d case(s) did not run" % missing)

    print("\n%d passed, %d failed, %d total" % (len(results) - failed, failed, len(cases)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
