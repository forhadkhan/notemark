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
import random
import re
import sys
from pathlib import Path
from types import SimpleNamespace

# The clipboard needs a real X selection owner: on a Wayland session GTK would pick that backend even under Xvfb.
os.environ.setdefault("GDK_BACKEND", "x11")

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gdk, GLib, Gtk, WebKit2  # noqa: E402

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import notemark as nm  # noqa: E402
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
  // WebKitGTK sends Shift+Tab with key "Unidentified"; code and keyCode are what identify it.
  tab(shift) {
    const ev = new KeyboardEvent('keydown', {key: shift ? 'Unidentified' : 'Tab', code: 'Tab', keyCode: 9, shiftKey: shift, bubbles: true, cancelable: true});
    T.page.dispatchEvent(ev);
  },
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

# Messages the page posts to the host (the test plays the host: see on_message in main).
MESSAGES = []


def last_msg(kind):
    """The newest message of a kind; the page also posts change and cursor messages between them."""
    return [m for m in MESSAGES if m.get("type") == kind][-1]


# The word count the editor used before the single-pass counter: trim, then split on runs of JS whitespace.
JS_SPACE = "[\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]"


def old_words(text):
    t = re.sub("^%s+|%s+$" % (JS_SPACE, JS_SPACE), "", text)
    return len(re.split("%s+" % JS_SPACE, t)) if t else 0


# What the status bar counts must not depend on how the text got there; every one of these once went through split().
COUNT_CORPUS = [
    "", " ", "\n\n", "one", "  padded  words  ", "a\tb\nc\r\nd", "tabs\t\t\tand   spaces",
    "naïve café – ünïcode 日本語 テスト", "emoji 😀 and 👨‍👩‍👧 joined", "no\u00a0break\u2003em\u3000ideo\u2028sep\u2029para\ufeffbom",
    "\u200bzero width is not space\u200b", "\u0085next line\u0085 and \u180e mongolian are not JS space",
    "# Title\n\nSome *prose* with `code`.\n\n- one\n- two\n", "x" * 5000, " ".join(["w"] * 3000),
]


def host_msgs(since, kind="changed"):
    return [m for m in MESSAGES[since:] if m.get("type") == kind]


# The app's copy_text writes the CLIPBOARD selection; the test points it at a private one so a run never
# overwrites what the person has copied.
TEST_SELECTION = Gdk.Atom.intern("NOTEMARK_TEST_CLIPBOARD", False)
nm.Gtk = SimpleNamespace(Clipboard=SimpleNamespace(get=lambda atom: Gtk.Clipboard.get(TEST_SELECTION)))


def clipboard_text():
    return Gtk.Clipboard.get(TEST_SELECTION).wait_for_text()


# CSV helpers: load a 3x2 table, select a cell, open its right-click menu and click an entry.
CSV_LOAD = "Notemark.setMode('page'); Notemark.load('a,b\\n1,2\\n3,4\\n', {kind: 'csv'}); "


def cell_sel(r, c):
    return '#grid %s .cell[data-r="%d"][data-c="%d"]' % ("thead" if r == 0 else "tbody", r, c)


def big_csv_rows(n=2500):
    """A header plus n-1 rows of 6 columns, with quoted commas, doubled quotes, newlines, unicode and empty cells."""
    rows = [["id", "name", "note", "city", "qty", "flag"]]
    for i in range(1, n):
        rows.append([str(i), "Name %d" % i,
                     ["plain", 'say "hi", twice', "two\nlines", "caf\u00e9 \u2713 \u65e5\u672c", "", " padded "][i % 6],
                     "needle-%d" % i if i == 1234 else "City %d" % (i % 37), str(i * 7 % 1000), "" if i % 5 else "x"])
    return rows


def csv_quote(value):
    return '"' + value.replace('"', '""') + '"' if re.search(r'["\r\n,]|^\s|\s$', value) else value


