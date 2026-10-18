#!/usr/bin/env node
/* Equivalence test for the chunked Markdown parse (ui/mdchunk.js).
 *
 * For every input, NotemarkChunk.parse(src, marked.parse) must give the same HTML as marked.parse(src),
 * with the chunk path forced (minimum length 0, tiny chunk targets) so small inputs are cut as well.
 * Inputs: hand-written tricky cases, the repo's Markdown files, a synthetic mixed document and, when
 * you point it at them, the CommonMark / GFM spec examples and extra files:
 *
 *     NOTEMARK_SPEC="/tmp/x/spec.txt:/tmp/y/gfm-spec.txt" NOTEMARK_MD="/path/a.md:/path/b.md" node tests/test_chunk.js
 *
 * The spec files are not vendored, so without NOTEMARK_SPEC that part is skipped (not failed).
 * Needs no network and no display. The speed claim (JavaScriptCore only) is measured in the app, not here.
 */
"use strict";
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const marked = require(path.join(ROOT, "ui/vendor/marked.umd.js")).marked;
const Chunk = require(path.join(ROOT, "ui/mdchunk.js"));
marked.setOptions({ gfm: true, breaks: false });   // as ui/app.js

const whole = src => marked.parse(src);
// Whitespace between two tags is not content; compare with it squeezed out as a second chance.
const squeeze = html => html.replace(/>\s+</g, "><");
const TARGETS = [0, 150, 1200, Chunk.TARGET];

const stats = { checked: 0, chunked: 0, exact: 0, squeezed: 0, failed: 0 };
const failures = [];

function check(name, src) {
  const expected = whole(src);
  const normal = src.replace(/\r\n?/g, "\n");
  for (const target of TARGETS) {
    stats.checked++;
    const parts = Chunk.split(normal, target);
    if (parts && parts.length > 1) stats.chunked++;
    const got = Chunk.parse(src, whole, { min: 0, target });
    if (got === expected) stats.exact++;
    else if (squeeze(got) === squeeze(expected)) stats.squeezed++;
    else {
      stats.failed++;
      if (failures.length < 3) failures.push({ name, target, src, expected, got });
    }
  }
}

// ---------- Hand-written cases ----------
const TRICKY = {
  "paragraphs": "one\n\ntwo\n\nthree\n",
  "fence with blank lines": "intro\n\n```js\na\n\n\nb\n```\n\nafter\n",
  "tilde fence, longer opener": "x\n\n~~~~\na\n\n~~~\nstill code\n\n~~~~\n\ny\n",
  "indented fence": "text\n\n  ```\n  a\n\n  b\n  ```\n\nend\n",
  "unclosed fence": "x\n\n```\na\n\nb\n\nc\n",
  "fence in list item": "- item\n\n  ```\n  a\n\n  b\n  ```\n\n- next\n\npara\n",
  "html comment with blank lines": "a\n\n<!--\nhidden\n\nmore\n-->\n\nb\n",
  "pre block with blank lines": "a\n\n<pre>\nx\n\ny\n</pre>\n\nb\n",
  "script and style": "a\n\n<script>\nlet a;\n\nlet b;\n</script>\n\n<style>\np {}\n\nq {}\n</style>\n\nb\n",
  "processing instruction": "a\n\n<?php\n\necho 1;\n?>\n\nb\n",
  "cdata and declaration": "a\n\n<![CDATA[\nx\n\ny\n]]>\n\n<!DOCTYPE html\n\n>\n\nb\n",
  "html block then fence-looking line": "a\n\n<div>\n```\n\nnot code\n```\n</div>\n\nz\n",
  "indented code": "a\n\n    code\n\n    more\n\nb\n",
  "tab indented code": "a\n\n\tcode\n\n\tmore\n\nb\n",
  "tight list": "- a\n- b\n\npara\n\n- c\n- d\n",
  "loose list": "- a\n\n- b\n\n- c\n\npara\n",
  "list with lazy line": "- a\nlazy\n\n- b\n\npara\n",
  "ordered list continues": "1. a\n\n2. b\n\n3. c\n",
  "list item with paragraphs": "- a\n\n  second\n\n  third\n\n- b\n",
  "nested list": "- a\n  - b\n\n  - c\n\n- d\n",
  "list after list ended by hr": "- a\n\n---\n\n- b\n",
  "table": "| a | b |\n|---|---|\n| 1 | 2 |\n\n| c |\n|---|\n| 3 |\n",
  "blockquote": "> a\n>\n> b\n\n> c\n\nd\n",
  "blockquote lazy": "> a\nlazy\n\nb\n",
  "setext and atx": "Title\n=====\n\nSub\n---\n\n# H\n\ntext\n",
  "thematic breaks": "a\n\n***\n\nb\n\n* * *\n\nc\n",
  "task list": "- [ ] a\n- [x] b\n\n- [ ] c\n",
  "reference defined later": "see [a] and [b][ref]\n\ntext\n\n[a]: /one\n[ref]: /two \"Two\"\n",
  "reference defined first": "[a]: /one\n\ntext\n\nsee [a]\n",
  "reference first definition wins": "[a]: /one\n\nsee [a]\n\n[a]: /two\n\nagain [a]\n",
  "reference case and spaces": "[Foo  Bar]: /u\n\nx\n\nuse [foo bar] and [FOO BAR]\n",
  "definition with title on next line": "[a]: /u\n  \"title\"\n\ntext\n\nuse [a]\n",
  "definition after paragraph line": "para\n[a]: /u\n\nuse [a]\n",
  "definition in blockquote": "> [a]: /u\n\ntext\n\nuse [a]\n",
  "definition in a list": "- [a]: /u\n\ntext\n\nuse [a]\n",
  "definition in a fence is not one": "```\n[a]: /u\n```\n\ntext\n\nuse [a]\n",
  "footnote-like": "text[^1]\n\n[^1]: note\n\nmore\n",
  "crlf": "one\r\n\r\n```\r\na\r\n\r\nb\r\n```\r\n\r\ntwo\r\n",
  "whitespace-only blank lines": "one\n   \n\t\ntwo\n  \n```\na\n \nb\n```\n",
  "no trailing newline": "one\n\ntwo",
  "leading blank lines": "\n\n\none\n\ntwo\n",
  "html tag line then text": "<custom-tag>\n\nplain\n\n</custom-tag>\n\nend\n",
  "inline html paragraph": "<b>bold</b> text\n```\ncode\n\nmore\n```\n\nz\n",
  "fence interrupts paragraph": "para\n```\ncode\n\nmore\n```\n\nz\n",
  "backtick fence with backtick info is not a fence": "``` a`b\n\ntext\n\n```\n",
  "heading then definition": "# H\n[a]: /u\n\nuse [a]\n",
  "emphasis across paragraphs": "*a\n\nb*\n\n_c_\n",
  "link text across lines": "[a\nb](/u)\n\n[c](/v)\n",
  "numbered paragraph start": "2. a\n\ntext\n\n10) b\n",
  "html in list": "- <div>\n\n  x\n\n  </div>\n\n- y\n",
  "empty": "",
  "only blank": "\n\n\n",
};
for (const [name, src] of Object.entries(TRICKY)) check("tricky: " + name, src);

