/* Notemark page logic.
 *
 * Modes:
 *   page   – Notion-like WYSIWYG: the rendered document is contenteditable. You type into the
 *            formatted text; Markdown shortcuts (# , - , 1. , [] , > , ```) convert as you type.
 *            The HTML is converted back to Markdown (Turndown) for saving.
 *   source – one plain Markdown textarea.
 *   split  – source on the left, WYSIWYG page on the right.
 *
 * #editor (textarea) always holds the canonical Markdown text.
 */
(function () {
  "use strict";

  const html = document.documentElement;
  const editor = document.getElementById("editor");
  const page = document.getElementById("page");
  const editorPane = document.getElementById("editor-pane");
  const pagePane = document.getElementById("page-pane");

  let mode = "page";
  let savedText = "";
  let renderTimer = null;
  let syncTimer = null;
  let pageDirty = false;   // page DOM has edits not yet converted into #editor
  let syncing = false;

  // ---------- Host bridge ----------
  function send(msg) {
    try { window.webkit.messageHandlers.app.postMessage(JSON.stringify(msg)); } catch (e) { /* not in GTK host */ }
  }

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

  function renderHtml(markdown) {
    const body = sanitize(marked.parse(markdown));
    decorate(body);
    return body;
  }

  function renderPage() {
    const body = renderHtml(editor.value);
    page.replaceChildren(...Array.from(body.childNodes));
    if (!page.firstElementChild) page.appendChild(emptyParagraph());
    pageDirty = false;
  }

  /** Convert the page DOM back into Markdown and store it as the canonical text. */
  function syncFromPage() {
    clearTimeout(syncTimer);
    if (!pageDirty) return;
    let md = turndown.turndown(page).replace(/\u00a0/g, " ").replace(/\n{3,}/g, "\n\n").replace(/[ \t]+$/gm, m => m.length >= 2 ? "  " : "");
    md = md.replace(/^\n+/, "");
    if (md && !md.endsWith("\n")) md += "\n";
    if (!md.trim()) md = "";
    editor.value = md;
    autogrow(editor);
    pageDirty = false;
  }

  function scheduleSync() {
    pageDirty = true;
    clearTimeout(syncTimer);
    syncTimer = setTimeout(() => { syncFromPage(); notifyChange(); }, 150);
  }

  // ---------- Text helpers ----------
  function countWords(text) {
    const t = text.trim();
    return t ? t.split(/\s+/).length : 0;
  }

  function currentText() {
    syncFromPage();
    return editor.value;
  }

  function notifyChange() {
    const text = currentText();
    send({ type: "changed", dirty: text !== savedText, words: countWords(text), chars: text.length });
  }

  // ---------- Source editor ----------
  const LIST_LINE_RE = /^(\s*)([-*+]|\d+[.)])(\s+)(\[[ xX]\]\s+)?(.*)$/;

  function autogrow(ta) {
    ta.style.height = "auto";
    ta.style.height = ta.scrollHeight + "px";
  }

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
    if (ev.key === "Tab") { ev.preventDefault(); indentSelection(ta, ev.shiftKey); return; }
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
    autogrow(editor);
    if (mode === "split") {
      clearTimeout(renderTimer);
      renderTimer = setTimeout(renderPage, 120);
    }
    notifyChange();
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

  function onPageKeydown(ev) {
    const mod = ev.ctrlKey || ev.metaKey;
    const block = selectionBlock();
    if (ev.key === "Tab") {
      ev.preventDefault();
      if (block && block.nodeName === "LI") document.execCommand(ev.shiftKey ? "outdent" : "indent");
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
    if (/\n/.test(text) || /^(#{1,6} |[-*+] |\d+\. |> |```)/m.test(text) || /\[.+\]\(.+\)|\*\*.+\*\*/.test(text)) {
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
    // Clicking the empty space below the text puts the caret at the end (or adds a paragraph).
    if (ev.target !== pagePane && ev.target !== page) return;
    ev.preventDefault();
    let last = page.lastElementChild;
    if (!last || /^(PRE|TABLE|HR|UL|OL|BLOCKQUOTE)$/.test(last.nodeName)) { last = emptyParagraph(); page.appendChild(last); scheduleSync(); }
    page.focus();
    placeCaret(last, true);
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

  // ---------- Public API for the host ----------
  const api = {
    init(opts) {
      opts = opts || {};
      document.execCommand("defaultParagraphSeparator", false, "p");
      document.execCommand("styleWithCSS", false, "false");
      api.setTheme(opts.theme || "light");
      api.setMode(opts.mode || "page", true);
      autogrow(editor);
    },
    load(text) {
      editor.value = text || "";
      savedText = editor.value;
      pageDirty = false;
      autogrow(editor);
      editor.setSelectionRange(0, 0);
      editorPane.scrollTop = 0;
      pagePane.scrollTop = 0;
      if (mode !== "source") {
        renderPage();
        if (mode === "page") { page.focus(); placeCaret(page.firstElementChild || page, false); }
      } else {
        editor.focus();
      }
      notifyChange();
    },
    markClean() {
      savedText = currentText();
      notifyChange();
    },
    sendContent() {
      send({ type: "content", text: currentText() });
    },
    sendHtml(title) {
      const body = renderHtml(currentText());
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
    setTheme(theme) { html.dataset.theme = theme === "dark" ? "dark" : "light"; },
    setMode(next, quiet) {
      if (!["page", "source", "split"].includes(next)) return;
      syncFromPage();
      mode = next;
      html.dataset.mode = mode;
      if (mode !== "source") renderPage();
      if (mode === "source") { autogrow(editor); editor.focus(); }
      else if (mode === "page") page.focus();
      if (!quiet) send({ type: "mode", mode });
    },
    toMarkdown() { return currentText(); },
    getMode() { return mode; },
  };
  window.Notemark = api;

  // ---------- Wiring ----------
  editor.addEventListener("input", onSourceInput);
  editor.addEventListener("keydown", onSourceKeydown);
  editorPane.addEventListener("scroll", () => { if (mode === "split") syncScroll(editorPane, pagePane); });
  pagePane.addEventListener("scroll", () => { if (mode === "split") syncScroll(pagePane, editorPane); });
  pagePane.addEventListener("mousedown", onPagePaneMousedown);
  page.addEventListener("beforeinput", onPageBeforeInput);
  page.addEventListener("keydown", onPageKeydown);
  page.addEventListener("input", onPageInput);
  page.addEventListener("paste", onPagePaste);
  page.addEventListener("click", onPageClick);
  page.addEventListener("change", onPageChange);
  page.addEventListener("blur", () => { syncFromPage(); notifyChange(); });
  window.addEventListener("resize", () => autogrow(editor));
  editorPane.addEventListener("mousedown", ev => {
    if (ev.target === editorPane || ev.target.classList.contains("page-inner")) {
      ev.preventDefault();
      editor.focus();
      editor.setSelectionRange(editor.value.length, editor.value.length);
    }
  });

  send({ type: "ready" });
})();
