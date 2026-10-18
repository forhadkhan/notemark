#!/usr/bin/env python3
"""Tests for the incremental page -> Markdown sync (dirty blocks only).

An untouched block of the page goes back to the editor as the exact Markdown it was rendered from; only
blocks that were edited pass through Turndown. These tests load documents in the real WebView, then:

  - check that a page nobody touched gives back the source byte for byte (and that documents the mapping cannot
    describe, such as ones with HTML blocks, fall back to the whole-page conversion),
  - run seeded random edit sequences (typing, Enter, Backspace merges, deleting and pasting blocks, checkbox
    toggles, heading changes, list indents, undo and redo) and compare every step with an oracle that only knows
    the lexer's block texts and the HTML each block had when rendered (tests/sync_probe.js),
  - edit, sync, undo back to the original page and check the text is the original again.

Set NOTEMARK_SYNC_SEQS to run more sequences (default 40 per document), NOTEMARK_REAL_MD to add a document.

    xvfb-run -a python3 tests/test_sync.py
"""
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("GDK_BACKEND", "x11")

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import GLib, Gtk, WebKit2  # noqa: E402

HERE = Path(__file__).resolve().parent.parent
UI_INDEX = HERE / "ui" / "index.html"
PROBE = (HERE / "tests" / "sync_probe.js").read_text()
SEQS = int(os.environ.get("NOTEMARK_SYNC_SEQS", "40"))
TIMEOUT_MS = 600000


def synthetic(blocks=60, defs=True):
    """A document of many kinds of block, in Markdown that is not what Turndown would write."""
    out = ["---", "title: Sync fixture", "---", "", "Setext heading", "==============", ""]
    for i in range(blocks):
        n = i % 12
        if n == 0:
            out += ["## Section %d" % i, ""]
        elif n == 1:
            out += ["Some *emphasis*, _other emphasis_, __strong__ and `code` with a [link](https://example.com/%d) and a [ref][r%d]." % (i, i % 3), ""]
        elif n == 2:
            out += ["* star bullet one", "* star bullet two", "    * nested three", "* four", ""]
        elif n == 3:
            out += ["1) first", "2) second", "3) third", ""]
        elif n == 4:
            out += ["~~~python", "def f(x):", "    return x  # %d" % i, "~~~", ""]
        elif n == 5:
            out += ["| a | b |", "|--|:-:|", "| 1 | long cell %d |" % i, "| 3 | 4 |", ""]
        elif n == 6:
            out += ["> quoted *text* %d" % i, "> second line", ""]
        elif n == 7:
            out += ["***", ""]
        elif n == 8:
            out += ["- [ ] open task %d" % i, "- [x] done task", ""]
        elif n == 9:
            out += ["Line with a hard break  ", "and the next line", ""]
        elif n == 10:
            out += ["Heading Two", "-----------", ""]
        else:
            out += ["Plain paragraph %d with  odd   spacing and trailing words that go on a while." % i, ""]
    if defs:
        out += ["[r0]: https://example.com/zero", "[r1]: https://example.com/one 'One'", "[r2]: <https://example.com/two>", ""]
    return "\n".join(out)