// ---------- Repo documents and a synthetic one ----------
const mdFiles = [path.join(ROOT, "sample.md"), path.join(ROOT, "README.md"), path.join(ROOT, "CHANGELOG.md")];
const docs = path.join(ROOT, "docs");
if (fs.existsSync(docs)) for (const f of fs.readdirSync(docs)) if (/\.md$/i.test(f)) mdFiles.push(path.join(docs, f));
for (const f of (process.env.NOTEMARK_MD || "").split(":").filter(Boolean)) mdFiles.push(f);
let sawFile = 0;
for (const f of mdFiles) {
  if (!fs.existsSync(f)) continue;
  sawFile++;
  const src = fs.readFileSync(f, "utf8");
  check("file: " + path.basename(f), src);
  // A larger input from the same file, so the default threshold takes the chunk path too.
  if (src.length < 200000) check("file x" + Math.ceil(200000 / src.length) + ": " + path.basename(f), src.repeat(Math.ceil(200000 / src.length)));
}

function synthetic(bytes) {
  const out = [];
  let i = 0, size = 0;
  const push = s => { out.push(s); size += s.length; };
  while (size < bytes) {
    i++;
    push("## Section " + i + "\n\n");
    push("Paragraph with *emphasis*, **strong**, `code`, a [link](https://example.com/" + i + ") and a ref [r" + (i % 7) + "]. ".repeat(6) + "\n\n");
    push(Array.from({ length: 6 }, (_, k) => "- item " + i + "." + k + " with `code` and text\n" + (k % 3 === 0 ? "  - nested " + k + "\n" : "")).join("") + "\n");
    push("| a | b | c |\n|---|:-:|--:|\n" + Array.from({ length: 4 }, (_, k) => "| " + k + " | x" + k + " | " + i + " |\n").join("") + "\n");
    push("```js\nfunction f" + i + "() {\n\n  return " + i + ";\n}\n```\n\n");
    push("> quote " + i + "\n>\n> more\n\n");
    push("1. one\n2. two\n\n3. three\n\n");
    push("<!-- c" + i + "\n\nhidden -->\n\n");
  }
  for (let k = 0; k < 7; k++) push("\n[r" + k + "]: https://example.com/ref/" + k + " \"R" + k + "\"\n");
  return out.join("");
}
for (const bytes of [3000, 20000, 150000]) {
  const src = synthetic(bytes);
  check("synthetic " + bytes, src);
  if (Chunk.split(src, Chunk.TARGET) === null && bytes > Chunk.MIN_LENGTH) { stats.failed++; failures.push({ name: "synthetic " + bytes + " did not split", src: "", expected: "", got: "" }); }
}