def csv_text(rows):
    return "".join(",".join(csv_quote(c) for c in r) + "\n" for r in rows)


def right_click(sel):
    return ("(function(){var el=document.querySelector('%s');el.focus();"
            "el.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:30,clientY:30}));})();") % sel


def menu_click(label):
    return ("(function(){var b=[].slice.call(document.querySelectorAll('.ctx-menu button'))"
            ".filter(function(x){return x.textContent.trim()==='%s';})[0];"
            "if(!b)return 'missing';if(b.disabled)return 'disabled';b.click();return 'clicked';})()") % label


def build_cases():
    """Return [(name, javascript, check)]; check receives the JS result as a string."""
    c = []

    def case(name, js, check, wait=0):
        c.append((name, js, check, wait))

    # --- Setup and rendering -------------------------------------------------
    case("helpers load", HELPERS, lambda o: o == "ready")
    case("the Markdown page loads without a script error or console error", "JSON.stringify(window.__errors)",
         lambda o: o == "[]")
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

    # --- Re-rendering and chunked parsing -------------------------------------
    case("switching mode with unchanged text keeps the rendered page",
         "Notemark.setMode('page'); Notemark.load('# Keep\\n\\ntext\\n'); window.__h1 = document.querySelector('#page h1');"
         " Notemark.setMode('source'); Notemark.setMode('split'); Notemark.setMode('page');"
         " document.querySelector('#page h1') === window.__h1", lambda o: o == "true")
    case("an edit in the page shows in the source after switching",
         "T.page.focus(); T.caretEnd(document.querySelector('#page h1')); T.type('!'); Notemark.setMode('source');"
         " document.getElementById('editor').value", lambda o: o == "# Keep!\n\ntext\n")
    case("and in the page after switching back",
         "Notemark.setMode('page'); document.querySelector('#page h1').textContent", lambda o: o == "Keep!")
    case("loading the same text again drops unsaved page edits",
         "Notemark.load('# Keep\\n\\ntext\\n'); T.page.focus(); T.caretEnd(document.querySelector('#page h1')); T.type('?');"
         " Notemark.load('# Keep\\n\\ntext\\n'); document.querySelector('#page h1').textContent", lambda o: o == "Keep")
    case("a long document is parsed in chunks with the same result",
         "var big = ''; for (var i = 0; i < 300; i++) big += '## Part ' + i + '\\n\\nSome *text* here, `code` and a [link][r].\\n\\n- one\\n- two\\n\\n```\\na\\n\\nb\\n```\\n\\n';"
         " big += '[r]: https://example.com/\\n';"
         " var whole = marked.parse(big); var parts = NotemarkChunk.split(big, NotemarkChunk.TARGET);"
         " (big.length > NotemarkChunk.MIN_LENGTH) + '|' + (parts && parts.length > 2) + '|' + (NotemarkChunk.parse(big, marked.parse) === whole)",
         lambda o: o == "true|true|true")
    case("a long document renders into the page",
         "Notemark.setMode('page'); Notemark.load(big); document.querySelectorAll('#page h2').length + '|' +"
         " document.querySelectorAll('#page a[href=\"https://example.com/\"]').length",
         lambda o: o == "300|300")
    case("back to the source view for the cases below",
         "Notemark.load('# From source!\\n'); Notemark.setMode('source'); document.documentElement.dataset.mode",
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

    # --- List indent (Tab / Shift+Tab) ---------------------------------------
    case("Tab nests a list item under the one above",
         "Notemark.setMode('page', true); Notemark.load('- a\\n- b\\n'); T.page.focus(); T.caretEnd(T.page.querySelectorAll('li')[1]); "
         "T.tab(false); " + MD,
         lambda o: o.startswith("- a\n  - b"))
    case("the caret stays in the moved item",
         "T.blockTag()", lambda o: o == "LI")
    case("Shift+Tab (reported as key 'Unidentified') lifts it back out",
         "T.tab(true); " + MD, lambda o: o.startswith("- a\n- b"))
    case("Tab on the first item changes nothing",
         "Notemark.load('- a\\n- b\\n'); T.caretEnd(T.page.querySelectorAll('li')[0]); T.tab(false); " + MD,
         lambda o: o.startswith("- a\n- b"))

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
    case("loading a CSV document raises no script error or console error", "JSON.stringify(window.__errors)",
         lambda o: o == "[]")
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
    case("right-click inserts a column to the left of the cell",
         CSV_LOAD + right_click(cell_sel(1, 1)) + "var res = " + menu_click("Insert column left") + "; res + '|' + " + MD,
         lambda o: o == "clicked|a,,b\n1,,2\n3,,4\n")
    case("right-click deletes the column",
         CSV_LOAD + right_click(cell_sel(0, 1)) + "var res = " + menu_click("Delete column") + "; res + '|' + " + MD,
         lambda o: o == "clicked|a\n1\n3\n")
    case("the header row cannot get an insert above or a delete row",
         CSV_LOAD + right_click(cell_sel(0, 0)) +
         "var off = [].slice.call(document.querySelectorAll('.ctx-menu button[disabled]')).map(function(b){return b.textContent.trim();}); off.join(',')",
         lambda o: o == "Insert row above,Delete row")
    case("Escape closes the right-click menu and returns to the cell",
         CSV_LOAD + right_click(cell_sel(1, 1)) +
         "document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true, cancelable: true}));"
         " (document.querySelector('.ctx-menu') ? 'open' : 'closed') + '|' + document.activeElement.dataset.r + document.activeElement.dataset.c",
         lambda o: o == "closed|11")
    case("Shift+F10 opens the menu on the focused cell, with focus in it",
         CSV_LOAD + "var el = document.querySelector('" + cell_sel(1, 0) + "'); el.focus();"
         " el.dispatchEvent(new KeyboardEvent('keydown', {key: 'F10', shiftKey: true, bubbles: true, cancelable: true}));"
         " (document.querySelector('.ctx-menu') ? 'open' : 'closed') + '|' + document.activeElement.textContent.trim()",
         lambda o: o == "open|Insert row above")
    case("the toolbar delete row leaves the header alone",
         CSV_LOAD + "document.querySelector('" + cell_sel(0, 0) + "').focus();"
         " document.querySelector('[data-act=row-del]').click(); " + MD,
         lambda o: o == "a,b\n1,2\n3,4\n")
    case("inserting a column before the sorted column keeps the sort on its column",
         CSV_LOAD + "document.querySelector('[data-sort=\"1\"]').click();" + right_click(cell_sel(1, 0)) +
         "var res = " + menu_click("Insert column left") + "; res + '|' + "
         "(document.querySelector('#grid thead th[data-c=\"2\"] .sort') ? 'sorted' : 'lost')",
         lambda o: o == "clicked|sorted")
    case("source edits re-render the table in split mode",
         "Notemark.setMode('split'); var ed = document.getElementById('editor'); ed.value = 'x,y\\n1,2\\n3,4\\n5,6\\n';"
         " ed.dispatchEvent(new Event('input', {bubbles: true})); 1", lambda o: o == "1")
    case("the table follows the source", "document.querySelectorAll('#grid tbody tr').length",
         lambda o: o == "3")
    # Large tables list at most 2000 rows and render only a window of them; the data model still holds all of them.
    BIG_ROWS = big_csv_rows()
    BIG = csv_text(BIG_ROWS)
    SCROLL = "document.querySelector('.grid-scroll')"
    DATA_ROWS = "document.querySelectorAll('#grid tbody tr:not(.vs)')"
    FIRST_LAST = ("(function(){var r=[].map.call(document.querySelectorAll('#grid tbody tr:not(.vs) .cell[data-c=\"0\"]'),"
                  "function(e){return +e.dataset.r;});return r.length+'|'+r[0]+'|'+r[r.length-1];})()")
    LAST_LISTED = 2000          # the 2,000th data row, rows[2000]: the cap

    case("a 2500-row table puts only a window of rows in the page",
         "Notemark.setMode('page'); Notemark.load(%s, {kind: 'csv'}); %s.length + '|' + document.querySelector('.grid-info').textContent"
         % (json.dumps(BIG), DATA_ROWS),
         lambda o: 5 < int(o.split("|")[0]) < 100 and o.split("|")[1].startswith("2499 rows") and
         o.split("|")[1].endswith("2000 shown"))
    case("an unedited large table serialises byte for byte, past the cap too", "%s === %s" % (MD, json.dumps(BIG)),
         lambda o: o == "true")
    case("scrolling down renders the later rows", SCROLL + ".scrollTop = 30000; 1", lambda o: o == "1")
    case("the rows around the scroll position are in the page", FIRST_LAST,
         lambda o: 5 < int(o.split("|")[0]) < 100 and int(o.split("|")[1]) > 300)
    case("scrolling to the end shows the last listed row",
         SCROLL + ".scrollTop = " + SCROLL + ".scrollHeight; 1", lambda o: o == "1")
    case("the last listed row is the 2000th, and no row past it is in the page", FIRST_LAST + " + '|' + "
         "!!document.querySelector('#grid tbody .cell[data-r=\"2001\"]')",
         lambda o: o.split("|")[2] == str(LAST_LISTED) and o.split("|")[3] == "false")
    case("editing a cell in the last listed row updates the model",
         "var el = document.querySelector('#grid tbody .cell[data-r=\"2000\"][data-c=\"2\"]'); el.focus();"
         " el.textContent = 'edited, \"x\"'; el.dispatchEvent(new Event('input', {bubbles: true})); " + MD,
         lambda o: o == csv_text(BIG_ROWS[:2000] + [BIG_ROWS[2000][:2] + ['edited, "x"'] + BIG_ROWS[2000][3:]] +
                                 BIG_ROWS[2001:]))
    case("Tab out of the last cell of the last listed row adds no row nobody can see",
         "var el = document.querySelector('#grid tbody .cell[data-r=\"2000\"][data-c=\"5\"]'); el.focus();"
         " var ev = new KeyboardEvent('keydown', {key: 'Tab', bubbles: true, cancelable: true}); el.dispatchEvent(ev);"
         " ev.defaultPrevented + '|' + document.querySelector('.grid-info').textContent",
         lambda o: o.startswith("true|2499 rows"), wait=250)
    case("...and saving still writes every row, the ones past the cap too",
         MD, lambda o: o == csv_text(BIG_ROWS[:2000] + [BIG_ROWS[2000][:2] + ['edited, "x"'] + BIG_ROWS[2000][3:]] +
                                      BIG_ROWS[2001:]))
    case("a row past the cap is not rendered, and an edit made in the model reaches the text",
         "(function(){var host = document.createElement('div'); document.body.appendChild(host);"
         " var g = NotemarkCsv.create(host, {onEdit: function(){}, onChange: function(){}, copy: function(){}});"
         " g.load(%s); var absent = !host.querySelector('.cell[data-r=\"2300\"]');"
         " g.rows[2300][2] = 'edited, \"y\"'; var out = absent + '|' + g.text; host.remove(); return out;})()"
         % json.dumps(BIG),
         lambda o: o == "true|" + csv_text(BIG_ROWS[:2300] + [BIG_ROWS[2300][:2] + ['edited, "y"'] + BIG_ROWS[2300][3:]] +
                                           BIG_ROWS[2301:]))
    case("scrolling back to the top", SCROLL + ".scrollTop = 0; 1", lambda o: o == "1")
    case("keyboard navigation continues past the rendered window",
         "var el = document.querySelector('" + cell_sel(1, 0) + "'); el.focus();"
         " for (var i = 0; i < 150; i++) document.activeElement.dispatchEvent("
         "new KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true, cancelable: true}));"
         " document.activeElement.dataset.r + '|' + document.activeElement.dataset.c",
         lambda o: o == "151|0")
    case("no script error or console error was raised by any of it", "JSON.stringify(window.__errors)",
         lambda o: o == "[]")
    case("the filter finds a row that is far outside the window",
         "var f = document.querySelector('.grid-filter'); f.value = 'needle-1234';"
         " f.dispatchEvent(new Event('input', {bubbles: true})); 1", lambda o: o == "1")
    case("only the matching row is listed",
         DATA_ROWS + ".length + '|' + " + DATA_ROWS + "[0].querySelector('.cell[data-c=\"1\"]').textContent",
         lambda o: o == "1|Name 1234", wait=150)
    case("clearing the filter brings the window back",
         "var f = document.querySelector('.grid-filter'); f.value = ''; f.dispatchEvent(new Event('input', {bubbles: true})); 1",
         lambda o: o == "1")
    case("the full table is listed again", DATA_ROWS + ".length",
         lambda o: 5 < int(o) < 100, wait=150)
    case("the sticky header cells are opaque, selected one included",
         "document.querySelector('" + cell_sel(0, 0) + "').focus(); " + SCROLL + ".scrollTop = 3517;"
         " [].map.call(document.querySelectorAll('#grid thead th'), function(th){"
         "var b = getComputedStyle(th).backgroundColor; return (th.classList.contains('sel') ? 'sel:' : '') +"
         " (/^rgb\\(/.test(b) ? 'opaque' : b);}).join(',')",
         lambda o: "sel:opaque" in o and "transparent" not in o and "rgba" not in o)

    case("Markdown documents are unaffected afterwards",
         "Notemark.setMode('page'); Notemark.load('# Back\\n'); document.documentElement.dataset.kind + '|' +"
         " (document.querySelector('#page h1') || {}).textContent", lambda o: o == "md|Back")

    # --- Copy button on code blocks -----------------------------------------
    COPY_DOC = "Intro\n\n```js\nconst a = 1;\nb()\n```\n\nOutro\n"
    HOVER = ("var pre = document.querySelector('#page pre'); var btn = document.getElementById('code-copy');"
             " pre.querySelector('code').dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));")
    CLICK = "btn.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));"
    case("the copy button is hidden until a code block is hovered",
         "Notemark.setMode('page'); Notemark.load(%s); document.getElementById('code-copy').hidden" % json.dumps(COPY_DOC),
         lambda o: o == "true")
    case("hovering a code block shows the copy button over its top-right corner",
         HOVER + " var r = pre.getBoundingClientRect(), b = btn.getBoundingClientRect();"
         " [btn.hidden, b.right <= r.right, r.right - b.right < 24, b.top >= r.top, b.top - r.top < 24,"
         " btn.parentNode === document.body, btn.title].join('|')",
         lambda o: o == "false|true|true|true|true|true|Copy code")
    case("hovering a paragraph does not move the button off the block at once",
         "document.querySelector('#page p').dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));"
         " document.getElementById('code-copy').hidden", lambda o: o == "false", wait=400)
    case("...but it hides shortly after the pointer leaves the block", "document.getElementById('code-copy').hidden",
         lambda o: o == "true")
    case("a mouse press on the button keeps the caret in the editor",
         HOVER + " T.page.focus(); T.caretEnd(document.querySelector('#page p'));"
         " var ev = new MouseEvent('mousedown', {bubbles: true, cancelable: true}); btn.dispatchEvent(ev);"
         " ev.defaultPrevented + '|' + (document.activeElement === T.page) + '|' + getSelection().anchorNode.textContent",
         lambda o: o == "true|true|Intro")
    case("clicking the button sends the exact code to the host",
         HOVER + CLICK + " 1", lambda o: o == "1")
    case("the host got the code with no trailing newline, and the button says Copied",
         "var btn = document.getElementById('code-copy'); btn.textContent + '|' + btn.classList.contains('copied')",
         lambda o: o == "Copied|true" and last_msg("copy") == {"type": "copy", "text": "const a = 1;\nb()", "note": "Copied the code block"})
    case("the host puts that text on the system clipboard",
         "1", lambda o: o == "1" and clipboard_text() == "const a = 1;\nb()", wait=1700)
    case("the label goes back to Copy after a moment",
         "document.getElementById('code-copy').textContent + '|' + document.getElementById('code-copy').classList.contains('copied')",
         lambda o: o == "Copy|false")
    case("hovering and copying leaves the Markdown and the page HTML untouched",
         HOVER + CLICK + " var h = T.page.innerHTML;"
         " (h.indexOf('code-copy') < 0) + '|' + (h.indexOf('>Copy<') < 0) + '|' + T.page.contains(btn) + '|' + (%s === %s)"
         % (MD, json.dumps(COPY_DOC)),
         lambda o: o == "true|true|false|true")
    case("HTML export does not contain the button",
         "Notemark.sendHtml('t'); 1",
         lambda o: o == "1")
    case("the exported document holds the code block but no button", "1", lambda o: o == "1" and "<pre><code" in last_msg("content")["text"]
         and "<button" not in last_msg("content")["text"] and ">Copy<" not in last_msg("content")["text"])
    case("a code block typed in page mode gets the button too",
         "Notemark.load(''); T.page.focus(); T.type('```js '); T.type('x()'); T.enter(); T.type('y()');"
         + HOVER + CLICK + " btn.hidden",
         lambda o: o == "false")
    case("...and copies what was typed",
         "1", lambda o: o == "1" and last_msg("copy")["text"] == "x()\ny()" and clipboard_text() == "x()\ny()")
    case("an empty typed code block copies nothing but text",
         "Notemark.load(''); T.page.focus(); T.type('```js '); " + HOVER + CLICK + " btn.hidden",
         lambda o: o == "false")
    case("the empty block copied an empty string", "1", lambda o: o == "1" and last_msg("copy")["text"] == "")
    case("the button is hidden in source mode and after a mode change",
         HOVER + " Notemark.setMode('source'); var hid = btn.hidden; Notemark.setMode('page'); hid + '|' + btn.hidden",
         lambda o: o == "true|true")
    case("the split view shows the button on its rendered side",
         "Notemark.load(%s); Notemark.setMode('split'); " % json.dumps(COPY_DOC) + HOVER + " btn.hidden",
         lambda o: o == "false")
    case("a CSV document never shows it",
         CSV_LOAD + "document.getElementById('code-copy').hidden", lambda o: o == "true")
    case("Markdown works after the copy tests",
         "Notemark.setMode('page'); Notemark.load('# Back\\n'); document.querySelector('#page h1').textContent",
         lambda o: o == "Back")

    # --- Typing in the source editor: cost per key, counts and caret ---------
    # The status bar counts and the caret position follow a keystroke after a short pause; the dirty flag does not wait.
    marks = {}

    def mark():
        marks["n"] = len(MESSAGES)
        return True

    case("source typing: setup", "Notemark.setMode('source'); Notemark.load('hello world', {saved: 'hello world'}); 'ok'",
         lambda o: o == "ok" and mark())
    case("the dirty flag turns on within the first keystroke, before counts are sent",
         "var ed = document.getElementById('editor'); ed.focus(); ed.setSelectionRange(11, 11); document.execCommand('insertText', false, '!'); 1",
         lambda o: [(m["dirty"], "words" in m) for m in host_msgs(marks["n"])] == [(True, False)])
    case("...and the counts follow after the pause", "1",
         lambda o: o == "1" and last_msg("changed") == {"type": "changed", "dirty": True, "words": 2, "chars": 12, "line": 1, "col": 13}, wait=300)
    case("five fast keystrokes add one message, with counts and the caret",
         "var e = document.getElementById('editor'); for (const ch of ' a b ') document.execCommand('insertText', false, ch); 1",
         lambda o: mark(), wait=300)
    case("...so five keystrokes cost one message", "1",
         lambda o: len([m for m in MESSAGES[marks["n"]:] if m["type"] in ("changed", "cursor")]) == 1
         and last_msg("changed") == {"type": "changed", "dirty": True, "words": 4, "chars": 17, "line": 1, "col": 18})
    case("undoing back to the saved text reports clean",
         "var e = document.getElementById('editor'); for (let i = 0; i < 10 && e.value !== 'hello world'; i++) Notemark.undo(); e.value",
         lambda o: o == "hello world", wait=300)
    case("...the last report says clean", "1", lambda o: last_msg("changed")["dirty"] is False and last_msg("changed")["words"] == 2)

    case("a mode switch flushes the pending counts at once",
         "Notemark.setMode('source'); Notemark.load('a b', {saved: 'a b'}); var e = document.getElementById('editor'); e.focus(); e.setSelectionRange(3, 3);"
         " document.execCommand('insertText', false, ' c d'); Notemark.setMode('page'); 1",
         lambda o: o == "1" and last_msg("changed")["words"] == 4 and last_msg("changed")["dirty"] is True)
    case("saving flushes them before the text goes out",
         "Notemark.setMode('source'); Notemark.load('a', {saved: 'a'}); var e = document.getElementById('editor'); e.focus(); e.setSelectionRange(1, 1);"
         " document.execCommand('insertText', false, ' b c'); Notemark.sendContent(); 1",
         lambda o: o == "1" and [m["type"] for m in MESSAGES[-2:]] == ["changed", "content"] and MESSAGES[-2]["words"] == 3)
    case("marking the document clean reports it clean at once",
         "Notemark.markClean(); 1", lambda o: o == "1" and last_msg("changed")["dirty"] is False and last_msg("changed")["words"] == 3)
    case("losing focus flushes the pending counts",
         "var e = document.getElementById('editor'); e.focus(); document.execCommand('insertText', false, ' d e'); e.blur(); 1",
         lambda o: o == "1" and last_msg("changed")["words"] == 5)

    case("counts match the old algorithm on a corpus (source load)", "Notemark.setMode('source'); %s.forEach(t => Notemark.load(t)); 1" % json.dumps(COUNT_CORPUS),
         lambda o: o == "1" and mark())
    case("...every text, whitespace kinds and unicode included", "1",
         lambda o: [m["words"] for m in host_msgs(marks["n"] - len(COUNT_CORPUS))[-len(COUNT_CORPUS):]]
         == [old_words(t.replace("\r\n", "\n")) for t in COUNT_CORPUS])
    case("...and the characters", "1",
         lambda o: [m["chars"] for m in host_msgs(marks["n"] - len(COUNT_CORPUS))[-len(COUNT_CORPUS):]]
         == [len(t.replace("\r\n", "\n").encode("utf-16-le")) // 2 for t in COUNT_CORPUS])
    case("page mode counts the same way after the sync",
         "Notemark.setMode('page'); Notemark.load('# Title\\n\\nSome *prose* here\\n'); T.page.focus(); T.caretEnd(T.last()); T.type(' and more'); 1",
         lambda o: True, wait=400)
    case("...page counts", "1", lambda o: last_msg("changed")["words"] == old_words("# Title\n\nSome *prose* here and more\n"))

    CARET_TEXT = "ab\ncd\r\nef\n\nlast"      # the textarea turns the CRLF into a line feed
    CARET_POS = [2, 3, 5, 6, 9, 10, 14, 0]
    case("caret: setup", "Notemark.setMode('source'); Notemark.load(%s); 'ok'" % json.dumps(CARET_TEXT), lambda o: o == "ok" and mark())
    case("caret: moves are reported one by one",
         "var e = document.getElementById('editor'); e.focus(); var pos = %s, i = 0;"
         " var t = setInterval(() => { if (i >= pos.length) return clearInterval(t); e.setSelectionRange(pos[i], pos[i]); e.dispatchEvent(new Event('click')); i++; }, 90); 1"
         % json.dumps(CARET_POS), lambda o: True, wait=1100)
    case("...start, middle, line start, after newline, blank line, end", "1",
         lambda o: [(m["line"], m["col"]) for m in MESSAGES[marks["n"]:] if "line" in m][-8:]
         == [(1, 3), (2, 1), (2, 3), (3, 1), (4, 1), (5, 1), (5, 5), (1, 1)])

    # Random edits and moves on a long text: the remembered line number must equal a plain count every time.
    rnd = random.Random(7)
    steps = [(rnd.choice(["move", "move", "type", "enter", "back", "undo"]), rnd.random()) for _ in range(16)]
    case("caret: edits on a long document keep the line number exact",
         "Notemark.setMode('source'); Notemark.load('word\\n'.repeat(2000) + 'tail'); var e = document.getElementById('editor'); e.focus();"
         " window.expect = []; var steps = %s, i = 0;"
         " var t = setInterval(() => { if (i >= steps.length) return clearInterval(t); var s = steps[i++], at = Math.floor(s[1] * e.value.length);"
         " if (s[0] !== 'undo') e.setSelectionRange(at, at);"
         " if (s[0] === 'type') document.execCommand('insertText', false, 'x');"
         " else if (s[0] === 'enter') document.execCommand('insertText', false, '\\n');"
         " else if (s[0] === 'back') document.execCommand('delete');"
         " else if (s[0] === 'undo') Notemark.undo();"
         " else e.dispatchEvent(new Event('click'));"
         " var p = e.selectionStart, b = e.value.slice(0, p); window.expect.push(b.split('\\n').length + ':' + (p - b.lastIndexOf('\\n'))); }, 220); 1"
         % json.dumps(steps), lambda o: o == "1" and mark(), wait=4500)
    case("...every reported position is the brute-force one", "JSON.stringify(window.expect)",
         lambda o: (lambda exp, got: got == [x for i, x in enumerate(exp) if i == 0 or x != exp[i - 1]][-len(got):] and len(got) >= 8)(
             json.loads(o), ["%s:%s" % (m["line"], m["col"]) for m in MESSAGES[marks["n"]:] if "line" in m][1:]))

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

    # Play the host: record what the page posts, and set the clipboard the way the app does.
    def on_message(ucm, result):
        msg = json.loads(result.get_js_value().to_string())
        MESSAGES.append(msg)
        if msg.get("type") == "copy":
            nm.NotemarkWindow.copy_text(SimpleNamespace(notify=lambda *a, **k: None), msg["text"], msg.get("note") or "Copied")

    # Record every script error and console.error from the first line of the page on: the cases read window.__errors.
    manager.add_script(WebKit2.UserScript.new(
        "window.__errors = [];"
        "window.addEventListener('error', function (e) { window.__errors.push(String(e.message)); });"
        "window.addEventListener('unhandledrejection', function (e) { window.__errors.push(String(e.reason)); });"
        "var __ce = console.error; console.error = function () {"
        " window.__errors.push([].join.call(arguments, ' ')); return __ce.apply(console, arguments); };",
        WebKit2.UserContentInjectedFrames.ALL_FRAMES, WebKit2.UserScriptInjectionTime.START, None, None))
    manager.connect("script-message-received::app", on_message)
    manager.register_script_message_handler("app")
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
        name, js, check, wait = pending.pop(0)
        # Run the next case after the usual pause, or after the case's own wait if it needs longer.
        delay = max(STEP_DELAY_MS, wait)

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
            GLib.timeout_add(delay, run_next)

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