def fixtures():
    docs = {
        "sample.md": ((HERE / "sample.md").read_text(), True),
        "synthetic 60 blocks": (synthetic(60), True),
        "synthetic 600 blocks, chunked": (synthetic(600, defs=False), True),
        "synthetic 600 blocks with definitions, chunked": (synthetic(600), False),
        "ref links and defs between blocks": ("Intro [a][a].\n\n[a]: https://example.com/a\n\n## Two\n\n[b]: https://b.example\n\nText [b].\n", True),
        "defs at the end": ("One\n\nTwo [x]\n\n[x]: https://x.example\n[y]: https://y.example\n", True),
        "def before any block": ("[x]: https://x.example\n\nText [x].\n", False),
        "no trailing newline": ("# Title\n\nlast para", True),
        "leading blank lines": ("\n\n# Title\n\npara\n", True),
        "extra blank lines": ("# Title\n\n\n\npara\n\n\n- a\n- b\n\n\n", True),
        "whitespace-only lines": ("# Title\n   \n\npara\n \t \n\n> q\n", True),
        "ends in a table": ("intro\n\n| a | b |\n|---|---|\n| 1 | 2 |", True),
        "ends in a fence": ("intro\n\n```js\nlet a = 1;\n```\n", True),
        "task and nested lists": ("- [ ] a\n  - [x] b\n    - c\n- d\n\n1. one\n   1. sub\n2. two\n", True),
        "indented code and paragraphs after lists": ("Intro paragraph\n\n- item a\n- item b\n\nNote:\n\n    code line <b> * x\n    more code\n\nmid paragraph\n\n  indented paragraph\n\n1. one\n2. two\n\nbetween\n\n   three space para\n\n    $ make <target>\n\nlast\n", True),
        "indented code after paragraphs": ("Steps:\n\n    $ make <target>\n\nThen:\n\n    $ make install\n\n    # not a heading\n\nDone\n", True),
        "lists of one kind apart": ("- a\n\nmid\n\n- b\n\nmid two\n\n1. x\n\nmid three\n\n2. y\n", True),
        "html block": ("# T\n\n<div class=\"x\">raw</div>\n\ntext\n", False),
        "html comment": ("# T\n\n<!-- note -->\n\ntext\n", False),
        "crlf (the editor takes line ends as \\n)": ("# T\r\n\r\ntext\r\n", True),
        "unclosed fence": ("# T\n\n```js\nlet a;\n\nmore\n", False),
        "single paragraph": ("just one line", True),
        "empty": ("", False),
        "only hr": ("---\n", True),
        "repo README": ((HERE / "README.md").read_text(), None),
        "repo CHANGELOG": ((HERE / "CHANGELOG.md").read_text(), None),
    }
    real = os.environ.get("NOTEMARK_REAL_MD")
    if real and Path(real).is_file():
        docs["real document"] = (Path(real).read_text(), True)
    return docs


def js(doc):
    return json.dumps(doc)


