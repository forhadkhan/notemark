/* Notemark page logic.
 *
 * Modes:
 *   page   – Notion-like WYSIWYG: the rendered document is contenteditable. You type into the
 *            formatted text; Markdown shortcuts (# , - , 1. , [] , > , ```) convert as you type.
 *            The HTML is converted back to Markdown (Turndown) for saving.
 *   source – one plain Markdown textarea.
 *   split  – source on the left, WYSIWYG page on the right.
 *
 * #editor (textarea) always holds the canonical text. A document is either Markdown ("md") or
 * CSV ("csv"); for CSV the page is an editable table (see csv.js) instead of the WYSIWYG page.
 */
(function () {
  "use strict";

  // WebKitGTK reports Shift+Tab as key "Unidentified" (the key is ISO_Left_Tab); code and keyCode stay right.
  const isTab = ev => ev.key === "Tab" || ev.code === "Tab" || ev.keyCode === 9;

  const html = document.documentElement;
  const editor = document.getElementById("editor");
  const page = document.getElementById("page");
  const pagePane = document.getElementById("page-pane");
  const workspace = document.getElementById("workspace");
  const divider = document.getElementById("divider");

  let mode = "page";
  let kind = "md";
  let savedText = "";
  let renderTimer = null;
  let syncTimer = null;
  let pageDirty = false;   // page DOM has edits not yet converted into #editor
  let syncing = false;
  let syncEnabled = true;   // split mode: keep the two panes scrolled together
  let resizing = false;

  // ---------- Host bridge ----------
  function send(msg) {
    try { window.webkit.messageHandlers.app.postMessage(JSON.stringify(msg)); } catch (e) { /* not in GTK host */ }
  }

  // ---------- CSV table view ----------
  const grid = window.NotemarkCsv.create(document.getElementById("grid"), {
    // The first keystroke in a cell turns the title bar dirty at once; the text, counts and caret follow in onChange.
    onEdit() {
      rendered = null;
      if (!reportedDirty) { reportedDirty = true; send({ type: "changed", dirty: true }); }
    },
    onChange(text) { editor.value = text; rendered = null; notifyChange(); },
    copy(text, note) { send({ type: "copy", text, note }); },
  });

  // ---------- Markdown <-> HTML ----------
  marked.setOptions({ gfm: true, breaks: false });

  const turndown = new TurndownService({
    headingStyle: "atx", bulletListMarker: "-", codeBlockStyle: "fenced",
    emDelimiter: "*", strongDelimiter: "**", hr: "---", br: "  ",
  });
  turndown.use(turndownPluginGfm.gfm);
  turndown.keep(["kbd", "sub", "sup", "mark"]);
  // Our task-list markup: <li class="task"><input type=checkbox> <span class="task-text">…</span>
  turndown.addRule("taskText", { filter: n => n.nodeName === "SPAN" && n.classList.contains("task-text"), replacement: c => c.replace(/^\s+/, "") });
  // Single-space list markers ("- item", "1. item"); nested content indented to the marker width.
  turndown.addRule("listItem", {
    filter: "li",
    replacement: (content, node, options) => {
      let prefix = options.bulletListMarker + " ";
      const parent = node.parentNode;
      if (parent.nodeName === "OL") {
        const start = parent.getAttribute("start");
        const index = Array.prototype.indexOf.call(parent.children, node);
        prefix = (start ? Number(start) + index : index + 1) + ". ";
      }
      content = content.replace(/^\n+/, "").replace(/\n+$/, "\n").replace(/\n/gm, "\n" + " ".repeat(prefix.length));
      return prefix + content + (node.nextSibling && !/\n$/.test(content) ? "\n" : "");
    },
  });
  // Code blocks: <br> (inserted while editing) and text newlines both become lines.
  turndown.addRule("fencedCodeWithBreaks", {
    filter: n => n.nodeName === "PRE" && n.firstChild && n.firstChild.nodeName === "CODE",
    replacement: (content, node, options) => {
      const code = node.firstChild;
      const lang = (code.className.match(/language-(\S+)/) || [null, ""])[1];
      let text = "";
      code.childNodes.forEach(function walk(n) {
        if (n.nodeType === 3) text += n.nodeValue;
        else if (n.nodeName === "BR") text += "\n";
        else n.childNodes.forEach(walk);
      });
      text = text.replace(/\n+$/, "");
      const fence = options.fence || "```";
      return "\n\n" + fence + lang + "\n" + text + "\n" + fence + "\n\n";
    },
  });
  // WebKit editing artefacts.
  turndown.addRule("emptyBlock", {
    filter: n => (n.nodeName === "P" || n.nodeName === "DIV") && !n.textContent.trim() && !n.querySelector("img, hr, input"),
    replacement: () => "\n\n",
  });

  function sanitize(fragmentHtml) {
    const doc = new DOMParser().parseFromString(fragmentHtml, "text/html");
    doc.querySelectorAll("script, iframe, object, embed, form, style, link, meta").forEach(n => n.remove());
    doc.querySelectorAll("*").forEach(el => {
      for (const attr of Array.from(el.attributes)) {
        const name = attr.name.toLowerCase();
        const value = attr.value.trim().toLowerCase();
        if (name.startsWith("on") ||
            ((name === "href" || name === "src" || name === "xlink:href") &&
             (value.startsWith("javascript:") || value.startsWith("data:text/html")))) {
          el.removeAttribute(attr.name);
        }
      }
    });
    return doc.body;
  }

  function decorateTask(li, box) {
    li.classList.add("task");
    li.classList.toggle("done", box.checked);
    box.disabled = false;
    box.contentEditable = "false";
    if (!li.querySelector(":scope > .task-text")) {
      const text = document.createElement("span");
      text.className = "task-text";
      const holder = box.parentElement;
      while (box.nextSibling) text.appendChild(box.nextSibling);
      if (text.firstChild && text.firstChild.nodeType === 3) text.firstChild.nodeValue = text.firstChild.nodeValue.replace(/^\s+/, "");
      holder.appendChild(text);
      if (holder !== li) li.insertBefore(box, holder);
      if (!text.textContent && !text.querySelector("*")) text.appendChild(document.createElement("br"));
    }
  }

  function decorate(root) {
    root.querySelectorAll("li").forEach(li => {
      const box = li.querySelector(":scope > input[type=checkbox], :scope > p > input[type=checkbox]");
      if (box) decorateTask(li, box);
    });
    root.querySelectorAll("blockquote").forEach(bq => {
      const first = (bq.textContent || "").trimStart();
      bq.classList.toggle("callout", /^(\[!\w+\]|[\u{1F300}-\u{1FAFF}☀-➿])/u.test(first));
    });
    root.querySelectorAll("hr, img").forEach(el => { el.contentEditable = "false"; });
    root.querySelectorAll("pre > code").forEach(code => {
      const last = code.lastChild;
      if (last && last.nodeType === 3) last.nodeValue = last.nodeValue.replace(/\n$/, "");
      if (!code.firstChild) code.appendChild(document.createElement("br"));
    });
    // Make sure there is always a paragraph to type into at the end.
    const last = root.lastElementChild;
    if (!last || /^(PRE|TABLE|HR|UL|OL|BLOCKQUOTE)$/.test(last.nodeName)) root.appendChild(emptyParagraph());
  }

  function emptyParagraph() {
    const p = document.createElement("p");
    p.appendChild(document.createElement("br"));
    return p;
  }

  // marked.parse is quadratic in WebKit, so long documents are parsed in chunks (see mdchunk.js).
  // With `lists`, the block tokens of every chunk are collected there, for mapBlocks.
  function renderHtml(markdown, lists) {
    const parse = lists ? src => { const toks = marked.lexer(src); lists.push(toks); return marked.parser(toks); } : marked.parse;
    const body = sanitize(window.NotemarkChunk.parse(markdown, parse));
    decorate(body);
    return body;
  }

  // What the page last showed: the Markdown and the kind it was rendered from. Cleared by any edit in the page,
  // because the DOM is then the truth and may differ from a fresh render of the same text.
  let rendered = null;

  function renderPage() {
    const key = kind + "\n" + editor.value;
    if (rendered === key && !pageDirty) return;
    if (kind === "csv") { grid.load(editor.value); pageDirty = false; rendered = key; return; }
    const lists = [];
    const body = renderHtml(editor.value, lists);
    hideCopy();
    observer.disconnect();   // the swap below is not an edit
    page.replaceChildren(...Array.from(body.childNodes));
    if (!page.firstElementChild) page.appendChild(emptyParagraph());
    mapBlocks(editor.value, lists);
    pageDirty = false;
    rendered = key;
  }

  // ---------- Incremental page -> Markdown ----------
  // Turndown over the whole page costs about a second on a 1 MB document and rewrites every block in its own style
  // (list markers, emphasis, table spacing) as soon as anything is edited. So renderPage remembers, for each top-level
  // block element, the source text it was rendered from; a MutationObserver notes the blocks that get touched; and
  // syncFromPage runs Turndown on those alone, keeping the rest of the text byte for byte. Whatever does not line up
  // (raw HTML blocks, \r, a chunked parse of a document with link definitions, odd top-level nodes, a block that
  // carries link definitions going away) leaves `mapping` null
  // or makes incrementalMarkdown return null, and the whole page goes through Turndown as before.
  //   mapping = { records: WeakMap<Element, record>, dirty: Set<Node>, count, held }
  //   record  md    the block's text: its source plus the blank lines after it (the first block also the ones before it)
  //           html  outerHTML when md was recorded, to tell whether the block changed since
  //           seq   the block's position in the rendered source; untouched blocks with consecutive seq join as they were
  //           ed    md is Turndown's, not the source's, so a blank line must separate it from its neighbours
  //           bmd, bhtml  md and html as rendered: a block edited and then undone gets its source text back
  //           keep  link reference definitions that follow the block in the source (they must survive: see `held`)
  const BLOCK_TAGS = /^(P|H[1-6]|UL|OL|PRE|BLOCKQUOTE|TABLE|HR|DIV)$/;
  const TOKEN_TAGS = { code: "PRE", blockquote: "BLOCKQUOTE", table: "TABLE", hr: "HR" };
  const OBSERVE = { subtree: true, childList: true, characterData: true, attributes: true };
  const observer = new MutationObserver(list => noteMutations(list));
  const scratch = document.createElement("div");
  let mapping = null;

  // A fence that is never closed swallows whatever is written after it, so the block cannot be kept apart from its end.
  function closedFence(raw) {
    const lines = raw.replace(/\n+$/, "").split("\n");
    const open = /^ {0,3}(`{3,}|~{3,})/.exec(lines[0]);
    const close = /^ {0,3}(`{3,}|~{3,})[ \t]*$/.exec(lines[lines.length - 1]);
    return lines.length > 1 && !!open && !!close && close[1][0] === open[1][0] && close[1].length >= open[1].length;
  }

  const tokenTag = t => t.type === "heading" ? "H" + t.depth : t.type === "paragraph" ? "P" : t.type === "list" ? (t.ordered ? "OL" : "UL") : TOKEN_TAGS[t.type];

  /** Pair the top-level elements just rendered from `markdown` with the tokens (`lists`, per chunk) they came from. */
  function mapBlocks(markdown, lists) {
    mapping = null;
    try {
      if (markdown.includes("\r")) return;
      const starts = [], tags = [], keeps = [];
      let pos = 0, defs = 0;
      for (const toks of lists) {
        for (const t of toks) {
          // The raw texts must tile the source exactly: that is what lets untouched blocks be copied back verbatim.
          // (Chunks that got copies of the document's definitions, see mdchunk.js, do not tile: marked drops the duplicates.)
          if (!markdown.startsWith(t.raw, pos)) return;
          if (t.type === "def") {
            if (!starts.length) return;
            if (keeps[keeps.length - 1] < 0) keeps[keeps.length - 1] = pos;
            defs++;
          } else if (t.type !== "space") {
            const tag = tokenTag(t);
            if (!tag || (t.type === "code" && t.codeBlockStyle !== "indented" && !closedFence(t.raw))) return;
            starts.push(pos); tags.push(tag); keeps.push(-1);
          }
          pos += t.raw.length;
        }
      }
      // A definition inside a quote or list applies to the whole document and would go with its block.
      if (pos !== markdown.length || !starts.length) return;
      if (markdown.includes("]:") && (markdown.match(/^[ \t>*+\-\d.]*\[[^\]\n]+\]:[ \t]*\S/gm) || []).length !== defs) return;
      const els = Array.from(page.children);
      const spare = els.length - starts.length;   // decorate adds an empty paragraph to type into after a final table, code block...
      if (spare < 0 || spare > 1 || (spare && !(els[els.length - 1].nodeName === "P" && !els[els.length - 1].textContent))) return;
      for (let i = 0; i < starts.length; i++) if (els[i].nodeName !== tags[i]) return;
      const records = new WeakMap();
      let held = 0;
      for (let i = 0; i < starts.length; i++) {
        const end = i + 1 < starts.length ? starts[i + 1] : markdown.length;
        const keep = keeps[i] < 0 ? "" : markdown.slice(keeps[i], end);
        if (keep) held++;
        const md = markdown.slice(i ? starts[i] : 0, end), html = els[i].outerHTML;
        records.set(els[i], { md, html, seq: i, keep, ed: false, bmd: md, bhtml: html });
      }
      if (spare) {
        const html = els[els.length - 1].outerHTML;
        records.set(els[els.length - 1], { md: "", html, seq: -1, keep: "", ed: false, bmd: "", bhtml: html });
      }
      mapping = { records, dirty: new Set(), count: starts.length, held };
      observer.observe(page, OBSERVE);
    } catch (e) {
      mapping = null;
    }
  }

  /** The top-level block holding each mutated node is touched; a block put into #page counts as new. */
  function noteMutations(list) {
    const dirty = mapping && mapping.dirty;
    if (!dirty) return;
    for (const m of list) {
      let n = m.target;
      if (n === page) {
        if (m.type === "childList") for (const a of m.addedNodes) dirty.add(a);
        continue;
      }
      while (n && n.parentNode !== page) n = n.parentNode;   // null once the node has left the page
      if (n) dirty.add(n);
    }
  }

  const tidy = s => s.replace(/ /g, " ").replace(/\n{3,}/g, "\n\n").replace(/[ \t]+$/gm, m => m.length >= 2 ? "  " : "");

  /** Turndown for one top-level block, as it would come out within the whole page. */
  function blockMarkdown(el) {
    scratch.replaceChildren(el.cloneNode(true));
    const md = tidy(turndown.turndown(scratch)).replace(/^\n+/, "");
    scratch.replaceChildren();
    return md;
  }

  /** `s` ending in a blank line. */
  function blankAfter(s) {
    let n = 0;
    while (n < 2 && s.charCodeAt(s.length - 1 - n) === 10) n++;
    return n < 2 ? s + "\n".repeat(2 - n) : s;
  }

  /** Make `parts` end in a blank line. */
  function padBlank(parts) {
    parts[parts.length - 1] = blankAfter(parts[parts.length - 1]);
  }

  /**
   * Whether `md`, a block kept as it was in the source, still starts a block of its own, of kind `tag`, after `prev`.
   * A blank line is not always enough: once the paragraph between them is gone, an indented block that followed it
   * reads as part of a list above, two lists of one kind become one, an unclosed fence swallows what follows. Whatever
   * the cause, the lexer shows it: there is no token boundary at the join, or the block starting there is another kind.
   */
  function standsAlone(prev, md, tag) {
    const head = blankAfter(prev);
    let pos = 0, joined = false;
    for (const t of marked.lexer(head + md)) {
      if (pos === head.length) joined = true;
      if (pos >= head.length && t.type !== "space") return joined && tokenTag(t) === tag;
      pos += t.raw.length;
    }
    return false;
  }

  function endWithNewline(s) {
    let e = s.length;
    while (e > 0 && s.charCodeAt(e - 1) === 10) e--;
    return s.slice(0, e) + "\n";
  }

  /** The page as Markdown from the blocks touched since the last time, or null when only a whole-page pass can be trusted. */
  function incrementalMarkdown(dry) {
    const map = mapping;
    if (!map) return null;
    noteMutations(observer.takeRecords());
    const { records, dirty } = map;
    const parts = [], recs = [], edited = [];   // what goes into the text, per non-empty block
    const changes = [];                          // blocks whose record has to be replaced: [node, rec, state, md]
    let held = 0, lastSeq = -1, prevSeq = -2, prevEdited = false;
    for (let node = page.firstChild; node; node = node.nextSibling) {
      if (node.nodeType !== 1) {
        if (node.nodeType === 3 && !/[^ \t\n\r]/.test(node.nodeValue)) continue;
        return null;   // loose text or a comment in #page: Turndown knows how to wrap those
      }
      if (!BLOCK_TAGS.test(node.nodeName)) return null;
      const rec = records.get(node);
      // state 0: as recorded; 1: edited and then undone back to how it was rendered; 2: serialise it again
      let state = 2, md, ed = true;
      if (rec) {
        const outer = dirty.has(node) ? node.outerHTML : rec.html;
        if (outer === rec.html) { state = 0; md = rec.md; ed = rec.ed; }
        else if (outer === rec.bhtml) { state = 1; md = rec.bmd; ed = false; }
        if (rec.seq >= 0) {
          if (rec.seq <= lastSeq) return null;   // blocks out of their rendered order: not worth reasoning about
          lastSeq = rec.seq;
        }
        if (rec.keep) held++;
      }
      const seq = rec ? rec.seq : -1;
      // A source block next to something it was not written beside (the block above was edited or removed) may no longer
      // parse as it did: it is serialised again unless the lexer says it still stands on its own.
      // The record stays as it is (the block may well be next to its old neighbours again after an undo).
      const again = state !== 2 && !ed && md && parts.length && (prevEdited || seq < 0 || seq !== prevSeq + 1) &&
        !standsAlone(parts[parts.length - 1], md, node.nodeName);
      if (state === 2 || again) {
        md = blockMarkdown(node);
        if (rec && rec.keep) md = md ? md + "\n\n" + rec.keep : rec.keep;
        ed = true;
      }
      if (state) changes.push(node, rec, state, md);
      if (!md) continue;
      // Untouched blocks that were neighbours in the source join as they were; anything else gets a blank line between.
      if (parts.length && (ed || prevEdited || seq < 0 || seq !== prevSeq + 1)) padBlank(parts);
      parts.push(md); recs.push(rec); edited.push(ed);
      prevEdited = ed; prevSeq = seq;
    }
    if (held !== map.held) return null;   // a block with link definitions is gone
    const n = parts.length;
    if (n) {
      if (edited[0] || recs[0].seq !== 0) parts[0] = parts[0].replace(/^\n+/, "");
      if (edited[n - 1] || recs[n - 1].seq !== map.count - 1) parts[n - 1] = endWithNewline(parts[n - 1]);
    }
    const md = parts.join("");
    if (dry) return md;
    // The records keep each block's own text: the blank lines padBlank added and the ends cut above are not part of it.
    for (let i = 0; i < changes.length; i += 4) {
      const node = changes[i], rec = changes[i + 1], state = changes[i + 2];
      records.set(node, state === 1
        ? { md: rec.bmd, html: rec.bhtml, seq: rec.seq, keep: rec.keep, ed: false, bmd: rec.bmd, bhtml: rec.bhtml }
        : { md: changes[i + 3], html: node.outerHTML, seq: rec ? rec.seq : -1, keep: rec ? rec.keep : "", ed: true, bmd: rec ? rec.bmd : "", bhtml: rec ? rec.bhtml : "" });
    }
    dirty.clear();
    return md;
  }

  function fullMarkdown() {
    let md = tidy(turndown.turndown(page)).replace(/^\n+/, "");
    if (md && !md.endsWith("\n")) md += "\n";
    if (!md.trim()) md = "";
    return md;
  }

  /** Convert the page DOM back into Markdown and store it as the canonical text. */
  function syncFromPage() {
    clearTimeout(syncTimer);
    if (!pageDirty || kind === "csv") return;
    let md = null;
    try {
      md = incrementalMarkdown();
    } catch (e) {
      mapping = null;
      observer.disconnect();
    }
    editor.value = md === null ? fullMarkdown() : md;
    pageDirty = false;
  }

  function scheduleSync() {
    pageDirty = true;
    rendered = null;
    clearTimeout(syncTimer);
    syncTimer = setTimeout(() => { syncFromPage(); notifyChange(); }, 150);
  }

  // ---------- Text helpers ----------
  // Same set as the regex \s: ASCII whitespace, no-break space, and the Unicode space separators.
  function isSpace(c) {
    return c === 32 || (c >= 9 && c <= 13) || (c >= 0xa0 && (c === 0xa0 || c === 0x1680 || (c >= 0x2000 && c <= 0x200a) ||
      c === 0x2028 || c === 0x2029 || c === 0x202f || c === 0x205f || c === 0x3000 || c === 0xfeff));
  }

  // Runs of non-whitespace characters, i.e. what text.trim().split(/\s+/).length gave, without building the pieces.
  function countWords(text) {
    let n = 0, inWord = false;
    for (let i = 0, len = text.length; i < len; i++) {
      const space = isSpace(text.charCodeAt(i));
      if (!space && !inWord) n++;
      inWord = !space;
    }
    return n;
  }

  function currentText() {
    syncFromPage();
    if (kind === "csv") grid.flush();   // the table rebuilds its text a moment after typing
    return editor.value;
  }

  // What the host was last told, so a keystroke can raise the dirty flag at once and leave the rest for later.
  let reportedDirty = false;
  let flushTimer = 0;      // trailing flush of counts, dirty and caret after typing in the source editor
  let cursorTimer = 0;     // caret moves that are not edits (arrows, clicks, focus)
  let lastCursor = "";     // "line:col" the host already has
  const REPORT_MS = 120;

  /** Send the document state: dirty flag and counts, plus the caret when asked (source typing). */
  function notifyChange(withCursor) {
    clearTimeout(flushTimer);
    flushTimer = 0;
    const text = currentText();
    reportedDirty = text !== savedText;
    const msg = { type: "changed", dirty: reportedDirty, words: countWords(text), chars: text.length };
    if (withCursor && mode !== "page") {
      const cur = caretPosition();
      if (cur.key !== lastCursor) { lastCursor = cur.key; msg.line = cur.line; msg.col = cur.col; }
    }
    send(msg);
  }

  /** Bring the host up to date now if a keystroke's report is still waiting. */
  function flushReport() {
    grid.flush();
    if (flushTimer) notifyChange(true);
  }

  // ---------- Source editor ----------
  const LIST_LINE_RE = /^(\s*)([-*+]|\d+[.)])(\s+)(\[[ xX]\]\s+)?(.*)$/;

  function insertTextTA(ta, str) {
    ta.focus();
    if (!document.execCommand("insertText", false, str)) {
      ta.setRangeText(str, ta.selectionStart, ta.selectionEnd, "end");
      ta.dispatchEvent(new Event("input", { bubbles: true }));
    }
  }

  function replaceRange(ta, start, end, str, selStart, selEnd) {
    ta.setSelectionRange(start, end);
    insertTextTA(ta, str);
    if (selStart != null) ta.setSelectionRange(selStart, selEnd == null ? selStart : selEnd);
  }

  function wrapSelection(ta, before, after) {
    const s = ta.selectionStart, e = ta.selectionEnd, sel = ta.value.slice(s, e);
    if (sel.startsWith(before) && sel.endsWith(after) && sel.length >= before.length + after.length) {
      replaceRange(ta, s, e, sel.slice(before.length, sel.length - after.length), s, e - before.length - after.length);
    } else {
      replaceRange(ta, s, e, before + sel + after, s + before.length, e + before.length);
    }
  }

  function lineBounds(ta, pos) {
    const v = ta.value;
    const start = v.lastIndexOf("\n", pos - 1) + 1;
    let end = v.indexOf("\n", pos);
    if (end === -1) end = v.length;
    return { start, end, text: v.slice(start, end) };
  }

  function indentSelection(ta, outdent) {
    const s = ta.selectionStart, e = ta.selectionEnd, v = ta.value;
    if (s === e && !outdent) { insertTextTA(ta, "  "); return; }
    const blockStart = v.lastIndexOf("\n", s - 1) + 1;
    let blockEnd = v.indexOf("\n", e === s ? e : e - 1);
    if (blockEnd === -1) blockEnd = v.length;
    const lines = v.slice(blockStart, blockEnd).split("\n");
    const joined = lines.map(l => outdent ? l.replace(/^(\t| {1,2})/, "") : "  " + l).join("\n");
    replaceRange(ta, blockStart, blockEnd, joined, blockStart, blockStart + joined.length);
  }

  function listEnter(ta) {
    const pos = ta.selectionStart;
    if (pos !== ta.selectionEnd) return false;
    const { start, text } = lineBounds(ta, pos);
    const m = text.slice(0, pos - start).match(LIST_LINE_RE);
    if (!m) return false;
    const [, indent, marker, gap, task, rest] = m;
    if (!rest.trim()) { replaceRange(ta, start, pos, indent, start + indent.length); return true; }
    let next = marker;
    if (/^\d+[.)]$/.test(marker)) next = (parseInt(marker, 10) + 1) + marker.slice(-1);
    insertTextTA(ta, "\n" + indent + next + gap + (task ? "[ ] " : ""));
    return true;
  }

  function onSourceKeydown(ev) {
    const ta = editor, mod = ev.ctrlKey || ev.metaKey;
    if (isTab(ev)) { ev.preventDefault(); indentSelection(ta, ev.shiftKey); return; }
    if (ev.key === "Enter" && !mod && !ev.shiftKey) {
      const { start, text } = lineBounds(ta, ta.selectionStart);
      if (LIST_LINE_RE.test(text.slice(0, ta.selectionStart - start))) { ev.preventDefault(); listEnter(ta); }
      return;
    }
    if (!mod || ev.shiftKey || ev.altKey) return;
    const k = ev.key.toLowerCase();
    if (k === "b") { ev.preventDefault(); wrapSelection(ta, "**", "**"); }
    else if (k === "i") { ev.preventDefault(); wrapSelection(ta, "*", "*"); }
    else if (k === "`") { ev.preventDefault(); wrapSelection(ta, "`", "`"); }
    else if (k === "k") {
      ev.preventDefault();
      const s = ta.selectionStart, e = ta.selectionEnd, sel = ta.value.slice(s, e);
      if (/^https?:\/\//.test(sel)) replaceRange(ta, s, e, "[](" + sel + ")", s + 1);
      else replaceRange(ta, s, e, "[" + sel + "](url)", s + sel.length + 3, s + sel.length + 6);
    }
  }

  function onSourceInput() {
    if (mode === "split") {
      clearTimeout(renderTimer);
      renderTimer = setTimeout(renderPage, 120);
    }
    // The title bar and tab dot turn on with the first keystroke; counts and caret follow after a short pause,
    // at most one message per REPORT_MS however fast the typing.
    if (!reportedDirty) {
      reportedDirty = true;
      send({ type: "changed", dirty: true });
    }
    if (!flushTimer) flushTimer = setTimeout(() => notifyChange(true), REPORT_MS);
  }

  // ---------- WYSIWYG page ----------
  const BLOCK_RE = /^(P|DIV|H[1-6]|LI|BLOCKQUOTE|PRE|TD|TH)$/;

  function selectionBlock() {
    const sel = window.getSelection();
    if (!sel || !sel.rangeCount) return null;
    let node = sel.getRangeAt(0).startContainer;
    if (node.nodeType === 3) node = node.parentNode;
    while (node && node !== page) {
      if (BLOCK_RE.test(node.nodeName)) return node;
      node = node.parentNode;
    }
    return null;
  }

  function caretAtEnd(block) {
    const sel = window.getSelection();
    if (!sel.rangeCount) return false;
    const r = sel.getRangeAt(0).cloneRange();
    r.setEnd(block, block.childNodes.length);
    return !r.toString().replace(/\n$/, "").length;
  }

  function caretAtStart(block) {
    const sel = window.getSelection();
    if (!sel.rangeCount) return false;
    const r = sel.getRangeAt(0).cloneRange();
    r.setStart(block, 0);
    return !r.toString().length;
  }

  function textBeforeCaret(block) {
    const sel = window.getSelection();
    if (!sel.rangeCount) return "";
    const r = sel.getRangeAt(0).cloneRange();
    r.setStart(block, 0);
    return r.toString();
  }

  /** Delete the first `n` characters of the block (the shortcut the user typed) keeping undo. */
  function deletePrefix(block, n) {
    const sel = window.getSelection();
    const r = document.createRange();
    r.setStart(block, 0);
    const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
    let remaining = n, node;
    while ((node = walker.nextNode())) {
      if (node.length >= remaining) { r.setEnd(node, remaining); break; }
      remaining -= node.length;
    }
    sel.removeAllRanges(); sel.addRange(r);
    document.execCommand("delete");
  }

  /** Trailing line breaks at the end of `el`: <br> elements or "\n" characters (pre-formatted text). */
  function trailingBreaks(el) {
    let n = 0, node = el.lastChild;
    while (node) {
      if (node.nodeName === "BR") { n++; node = node.previousSibling; continue; }
      if (node.nodeType === 3) {
        const m = node.nodeValue.match(/\n*$/)[0].length;
        n += m;
        if (m < node.nodeValue.length) break;
        node = node.previousSibling; continue;
      }
      break;
    }
    return n;
  }

  function removeTrailingBreaks(el, count) {
    while (count > 0) {
      const node = el.lastChild;
      if (!node) break;
      if (node.nodeName === "BR") { node.remove(); count--; }
      else if (node.nodeType === 3) {
        if (!node.nodeValue) { node.remove(); continue; }
        if (node.nodeValue.endsWith("\n")) { node.nodeValue = node.nodeValue.slice(0, -1); count--; if (!node.nodeValue) node.remove(); }
        else break;
      } else break;
    }
  }

  function placeCaret(node, atEnd) {
    const sel = window.getSelection(), r = document.createRange();
    r.selectNodeContents(node);
    r.collapse(!atEnd);
    sel.removeAllRanges(); sel.addRange(r);
  }

  function makeTask(li) {
    const box = document.createElement("input");
    box.type = "checkbox";
    li.insertBefore(box, li.firstChild);
    decorateTask(li, box);
    placeCaret(li.querySelector(".task-text"), false);
  }

  function makeCodeBlock(block, lang) {
    const pre = document.createElement("pre"), code = document.createElement("code");
    if (lang) code.className = "language-" + lang;
    code.appendChild(document.createElement("br"));
    pre.appendChild(code);
    block.replaceWith(pre);
    placeCaret(code, false);
  }

  /** Markdown-style shortcuts at the start of a block, applied after a space is typed. */
  function applyShortcut(block) {
    if (!block || block.nodeName === "PRE") return false;
    const before = textBeforeCaret(block);
    const inList = block.nodeName === "LI";
    let m;
    const sp = "[ \u00a0]";
    if ((m = before.match(new RegExp("^(#{1,6})" + sp + "$"))) && !inList) {
      deletePrefix(block, m[0].length);
      document.execCommand("formatBlock", false, "H" + m[1].length);
    } else if (new RegExp("^(\\[ ?\\]|-\\s?\\[ ?\\])" + sp + "$").test(before)) {
      deletePrefix(block, before.length);
      if (!inList) document.execCommand("insertUnorderedList");
      const li = selectionBlock();
      if (li && li.nodeName === "LI" && !li.classList.contains("task")) makeTask(li);
    } else if (new RegExp("^[-*+]" + sp + "$").test(before) && !inList) {
      deletePrefix(block, 2);
      document.execCommand("insertUnorderedList");
    } else if ((m = before.match(new RegExp("^1[.)]" + sp + "$"))) && !inList) {
      deletePrefix(block, m[0].length);
      document.execCommand("insertOrderedList");
    } else if (new RegExp("^>" + sp + "$").test(before) && !inList) {
      deletePrefix(block, 2);
      document.execCommand("formatBlock", false, "BLOCKQUOTE");
    } else if ((m = before.match(new RegExp("^```(\\w*)" + sp + "$"))) && !inList) {
      makeCodeBlock(block, m[1]);
    } else {
      return false;
    }
    return true;
  }

  function onPageBeforeInput(ev) {
    // Enter handling is done here so the browser's own structure edits stay undoable.
    if (ev.inputType !== "insertParagraph") return;
    const block = selectionBlock();
    if (!block) return;
    if (block.nodeName === "PRE") {
      ev.preventDefault();
      const code = block.querySelector("code") || block;
      if (caretAtEnd(block) && (trailingBreaks(code) >= 2 || /\n$/.test(textBeforeCaret(block)))) {
        // Enter on an empty last line leaves the code block.
        removeTrailingBreaks(code, trailingBreaks(code));
        if (!code.firstChild) code.appendChild(document.createElement("br"));
        const p = emptyParagraph();
        block.after(p);
        placeCaret(p, false);
      } else {
        document.execCommand("insertLineBreak");
      }
      scheduleSync();
      return;
    }
    if (/^(P|DIV)$/.test(block.nodeName) && /^```\w*$/.test(block.textContent.trim())) {
      ev.preventDefault(); makeCodeBlock(block, block.textContent.trim().slice(3)); scheduleSync(); return;
    }
    if (/^(P|DIV)$/.test(block.nodeName) && /^(-{3,}|\*{3,})$/.test(block.textContent.trim())) {
      ev.preventDefault();
      const hr = document.createElement("hr"); hr.contentEditable = "false";
      const p = emptyParagraph();
      block.replaceWith(hr); hr.after(p); placeCaret(p, false); scheduleSync(); return;
    }
    if (/^H[1-6]$/.test(block.nodeName) && caretAtEnd(block)) {
      ev.preventDefault();
      const p = emptyParagraph();
      block.after(p);
      placeCaret(p, false);
      scheduleSync();
      return;
    }
    if (block.nodeName === "LI" && block.classList.contains("task")) {
      const text = block.querySelector(".task-text");
      if (text && !text.textContent.trim()) {
        // Enter on an empty task: leave the list (the checkbox keeps WebKit from doing it itself).
        ev.preventDefault();
        const list = block.parentNode;
        const p = emptyParagraph();
        if (block.nextElementSibling) {
          // Split the list around the paragraph.
          const rest = list.cloneNode(false);
          while (block.nextSibling) rest.appendChild(block.nextSibling);
          list.after(p); p.after(rest);
        } else {
          list.after(p);
        }
        block.remove();
        if (!list.children.length) list.remove();
        placeCaret(p, false);
        scheduleSync();
      } else if (text && caretAtEnd(block)) {
        ev.preventDefault();
        const li = document.createElement("li");
        block.after(li);
        li.appendChild(document.createElement("br"));
        makeTask(li);
        scheduleSync();
      }
      return;
    }
    const quote = block.closest("blockquote");
    if (quote) {
      ev.preventDefault();
      const inner = block === quote ? quote : block;
      if (!quote.textContent.trim()) {
        const p = emptyParagraph(); quote.replaceWith(p); placeCaret(p, false);
      } else if (caretAtEnd(inner) && trailingBreaks(inner) >= 2) {
        removeTrailingBreaks(inner, 2);
        const p = emptyParagraph(); quote.after(p); placeCaret(p, false);
      } else if (block !== quote && !block.textContent.trim() && !block.nextElementSibling) {
        block.remove();
        const p = emptyParagraph(); quote.after(p); placeCaret(p, false);
      } else {
        document.execCommand("insertLineBreak");
      }
      scheduleSync();
    }
  }

  // Nest or un-nest a list item by moving the <li> itself. The browser's own
  // "indent" command puts a <ul> straight inside a <ul>, which Markdown cannot say.
  function indentListItem(li, outdent) {
    const sel = window.getSelection();
    // A live Range inside a moved node collapses to its old parent, so keep the plain positions instead.
    const caret = sel.rangeCount ? [sel.anchorNode, sel.anchorOffset, sel.focusNode, sel.focusOffset] : null;
    const list = li.parentNode;
    if (!outdent) {
      const prev = li.previousElementSibling;
      if (!prev || prev.nodeName !== "LI") return;          // the first item has nothing to nest under
      let sub = prev.lastElementChild;
      if (!sub || !/^(UL|OL)$/.test(sub.nodeName)) {
        sub = document.createElement(list.nodeName.toLowerCase());
        prev.appendChild(sub);
      }
      sub.appendChild(li);
    } else {
      const parent = list.parentNode;
      if (!parent || parent.nodeName !== "LI") return;      // already at the top level
      const after = [];
      for (let n = li.nextElementSibling; n; n = n.nextElementSibling) after.push(n);
      if (after.length) {                                   // the items below stay below, now inside this one
        let sub = li.lastElementChild;
        if (!sub || !/^(UL|OL)$/.test(sub.nodeName)) {
          sub = document.createElement(list.nodeName.toLowerCase());
          li.appendChild(sub);
        }
        after.forEach(n => sub.appendChild(n));
      }
      parent.after(li);
      if (!list.children.length) list.remove();
    }
    if (caret) sel.setBaseAndExtent(...caret);
  }

  function onPageKeydown(ev) {
    const mod = ev.ctrlKey || ev.metaKey;
    const block = selectionBlock();
    if (isTab(ev)) {
      ev.preventDefault();
      if (block && block.nodeName === "LI") { indentListItem(block, ev.shiftKey); scheduleSync(); }
      else if (block && block.nodeName === "PRE") document.execCommand("insertText", false, "  ");
      return;
    }
    if (ev.key === "Backspace" && block && caretAtStart(block) && window.getSelection().isCollapsed) {
      if (/^H[1-6]$/.test(block.nodeName) || block.nodeName === "BLOCKQUOTE" || block.nodeName === "PRE") {
        // Turn the block back into a paragraph first (like Notion), instead of merging upward.
        ev.preventDefault();
        if (block.nodeName === "PRE") {
          const p = document.createElement("p");
          p.textContent = block.textContent.replace(/\n$/, "");
          if (!p.textContent) p.appendChild(document.createElement("br"));
          block.replaceWith(p); placeCaret(p, false);
        } else {
          document.execCommand("formatBlock", false, "P");
        }
        scheduleSync();
        return;
      }
      if (block.nodeName === "LI" && block.classList.contains("task") && caretAtStart(block.querySelector(".task-text") || block)) {
        ev.preventDefault();
        const box = block.querySelector(":scope > input[type=checkbox]");
        if (box) box.remove();
        block.classList.remove("task", "done");
        scheduleSync();
        return;
      }
    }
    if (ev.key === "ArrowDown" && block && block.nodeName === "PRE" && caretAtEnd(block) && !block.nextElementSibling) {
      const p = emptyParagraph(); block.after(p); placeCaret(p, false); ev.preventDefault(); return;
    }
    if (!mod || ev.altKey) return;
    const k = ev.key.toLowerCase();
    if (k === "b" && !ev.shiftKey) { ev.preventDefault(); document.execCommand("bold"); scheduleSync(); }
    else if (k === "i" && !ev.shiftKey) { ev.preventDefault(); document.execCommand("italic"); scheduleSync(); }
    else if (k === "`") { ev.preventDefault(); toggleInlineCode(); scheduleSync(); }
    else if (k === "k" && !ev.shiftKey) { ev.preventDefault(); insertLink(); scheduleSync(); }
    else if (k === "x" && ev.shiftKey) { ev.preventDefault(); document.execCommand("strikeThrough"); scheduleSync(); }
  }

  function toggleInlineCode() {
    const sel = window.getSelection();
    if (!sel.rangeCount) return;
    const r = sel.getRangeAt(0);
    let node = r.startContainer.nodeType === 3 ? r.startContainer.parentNode : r.startContainer;
    const code = node.closest && node.closest("code");
    if (code && !code.closest("pre")) {
      const text = document.createTextNode(code.textContent);
      code.replaceWith(text);
      const nr = document.createRange(); nr.selectNodeContents(text); sel.removeAllRanges(); sel.addRange(nr);
      return;
    }
    const text = r.toString() || "code";
    const esc = text.replace(/[<>&]/g, c => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c]));
    document.execCommand("insertHTML", false, "<code>" + esc + "</code>");
  }

  function insertLink() {
    const sel = window.getSelection();
    const text = sel.toString();
    if (/^https?:\/\//.test(text)) {
      document.execCommand("createLink", false, text);
    } else {
      const url = "https://";
      const esc = (text || "link").replace(/[<>&]/g, c => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c]));
      document.execCommand("insertHTML", false, '<a href="' + url + '">' + esc + "</a>");
    }
  }

  function onPageInput(ev) {
    if (!ev.inputType || ev.inputType === "insertText") {
      const block = selectionBlock();
      if (block && /[ \u00a0]$/.test(textBeforeCaret(block)) && applyShortcut(block)) { scheduleSync(); return; }
    }
    scheduleSync();
  }

  function onPagePaste(ev) {
    const text = ev.clipboardData && ev.clipboardData.getData("text/plain");
    if (!text) return;
    ev.preventDefault();
    const at = getSelection().anchorNode, inCode = !!(at && (at.nodeType === 1 ? at : at.parentElement).closest("pre"));   // code stays text
    if (!inCode && (/\n/.test(text) || /^(#{1,6} |[-*+] |\d+\. |> |```)/m.test(text) || /\[.+\]\(.+\)|\*\*.+\*\*/.test(text))) {
      const body = sanitize(marked.parse(text));
      decorate(body);
      document.execCommand("insertHTML", false, body.innerHTML);
    } else {
      document.execCommand("insertText", false, text);
    }
    scheduleSync();
  }

  function onPageClick(ev) {
    const a = ev.target.closest("a[href]");
    if (!a) return;
    const href = a.getAttribute("href") || "";
    if (!(ev.ctrlKey || ev.metaKey)) return; // plain click edits the link text
    ev.preventDefault();
    if (href.startsWith("#")) {
      const id = decodeURIComponent(href.slice(1));
      const target = document.getElementById(id) || page.querySelector("[name='" + CSS.escape(id) + "']");
      if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
    } else {
      send({ type: "link", href });
    }
  }

  function onPageChange(ev) {
    const box = ev.target;
    if (!(box instanceof HTMLInputElement) || box.type !== "checkbox") return;
    const li = box.closest("li");
    if (li) li.classList.toggle("done", box.checked);
    if (box.checked) box.setAttribute("checked", ""); else box.removeAttribute("checked");
    scheduleSync();
    notifyChange();
  }

  function onPagePaneMousedown(ev) {
    if (kind !== "md") return;
    // Clicking the empty space below the text puts the caret at the end (or adds a paragraph).
    if (ev.target !== pagePane && ev.target !== page) return;
    ev.preventDefault();
    let last = page.lastElementChild;
    if (!last || /^(PRE|TABLE|HR|UL|OL|BLOCKQUOTE)$/.test(last.nodeName)) { last = emptyParagraph(); page.appendChild(last); scheduleSync(); }
    page.focus();
    placeCaret(last, true);
  }

  // ---------- Copy button on code blocks ----------
  // One floating button, outside #page so Turndown and the HTML export never see it. It follows the pointer
  // from block to block and sits over the top-right corner of the hovered <pre>.
  const copyBtn = document.getElementById("code-copy");
  let copyPre = null;      // the <pre> the button is showing for
  let copyHideTimer = null;
  let copiedTimer = null;

  function codeText(pre) {
    const code = pre.querySelector("code") || pre;
    // Typed blocks hold <br> for line breaks (and a lone <br> as the empty-block placeholder).
    let out = "";
    (function walk(node) {
      for (const n of Array.from(node.childNodes)) {
        if (n.nodeType === 3) out += n.nodeValue;
        else if (n.nodeName === "BR") { if (n.nextSibling || n.parentNode !== code) out += "\n"; }
        else walk(n);
      }
    })(code);
    return out;
  }

  function placeCopy() {
    if (!copyPre || !copyPre.isConnected) { hideCopy(); return; }
    const r = copyPre.getBoundingClientRect(), pane = pagePane.getBoundingClientRect();
    if (r.bottom < pane.top + 28 || r.top > pane.bottom - 8) { copyBtn.hidden = true; return; }
    copyBtn.hidden = false;
    copyBtn.style.top = Math.max(r.top, pane.top) + 6 + "px";
    copyBtn.style.left = Math.min(r.right, pane.right - 12) - copyBtn.offsetWidth - 8 + "px";
  }

  function hideCopy() {
    clearTimeout(copyHideTimer);
    copyPre = null;
    copyBtn.hidden = true;
  }

  function hideCopySoon() {
    clearTimeout(copyHideTimer);
    copyHideTimer = setTimeout(hideCopy, 250);   // long enough to travel from the block onto the button
  }

  function onPageOver(ev) {
    const pre = kind === "md" && ev.target.closest ? ev.target.closest("pre") : null;
    if (!pre || !page.contains(pre)) { if (copyPre) hideCopySoon(); return; }
    clearTimeout(copyHideTimer);
    if (pre !== copyPre) { copyPre = pre; resetCopyLabel(); }
    placeCopy();
  }

  function resetCopyLabel() {
    clearTimeout(copiedTimer);
    copyBtn.textContent = "Copy";
    copyBtn.classList.remove("copied");
  }

  // The host owns the clipboard (navigator.clipboard is not reliable on a file:// page); outside the host, fall back to it.
  function copyToClipboard(text) {
    const host = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.app;
    if (host) { send({ type: "copy", text, note: "Copied the code block" }); return Promise.resolve(); }
    if (navigator.clipboard) return navigator.clipboard.writeText(text);
    return Promise.reject(new Error("no clipboard"));
  }

  function onCopyClick() {
    if (!copyPre) return;
    copyToClipboard(codeText(copyPre)).then(() => {
      copyBtn.textContent = "Copied";
      copyBtn.classList.add("copied");
      clearTimeout(copiedTimer);
      copiedTimer = setTimeout(resetCopyLabel, 1500);
    }, () => {});
  }

  // ---------- Undo / redo ----------
  // WebKitGTK binds Ctrl+Z but not Ctrl+Y, and the binding is unreliable once the host window owns
  // the accelerators, so both are handled here and the native key action is suppressed.
  function historyTarget() {
    const active = document.activeElement;
    if (active === editor) return editor;
    if (kind === "csv") return grid.contains(active) && active.classList.contains("cell") ? grid : mode === "source" ? editor : grid;
    if (page.contains(active)) return page;
    return mode === "source" ? editor : page;
  }

  function runHistory(redo) {
    const target = historyTarget();
    if (target === grid) { if (redo) grid.redo(); else grid.undo(); return; }
    if (document.activeElement !== target && !(target === page && page.contains(document.activeElement))) target.focus();
    document.execCommand(redo ? "redo" : "undo");
    if (target === page) scheduleSync();
  }

  function onHistoryKey(ev) {
    if (!(ev.ctrlKey || ev.metaKey) || ev.altKey) return;
    const k = ev.key.toLowerCase();
    const z = k === "z" || ev.code === "KeyZ", y = k === "y" || ev.code === "KeyY";
    if (!z && !y) return;
    ev.preventDefault();
    runHistory(y || ev.shiftKey);
  }

  // ---------- Scroll sync (split mode) ----------
  function syncScroll(from, to) {
    if (syncing) return;
    const max = from.scrollHeight - from.clientHeight;
    if (max <= 0) return;
    syncing = true;
    to.scrollTop = (from.scrollTop / max) * (to.scrollHeight - to.clientHeight);
    requestAnimationFrame(() => { syncing = false; });
  }

  // ---------- Split layout: divider drag and cursor position ----------
  function clamp(n, lo, hi) { return Math.max(lo, Math.min(hi, n)); }

  function startResize(ev) {
    if (ev.button !== 0) return;
    ev.preventDefault();
    resizing = true;
    divider.classList.add("dragging");
    document.body.classList.add("dragging");
  }

  function onResize(ev) {
    if (!resizing) return;
    const r = workspace.getBoundingClientRect();
    const layout = html.dataset.layout;
    const horizontal = layout === "lr" || layout === "rl";
    let pct = horizontal ? (ev.clientX - r.left) / r.width * 100 : (ev.clientY - r.top) / r.height * 100;
    if (layout === "rl" || layout === "bt") pct = 100 - pct;   // editor sits on the far side
    api.setSplit(pct);
  }

  function stopResize() {
    if (!resizing) return;
    resizing = false;
    divider.classList.remove("dragging");
    document.body.classList.remove("dragging");
    send({ type: "split", pct: parseFloat(workspace.style.getPropertyValue("--split")) });
  }

  function focusPage(atStart) {
    if (kind === "csv") { if (atStart) grid.focus(); return; }
    page.focus();
    if (atStart) placeCaret(page.firstElementChild || page, false);
  }

  function countNewlines(s, from, to) {
    let n = 0, i = s.indexOf("\n", from);
    while (i !== -1 && i < to) { n++; i = s.indexOf("\n", i + 1); }
    return n;
  }

  // The line is remembered together with the text and offset it was counted at. After an edit or a caret move
  // only the stretch between the old and the new offset is scanned, as long as the text before it is unchanged.
  let lineMemo = { text: "", pos: 0, line: 1 };

  function caretPosition() {
    const text = editor.value, pos = editor.selectionStart, memo = lineMemo;
    const k = Math.min(pos, memo.pos);
    let line;
    if (memo.text === text || text.startsWith(memo.text.slice(0, k))) {
      line = memo.line - countNewlines(memo.text, k, memo.pos) + countNewlines(text, k, pos);
    } else {
      line = 1 + countNewlines(text, 0, pos);
    }
    lineMemo = { text, pos, line };
    const col = pos - (pos ? text.lastIndexOf("\n", pos - 1) : -1);
    return { line, col, key: line + ":" + col };
  }

  /** Caret moves that are not edits: one message per pause, and none when the host already has the position. */
  function reportCursor() {
    if (mode === "page" || cursorTimer) return;
    cursorTimer = setTimeout(() => {
      cursorTimer = 0;
      if (flushTimer || mode === "page") return;   // a pending report carries the caret
      const cur = caretPosition();
      if (cur.key === lastCursor) return;
      lastCursor = cur.key;
      send({ type: "cursor", line: cur.line, col: cur.col });
    }, 50);
  }

  // ---------- Public API for the host ----------
  const api = {
    init(opts) {
      opts = opts || {};
      document.execCommand("defaultParagraphSeparator", false, "p");
      document.execCommand("styleWithCSS", false, "false");
      api.setTheme(opts.theme || "light");
      api.setLayout(opts.layout || "lr");
      api.setSplit(opts.split || 50);
      api.setEditorFont(opts.editor_font || 14);
      syncEnabled = opts.sync !== false;
      api.setMode(opts.mode || "page", true);
    },
    load(text, opts) {
      grid.flush();   // a pending table report must not land on the new text
      api.setKind(opts && opts.kind);
      editor.value = text || "";
      savedText = opts && typeof opts.saved === "string" ? opts.saved : editor.value;
      pageDirty = false;
      editor.setSelectionRange(0, 0);
      editor.scrollTop = 0;
      pagePane.scrollTop = 0;
      if (mode !== "source") {
        renderPage();
        if (mode === "page") focusPage(true);
      } else {
        editor.focus();
      }
      notifyChange();
    },
    markClean(text) {     // text: what was written; anything typed since is still unsaved
      savedText = typeof text === "string" ? text : currentText();
      notifyChange();
    },
    sendContent() {
      flushReport();
      send({ type: "content", text: currentText() });
    },
    sendHtml(title) {
      const body = renderHtml(kind === "csv" ? window.NotemarkCsv.toMarkdown(window.NotemarkCsv.parse(currentText()).rows) : currentText());
      body.querySelectorAll("input[type=checkbox]").forEach(b => { b.disabled = true; });
      body.querySelectorAll("[contenteditable]").forEach(el => el.removeAttribute("contenteditable"));
      const css = Array.from(document.styleSheets).map(sheet => {
        try { return Array.from(sheet.cssRules).map(r => r.cssText).join("\n"); } catch (e) { return ""; }
      }).join("\n");
      const esc = s => String(s).replace(/[<>&]/g, c => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c]));
      const doc = "<!DOCTYPE html>\n<html lang=\"en\" data-theme=\"" + html.dataset.theme + "\">\n<head>\n<meta charset=\"utf-8\">\n" +
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n<title>" + esc(title || "Document") +
        "</title>\n<style>\n" + css + "\nhtml,body{height:auto}\n</style>\n</head>\n<body>\n<article class=\"page prose\">\n" +
        body.innerHTML + "\n</article>\n</body>\n</html>\n";
      send({ type: "content", text: doc });
    },
    setLayout(layout) {
      if (!["lr", "rl", "tb", "bt"].includes(layout)) return;
      html.dataset.layout = layout;
    },
    setSplit(pct) { workspace.style.setProperty("--split", clamp(pct, 15, 85) + "%"); },
    setKind(next) {
      kind = next === "csv" ? "csv" : "md";
      html.dataset.kind = kind;
    },
    getKind() { return kind; },
    setEditorFont(px) { document.documentElement.style.setProperty("--src-size", clamp(+px || 14, 10, 28) + "px"); },
    setSync(on) { syncEnabled = !!on; },
    setTheme(theme) { html.dataset.theme = theme === "dark" ? "dark" : "light"; },
    setMode(next, quiet) {
      if (!["page", "source", "split"].includes(next)) return;
      flushReport();
      syncFromPage();
      hideCopy();
      mode = next;
      html.dataset.mode = mode;
      if (mode !== "source") renderPage();
      if (mode === "source") editor.focus();
      else if (mode === "page") focusPage(false);
      if (!quiet) send({ type: "mode", mode });
    },
    undo() { runHistory(false); },
    redo() { runHistory(true); },
    toMarkdown() { return currentText(); },
    // For tests: the page as it is now converted incrementally (null when that falls back), as a whole, and one block; stored nowhere.
    syncProbe: { mapped: () => !!mapping, incremental: () => incrementalMarkdown(true), full: fullMarkdown, block: blockMarkdown },
    getMode() { return mode; },
  };
  window.Notemark = api;

  // ---------- Wiring ----------
  document.addEventListener("keydown", onHistoryKey, true);
  editor.addEventListener("input", onSourceInput);
  editor.addEventListener("keydown", onSourceKeydown);
  editor.addEventListener("blur", flushReport);
  editor.addEventListener("scroll", () => { if (mode === "split" && syncEnabled) syncScroll(editor, pagePane); });
  pagePane.addEventListener("scroll", () => { if (mode === "split" && syncEnabled) syncScroll(pagePane, editor); });
  for (const ev of ["keyup", "click", "focus"]) editor.addEventListener(ev, reportCursor);
  divider.addEventListener("mousedown", startResize);
  divider.addEventListener("dblclick", () => { api.setSplit(50); send({ type: "split", pct: 50 }); });
  document.addEventListener("mousemove", onResize);
  document.addEventListener("mouseup", stopResize);
  pagePane.addEventListener("mousedown", onPagePaneMousedown);
  pagePane.addEventListener("mouseover", onPageOver);
  pagePane.addEventListener("mouseleave", () => { if (copyPre) hideCopySoon(); });
  pagePane.addEventListener("scroll", placeCopy);
  window.addEventListener("resize", placeCopy);
  page.addEventListener("input", () => { if (copyPre) placeCopy(); });
  copyBtn.addEventListener("mousedown", ev => ev.preventDefault());   // keep focus and caret in the editor
  copyBtn.addEventListener("mouseenter", () => clearTimeout(copyHideTimer));
  copyBtn.addEventListener("mouseleave", hideCopySoon);
  copyBtn.addEventListener("click", onCopyClick);
  page.addEventListener("beforeinput", onPageBeforeInput);
  page.addEventListener("keydown", onPageKeydown);
  page.addEventListener("input", onPageInput);
  page.addEventListener("paste", onPagePaste);
  page.addEventListener("click", onPageClick);
  page.addEventListener("change", onPageChange);
  page.addEventListener("blur", () => { syncFromPage(); notifyChange(); });
  send({ type: "ready" });
})();