// ---------- Random line soup ----------
// Lines drawn from block openers, closers, indents and references, glued at random: the nastiest mixes.
function rng(seed) { return () => (seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296; }
const LINES = [
  "", "", "", "", "text", "more text", "# heading", "Setext", "---", "===", "***", "* * *", "- item", "* item", "+ item", "1. one", "2) two",
  "  - nested", "   1. nested", "    indented", "\tcode", "  continued", "> quote", ">", "> > deep", "```", "```js", "~~~", "~~~~", "  ```", "     ```", "`````",
  "| a | b |", "|---|---|", "| 1 | 2 |", "<div>", "</div>", "<pre>", "</pre>", "<script>", "</script>", "<!--", "-->", "<!-- one line -->", "<?x", "?>",
  "<![CDATA[", "]]>", "<!DOCTYPE x>", "<span>", "<custom/>", "[a]: /u", "[b]: /v \"t\"", "  [c]: <w>", "[a]", "see [b] and [c][a]", "[x\ny]: /z", "[ ] task", "- [x] done", "- ```", "- <div>", "1. <pre>", "  <p>x", "<b>t</b>", "</pre> x", "<TEXTAREA>", "</textarea>", "~~~js",
];
// Documents with HTML, fences or definitions in odd places are often parsed whole (by design), so
// the same soup is also drawn without them to exercise the cutting itself.
const PROFILES = {
  all: LINES,
  "no html": LINES.filter(l => !/^\s*(?:[-*+]\s+|\d+\.\s+)?</.test(l) && !/^(\?>|\]\]>|-->)$/.test(l) && l !== "- ```"),
  "no fences or refs": LINES.filter(l => !/^\s*(?:[-*+]\s+|\d+\.\s+)?</.test(l) && !/\]:|```|~~~|^\[x/.test(l)),
};
for (const [profile, pool] of Object.entries(PROFILES)) {
  const soupRand = rng(11);
  for (let n = 0; n < 700; n++) {
    const count = 3 + Math.floor(soupRand() * 40);
    const lines = [];
    for (let i = 0; i < count; i++) lines.push(pool[Math.floor(soupRand() * pool.length)]);
    check("soup (" + profile + ") " + n, lines.join("\n") + (soupRand() < 0.5 ? "\n" : ""));
  }
}

// ---------- Spec examples ----------
function specExamples(text) {
  const out = [];
  const re = /^`{32} example[^\n]*\n([\s\S]*?)^\.\n([\s\S]*?)^`{32}$/gm;
  let m;
  while ((m = re.exec(text))) out.push(m[1].replace(/→/g, "\t"));
  return out;
}
const specFiles = (process.env.NOTEMARK_SPEC || "").split(":").filter(f => f && fs.existsSync(f));
if (!specFiles.length) console.log("  skip spec examples: set NOTEMARK_SPEC=/path/spec.txt[:/path/gfm-spec.txt] (CommonMark / GFM spec.txt)");
let specTotal = 0;
for (const f of specFiles) {
  const examples = specExamples(fs.readFileSync(f, "utf8"));
  specTotal += examples.length;
  const before = stats.chunked;
  examples.forEach((src, i) => check(path.basename(f) + " example " + (i + 1), src));
  // Examples glued together in random order give blocks that meet in ways the single examples never do.
  const rand = rng(7);
  for (let round = 0; round < 40; round++) {
    const pick = examples.filter(() => rand() < 0.12);
    check(path.basename(f) + " mix " + round, pick.join("\n\n"));
    check(path.basename(f) + " mix-tight " + round, pick.join("\n"));
  }
  console.log("  " + path.basename(f) + ": " + examples.length + " examples, " + (stats.chunked - before) + " chunked runs");
}

console.log("\n" + stats.checked + " comparisons (" + stats.chunked + " cut into 2+ chunks): " +
  stats.exact + " identical, " + stats.squeezed + " equal ignoring whitespace between tags, " + stats.failed + " different" +
  (specTotal ? "; " + specTotal + " spec examples" : ""));
for (const f of failures) {
  console.log("\nFAIL " + f.name + " (target " + f.target + ")\n--- source\n" + JSON.stringify(f.src.slice(0, 400)) +
    "\n--- whole\n" + JSON.stringify(f.expected.slice(0, 400)) + "\n--- chunked\n" + JSON.stringify(f.got.slice(0, 400)));
}
if (!sawFile) console.log("  note: no repo Markdown files found");
process.exit(stats.failed ? 1 : 0);