def build_cases():
    cases = []

    def add(name, code, check, wait=0):
        cases.append((name, code, check, wait))

    add("probe loads", PROBE + "; typeof window.S", lambda out: out == "object")

    docs = fixtures()
    for name, (md, mapped) in docs.items():
        code = """(function(){
          const md = %s.replace(/\\r\\n?/g, '\\n'), mapped = S.start(md);
          const r = { mapped };
          if (mapped) {
            r.inc = Notemark.syncProbe.incremental() === md;
            document.getElementById('page').dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'historyUndo'}));
            r.real = Notemark.toMarkdown() === md;
          } else {
            document.getElementById('page').dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'historyUndo'}));
            r.real = Notemark.toMarkdown() === Notemark.syncProbe.full();
            r.inc = Notemark.syncProbe.incremental() === null;
          }
          return JSON.stringify(r);
        })()""" % js(md)

        def check(out, mapped=mapped):
            r = json.loads(out)
            return r["inc"] and r["real"] and (mapped is None or r["mapped"] == mapped)

        add("untouched page gives back the source: " + name, code, check)

    # Each scenario returns JSON with the final text as `out` and the oracle's complaints as `errs`.
    def scenario(name, body, check):
        add("scenario: " + name, "(function(){" + body + "})()", lambda out: json.loads(out)["errs"] == [] and check(json.loads(out)))

    fire = "p.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'historyUndo'}));"
    scenario("one edit leaves every other block verbatim", """
        const md = %s; S.start(md);
        const p = document.getElementById('page'); p.focus();
        const el = Array.from(p.children).find(e => e.textContent.includes('Plain paragraph 11'));
        const r = document.createRange(); r.setStart(el.firstChild, 3); r.collapse(true);
        getSelection().removeAllRanges(); getSelection().addRange(r);
        document.execCommand('insertText', false, 'Z'); %s
        const out = Notemark.toMarkdown();
        return JSON.stringify({ out, errs: S.check('x'),
          kept: out.includes('~~~python') && out.includes('* star bullet one') && out.includes('[r2]: <https://example.com/two>') && out.includes('Setext heading\\n====='),
          edited: out.includes('PlaZin paragraph 11') });
        """ % (js(synthetic(30)), fire), lambda r: r["kept"] and r["edited"])
    scenario("delete a block, keep the rest", """
        const md = '# A\\n\\n* one\\n* two\\n\\ntext   spaced\\n\\n~~~\\nfence\\n~~~\\n'; S.start(md);
        const p = document.getElementById('page'); p.children[1].remove(); %s
        return JSON.stringify({ out: Notemark.toMarkdown(), errs: S.check('x') });
        """ % fire, lambda r: r["out"] == "# A\n\ntext   spaced\n\n~~~\nfence\n~~~\n")
    scenario("split with Enter", """
        const md = '# A\\n\\nfirst second\\n\\n* keep\\n'; S.start(md);
        const p = document.getElementById('page'); p.focus();
        const r = document.createRange(); r.setStart(p.children[1].firstChild, 5); r.collapse(true);
        getSelection().removeAllRanges(); getSelection().addRange(r); document.execCommand('insertParagraph'); %s
        return JSON.stringify({ out: Notemark.toMarkdown(), errs: S.check('x') });
        """ % fire, lambda r: r["out"].startswith("# A\n\nfirst\n\n") and r["out"].endswith("\n\n* keep\n"))
    scenario("checkbox toggle", """
        const md = '# A\\n\\n- [ ] one\\n* two\\n\\nlast   one\\n'; S.start(md);
        document.querySelector('#page input[type=checkbox]').click();
        return JSON.stringify({ out: Notemark.toMarkdown(), errs: S.check('x') });
        """, lambda r: "- [x] one" in r["out"] and r["out"].endswith("\n\nlast   one\n"))
    scenario("trailing empty paragraph is not an edit", """
        const md = 'a\\n\\n| x | y |\\n|--|--|\\n| 1 | 2 |'; S.start(md);
        return JSON.stringify({ same: Notemark.syncProbe.incremental() === md, errs: S.check('x') });
        """, lambda r: r["same"])
    scenario("typing in the trailing paragraph", """
        const md = 'a\\n\\n```\\ncode\\n```\\n'; S.start(md);
        const p = document.getElementById('page'); p.focus();
        const r = document.createRange(); r.selectNodeContents(p.lastElementChild); r.collapse(true);
        getSelection().removeAllRanges(); getSelection().addRange(r); document.execCommand('insertText', false, 'after'); %s
        return JSON.stringify({ out: Notemark.toMarkdown(), errs: S.check('x') });
        """ % fire, lambda r: r["out"] == "a\n\n```\ncode\n```\n\nafter\n")
    scenario("removing the block that holds a link definition falls back", """
        const md = 'Intro [a].\\n\\n[a]: https://example.com/a\\n\\nTail\\n'; S.start(md);
        const p = document.getElementById('page'); const was = Notemark.syncProbe.mapped();
        p.children[0].remove(); %s
        return JSON.stringify({ was, inc: Notemark.syncProbe.incremental(), errs: S.check('x') });
        """ % fire, lambda r: r["was"] and r["inc"] is None)

    # Top-level block types of the saved text as a reader of the file would see them (what marked makes of it).
    kinds = "const kinds = t => marked.lexer(t).filter(x => x.type !== 'space').map(x => x.type).join();"
    scenario("deleting the paragraph between a list and an indented code block keeps the code a code block", kinds + """
        const md = '- a\\n\\nNote:\\n\\n    x = <b> * y *\\n'; S.start(md);
        const p = document.getElementById('page'); p.children[1].remove(); %s
        const out = Notemark.toMarkdown();
        return JSON.stringify({ out, kinds: kinds(out), errs: S.check('x') });
        """ % fire, lambda r: r["kinds"] == "list,code" and "<b>" in r["out"])
    scenario("a list made out of the paragraph above an indented code block does not swallow it", kinds + """
        const md = 'Steps:\\n\\n    $ make <target>\\n'; S.start(md);
        const p = document.getElementById('page'); p.focus();
        const r = document.createRange(); r.setStart(p.children[0].firstChild, 0); r.collapse(true);
        getSelection().removeAllRanges(); getSelection().addRange(r);
        document.execCommand('insertUnorderedList'); %s
        const out = Notemark.toMarkdown();
        return JSON.stringify({ out, kinds: kinds(out), errs: S.check('x') });
        """ % fire, lambda r: r["out"].startswith("- Steps:") and r["kinds"] == "list,code")
    scenario("an indented paragraph keeps clear of the list that came to stand above it", kinds + """
        const md = '- a\\n\\nNote:\\n\\n  indented paragraph\\n'; S.start(md);
        const p = document.getElementById('page'); p.children[1].remove(); %s
        const out = Notemark.toMarkdown();
        return JSON.stringify({ out, kinds: kinds(out), errs: S.check('x') });
        """ % fire, lambda r: r["kinds"] == "list,paragraph")
    scenario("an indented block that still has its old neighbour stays verbatim", kinds + """
        const md = '- a\\n\\nNote:\\n\\n    x = 1\\n\\nlast\\n'; S.start(md);
        const p = document.getElementById('page'); p.children[3].remove(); %s
        return JSON.stringify({ out: Notemark.toMarkdown(), errs: S.check('x') });
        """ % fire, lambda r: r["out"] == "- a\n\nNote:\n\n    x = 1\n")

    fuzz_docs = [("sample.md", docs["sample.md"][0]), ("synthetic 60 blocks", docs["synthetic 60 blocks"][0]),
                 ("ref links and defs", docs["ref links and defs between blocks"][0]), ("task and nested lists", docs["task and nested lists"][0]),
                 ("ends in a table", docs["ends in a table"][0]), ("single paragraph", docs["single paragraph"][0]),
                 ("chunked 600 blocks", docs["synthetic 600 blocks, chunked"][0]),
                 ("indented code and paragraphs after lists", docs["indented code and paragraphs after lists"][0]),
                 ("indented code after paragraphs", docs["indented code after paragraphs"][0]),
                 ("lists of one kind apart", docs["lists of one kind apart"][0])]
    if "real document" in docs:
        fuzz_docs.append(("real document", docs["real document"][0]))
    for i, (name, md) in enumerate(fuzz_docs):
        n = SEQS if name not in ("real document", "chunked 600 blocks") else max(10, SEQS // 4)
        code = "JSON.stringify(S.fuzz(%s, %d, %d, 14))" % (js(md), 1000 + i, n)
        add("fuzz %d sequences: %s" % (n, name), code, lambda out: json.loads(out)["errs"] == [] and json.loads(out).get("ops", 1) > 0)
        code = "JSON.stringify(S.undoRoundTrip(%s, %d, %d))" % (js(md), 2000 + i, max(10, n))
        add("undo round trip: " + name, code, lambda out: json.loads(out)["errs"] == [] and json.loads(out).get("restored", 1) >= 0)
    return cases


def main():
    cases = build_cases()
    results = []
    web = WebKit2.WebView()
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
        name, code, check, wait = pending.pop(0)

        def done(view, result):
            try:
                value = view.evaluate_javascript_finish(result)
                out = value.to_string() if value else None
            except GLib.Error as exc:
                out = "javascript error: %s" % exc.message
            try:
                ok = bool(check(out))
            except Exception as exc:
                ok = False
                out = "%s (check raised %s)" % (out, exc)
            results.append((name, ok, out))
            GLib.timeout_add(max(50, wait), run_next)

        web.evaluate_javascript(code, -1, None, None, None, done)

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
            print("  ok   %s%s" % (name, "  " + out if name.startswith(("fuzz", "undo")) else ""))
        else:
            failed += 1
            print("  FAIL %s\n         got: %r" % (name, str(out)[:1200]))
    missing = len(cases) - len(results)
    if missing > 0:
        failed += missing
        print("  FAIL %d case(s) did not run" % missing)
    print("\n%d passed, %d failed, %d total" % (len(results) - failed, failed, len(cases)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
