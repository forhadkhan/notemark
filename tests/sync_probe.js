/* Page-side half of tests/test_sync.py: random edit sequences against the incremental page -> Markdown sync.
 *
 * The oracle does not look at the app's records. It remembers, per top-level block, the source text it was
 * rendered from (the marked lexer's raw text, found on its own) and the HTML it had then, and checks that
 *   - a block that still has that HTML (or is back to it after an undo) shows up in the text verbatim,
 *   - every other block shows up as Turndown's rendering of it, which must also be found, in order, in the
 *     whole-page conversion (what the app did before the incremental sync),
 *   - nothing else is in the text and a blank line separates what was touched from its neighbours,
 * and that the real sync (what Ctrl+S does) returns the same text, and that a second look finds nothing to change.
 */
(function () {
  "use strict";
  const N = window.Notemark, P = N.syncProbe, page = document.getElementById("page");
  const BLOCK = /^(P|H[1-6]|UL|OL|PRE|BLOCKQUOTE|TABLE|HR|DIV)$/;
  const S = window.S = { model: null, rnd: null, fallbacks: 0, checks: 0, reparsed: 0 };

  function mulberry32(a) {
    return function () {
      a |= 0; a = (a + 0x6D2B79F5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  const rnd = () => S.rnd();
  const pick = arr => arr[Math.floor(rnd() * arr.length)];
  const trimNl = s => s.replace(/\n+$/, "");
  const fire = () => page.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "historyUndo" }));

  /** Load a document and learn each block's source text from the lexer. Returns whether the app mapped it. */
  S.start = function (md) {
    N.setMode("page");
    N.load(md);
    const mapped = P.mapped();
    const items = [];
    for (const t of marked.lexer(md)) {
      if (t.type === "space") continue;
      if (t.type === "def") { if (items.length) items[items.length - 1].keep.push(trimNl(t.raw)); continue; }
      items.push({ md: trimNl(t.raw), keep: [] });
    }
    const els = Array.from(page.children);
    if (els.length === items.length + 1) els.pop();   // the paragraph to type into, added after a final table or code block
    S.model = null;
    if (mapped) {
      if (els.length !== items.length) throw new Error("mapped, but " + els.length + " blocks for " + items.length + " tokens");
      S.model = new Map();
      els.forEach((el, i) => S.model.set(el, { html: el.outerHTML, md: items[i].md, bhtml: el.outerHTML, bmd: items[i].md, keep: items[i].keep }));
    }
    return mapped;
  };

  const trimEnd = s => s.replace(/\n+$/, "");
  /** Whether `text`, written after `before`, would be read as part of the block above it (or as another kind of block). */
  function absorbed(before, text) {
    const toks = marked.lexer(trimEnd(before) + "\n\n" + text).filter(t => t.type !== "space");
    return trimEnd(toks[toks.length - 1].raw) !== trimEnd(text);
  }
  /** What a reader of the Markdown sees: the top-level blocks, by kind and by their words (code exactly). */
  function shape(md) {
    const box = document.createElement("div");
    box.innerHTML = marked.parse(md);
    const out = [];
    for (const e of box.children) {
      const text = e.nodeName === "PRE" ? e.textContent : e.textContent.replace(/\s+/g, " ").trim();
      // Lists that touch each other are one list to Turndown's blank line (it writes the same marker for both), so they count as one.
      if (/^[UO]L$/.test(e.nodeName) && out.length && out[out.length - 1].tag === e.nodeName) out[out.length - 1].text = (out[out.length - 1].text + " " + text).trim();
      else out.push({ tag: e.nodeName, text });
    }
    return out.map(b => b.tag + ":" + b.text).join("\n");
  }

  /** Compare the sync with the oracle. Returns a list of problems (empty when all is well). */
  S.check = function (label) {
    S.checks++;
    const errs = [];
    const bad = (what, extra) => errs.push(label + ": " + what + (extra ? " | " + extra : ""));
    fire();
    const inc = P.incremental(), full = P.full();
    let weird = false;
    for (const n of page.childNodes) {
      if (n.nodeType === 1) { if (!BLOCK.test(n.nodeName)) weird = true; }
      else if (!(n.nodeType === 3 && !/[^ \t\n\r]/.test(n.nodeValue))) weird = true;
    }
    const els = Array.from(page.children), present = new Set(els);
    let lostKeep = false;
    for (const [el, m] of S.model) if (!present.has(el) && m.keep.length) lostKeep = true;

    // What the text must hold, in order: [{text, touched, after}] with `after` the kind of gap needed before the next.
    const pieces = [], learn = [];
    let prevTouched = false, prevKey = null;
    for (const el of els) {
      const m = S.model.get(el), html = el.outerHTML;
      let text, state;
      if (m && html === m.html) { text = m.md; state = 0; }
      else if (m && html === m.bhtml) { text = m.bmd; state = 1; }
      else { text = P.block(el); state = 2; }
      learn.push([el, m, state, text]);
      const keep = m ? m.keep : [];
      if (text) pieces.push({ text, touched: state === 2, el, kept: state !== 2 });
      for (const k of keep) pieces.push({ text: k, touched: state === 2, noFull: true, el });   // definitions are not in the page
    }
    S.lastPieces = pieces;

    if (weird || lostKeep) {
      if (inc !== null) bad("expected the fallback", weird ? "odd top-level node" : "link definitions lost");
    } else if (inc === null) {
      bad("unexpected fallback to whole-page Turndown");
    } else {
      let pos = 0;
      for (let i = 0; i < pieces.length; i++) {
        const p = pieces[i];
        let idx = inc.indexOf(p.text, pos);
        // A source block whose old neighbour is gone may be written again by Turndown, but only when keeping its text would
        // have changed what the file means: the block would have been taken in by the one above it.
        if (idx < 0 && p.kept && !p.noFull) {
          const alt = P.block(p.el), at = inc.indexOf(alt, pos);
          if (at >= 0 && absorbed(inc.slice(0, at), p.text)) { idx = at; p.swapped = alt; p.touched = true; }
        }
        if (idx < 0) { bad("block missing or out of order", JSON.stringify(p.text.slice(0, 80)) + " in " + JSON.stringify(inc.slice(pos, pos + 120))); break; }
        const gap = inc.slice(pos, idx);
        if (!/^\n*$/.test(gap)) { bad("stray text between blocks", JSON.stringify(gap.slice(0, 80))); break; }
        const nls = gap.length;
        const prev = pieces[i - 1];
        if (prev && prev.el !== p.el && (prev.touched || p.touched) && nls < 2) { bad("no blank line next to a touched block", JSON.stringify(prev.text.slice(-30) + "|" + p.text.slice(0, 30))); break; }
        pos = idx + (p.swapped || p.text).length;
      }
      if (!errs.length && !/^\n*$/.test(inc.slice(pos))) bad("stray text at the end", JSON.stringify(inc.slice(pos, pos + 80)));
      if (inc && !inc.endsWith("\n") && pieces.length && (pieces[pieces.length - 1].touched)) bad("touched last block without a newline at the end");
      if (inc && /^\n/.test(inc) && pieces.length && pieces[0].touched) bad("blank lines before a touched first block");
      // Touched blocks are what the whole-page pass writes for them, and in the same order.
      let at = 0;
      for (const p of pieces) {
        if (!p.touched || p.noFull) continue;
        const text = p.swapped || p.text, idx = full.indexOf(text, at);
        if (idx < 0) { bad("touched block differs from the whole-page conversion", JSON.stringify(text.slice(0, 80))); break; }
        at = idx + text.length;
      }
    }

    // Whatever the text is made of, a reader of the file must see the blocks that Turndown would have written: the same
    // kinds in the same order with the same words (not the same inline markup: source text keeps its own style).
    if (inc !== null && !errs.length) {
      const a = shape(inc), b = shape(full);
      if (a !== b) bad("the text parses differently from the whole-page conversion", "incremental " + JSON.stringify(inc.slice(0, 160)) + " => " + a.slice(0, 200) + " | full => " + b.slice(0, 200));
      S.reparsed++;
    }

    // Now do it for real, as the editor's own sync does.
    const got = N.toMarkdown();
    const want = inc === null ? full : inc;
    if (got !== want) bad("the real sync differs from the dry run", JSON.stringify(got.slice(0, 100)) + " vs " + JSON.stringify(want.slice(0, 100)));
    if (errs.length && page.innerHTML.length < 600) errs.push("html " + JSON.stringify(page.innerHTML) + " text " + JSON.stringify(inc) + " model " + JSON.stringify(els.map(e => { const m = S.model.get(e); return m ? [m.html, m.bhtml] : null; })));
    if (inc === null) S.fallbacks++;
    else {
      const again = P.incremental();
      if (again !== got) bad("a second look changes the text", JSON.stringify((again || "null").slice(0, 100)));
      for (const [el, m, state, text] of learn) {
        if (state === 0) continue;
        if (state === 1) S.model.set(el, { html: m.bhtml, md: m.bmd, bhtml: m.bhtml, bmd: m.bmd, keep: m.keep });
        else S.model.set(el, { html: el.outerHTML, md: text, bhtml: m ? m.bhtml : "", bmd: m ? m.bmd : "", keep: m ? m.keep : [] });
      }
      // Removed blocks stay in the model: an undo can bring the same node back, with its source text.
    }
    return errs;
  };

  // ---------- Edits ----------
  const sel = () => getSelection();
  function textNodes(el) {
    const out = [], w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let n = w.nextNode(); n; n = w.nextNode()) if (n.nodeValue.length) out.push(n);
    return out;
  }
  function place(node, off) {
    const r = document.createRange(); r.setStart(node, off); r.collapse(true);
    sel().removeAllRanges(); sel().addRange(r);
  }
  function caretIn(el, where) {   // "start" | "end" | undefined (anywhere)
    const tns = textNodes(el);
    if (!tns.length) { const r = document.createRange(); r.selectNodeContents(el); r.collapse(where !== "end"); sel().removeAllRanges(); sel().addRange(r); return; }
    const n = where === "start" ? tns[0] : where === "end" ? tns[tns.length - 1] : pick(tns);
    place(n, where === "start" ? 0 : where === "end" ? n.nodeValue.length : Math.floor(rnd() * (n.nodeValue.length + 1)));
  }
  function selectRange(a, b) {
    const ta = textNodes(a), tb = textNodes(b);
    const r = document.createRange();
    if (ta.length) r.setStart(ta[0], 0); else r.setStartBefore(a);
    if (tb.length) r.setEnd(tb[tb.length - 1], tb[tb.length - 1].nodeValue.length); else r.setEndAfter(b);
    sel().removeAllRanges(); sel().addRange(r);
  }
  const WORDS = ["alpha", "x", "beta gamma", "q", "delta", "é", "zz"];
  const PASTES = [
    "# Pasted\n\nsome *text* here\n\n- a\n- b\n", "para one\n\npara two\n\n```js\nlet a;\n```\n", "> quote\n\n1. one\n2. two\n",
    "| a | b |\n|---|---|\n| 1 | 2 |\n\nafter\n", "## Head\n\n- [ ] task\n- [x] done\n",
  ];
  const HTMLS = ["<h2>Inserted</h2><p>and a paragraph</p>", "<ul><li>one</li><li>two</li></ul><p>tail</p>", "<p>just one</p>", "<blockquote>q</blockquote><p>r</p>"];

  const OPS = {
    type(els) { const el = pick(els); caretIn(el); document.execCommand("insertText", false, pick(WORDS)); },
    typeMany(els) { const el = pick(els); caretIn(el); for (const ch of "hello world") document.execCommand("insertText", false, ch); },
    enter(els) { caretIn(pick(els)); document.execCommand("insertParagraph"); },
    enterKey(els) {
      caretIn(pick(els));
      const ev = new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, bubbles: true, cancelable: true });
      page.dispatchEvent(ev);
      if (!ev.defaultPrevented) document.execCommand("insertParagraph");
    },
    merge(els) { if (els.length < 2) return; caretIn(pick(els.slice(1)), "start"); document.execCommand("delete"); },
    backspaceKey(els) {
      if (els.length < 2) return;
      caretIn(pick(els.slice(1)), "start");
      const ev = new KeyboardEvent("keydown", { key: "Backspace", code: "Backspace", keyCode: 8, bubbles: true, cancelable: true });
      page.dispatchEvent(ev);
      if (!ev.defaultPrevented) document.execCommand("delete");
    },
    deleteBlock(els) { const el = pick(els); selectRange(el, el); document.execCommand("delete"); },
    removeBlock(els) { if (els.length > 1) pick(els).remove(); },
    deleteSpan(els) {
      if (els.length < 2) return;
      const i = Math.floor(rnd() * (els.length - 1)), j = i + 1 + Math.floor(rnd() * Math.min(3, els.length - 1 - i));
      selectRange(els[i], els[j]); document.execCommand("delete");
    },
    paste(els) { caretIn(pick(els), "end"); S.paste(pick(PASTES)); },
    insertHtml(els) { caretIn(pick(els), "end"); document.execCommand("insertHTML", false, pick(HTMLS)); },
    checkbox() { const boxes = page.querySelectorAll("input[type=checkbox]"); if (boxes.length) pick(Array.from(boxes)).click(); },
    heading(els) { caretIn(pick(els)); document.execCommand("formatBlock", false, pick(["H1", "H2", "H3", "H4", "P", "BLOCKQUOTE"])); },
    shortcut(els) {
      const el = pick(els.filter(e => /^(P|H\d)$/.test(e.nodeName)).concat([els[0]]));
      caretIn(el, "start");
      for (const ch of pick(["## ", "### ", "- ", "1. ", "> ", "[] "])) document.execCommand("insertText", false, ch);
    },
    bold(els) {
      const el = pick(els), tns = textNodes(el);
      if (!tns.length) return;
      const n = pick(tns), a = Math.floor(rnd() * n.nodeValue.length), b = a + 1 + Math.floor(rnd() * (n.nodeValue.length - a));
      const r = document.createRange(); r.setStart(n, a); r.setEnd(n, Math.min(b, n.nodeValue.length));
      sel().removeAllRanges(); sel().addRange(r);
      document.execCommand(pick(["bold", "italic", "strikeThrough"]));
    },
    list(els) { caretIn(pick(els)); document.execCommand(pick(["insertUnorderedList", "insertOrderedList"])); },
    tab(els) {
      const lis = page.querySelectorAll("li"); if (!lis.length) return;
      caretIn(pick(Array.from(lis)));
      page.dispatchEvent(new KeyboardEvent("keydown", { key: rnd() < .5 ? "Tab" : "Unidentified", code: "Tab", keyCode: 9, shiftKey: rnd() < .5, bubbles: true, cancelable: true }));
    },
    append(els) { caretIn(els[els.length - 1], "end"); document.execCommand("insertParagraph"); document.execCommand("insertText", false, "appended"); },
    undo() { document.execCommand("undo"); },
    redo() { document.execCommand("redo"); },
  };
  const EDITS = ["type", "type", "typeMany", "enter", "enterKey", "merge", "backspaceKey", "deleteBlock", "removeBlock", "deleteSpan", "paste", "insertHtml", "checkbox", "heading", "shortcut", "bold", "list", "tab", "append"];
  const ALL = EDITS.concat(["undo", "undo", "redo"]);

  S.paste = function (text) {
    const dt = new DataTransfer(); dt.setData("text/plain", text);
    page.dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }));
  };

  function apply(name) {
    const els = Array.from(page.children);
    if (!els.length) return false;
    page.focus();
    try { OPS[name](els); } catch (e) { return false; }
    fire();
    return true;
  }

  /** `seqs` random edit sequences of up to `len` operations on `md`, checked at random points and at the end. */
  S.fuzz = function (md, seed, seqs, len) {
    S.rnd = mulberry32(seed);
    S.fallbacks = 0; S.checks = 0; S.reparsed = 0;
    const errs = [], log = [];
    let ops = 0, mapped = true;
    for (let q = 0; q < seqs && errs.length < 5; q++) {
      mapped = S.start(md);
      if (!mapped) return { mapped: false, errs: [] };
      log.length = 0;
      const n = 1 + Math.floor(rnd() * len);
      for (let k = 0; k < n; k++) {
        const name = pick(ALL);
        try { if (apply(name)) { ops++; log.push(name); } }
        catch (x) { errs.push("seq " + q + " after " + log.join(",") + "," + name + ": threw " + x.message + " | html " + JSON.stringify(page.innerHTML.slice(0, 500))); break; }
        if (rnd() < 0.4 || k === n - 1) {
          let e;
          try { e = S.check("seq " + q + " after " + log.join(",")); }
          catch (x) { e = ["seq " + q + " after " + log.join(",") + ": threw " + x.message + " | html " + JSON.stringify(page.innerHTML.slice(0, 500))]; }
          if (e.length) { errs.push(...e); break; }
        }
      }
    }
    return { mapped, seqs, ops, checks: S.checks, reparsed: S.reparsed, fallbacks: S.fallbacks, errs: errs.slice(0, 5) };
  };

  /** Edit, let the sync run, undo back to the same page: the text must be the document's own again. */
  S.undoRoundTrip = function (md, seed, tries) {
    S.rnd = mulberry32(seed);
    const errs = [];
    let restored = 0, lost = 0, same = 0;
    const edits = ["type", "typeMany", "enter", "enterKey", "merge", "backspaceKey", "deleteBlock", "deleteSpan", "paste", "insertHtml", "checkbox", "heading", "shortcut", "bold", "list", "tab", "append"];
    for (let q = 0; q < tries && errs.length < 5; q++) {
      if (!S.start(md)) return { mapped: false, errs: [] };
      const h0 = page.innerHTML, names = [];
      for (let k = 1 + Math.floor(rnd() * 2); k > 0; k--) { const n = pick(edits); if (apply(n)) names.push(n); }
      if (page.innerHTML === h0) { same++; continue; }
      const mid = N.toMarkdown();   // the sync that follows typing
      let u = 0;
      while (page.innerHTML !== h0 && u++ < 30) { document.execCommand("undo"); fire(); }
      if (page.innerHTML !== h0) { lost++; continue; }
      restored++;
      const back = N.toMarkdown();
      if (back !== md) errs.push("undo of " + names.join(",") + " (seq " + q + ") gives different text: " + JSON.stringify(back.slice(0, 600)) + " after " + JSON.stringify(mid.slice(0, 600)));
    }
    return { mapped: true, restored, notRestorable: lost, noChange: same, errs };
  };
  return "ready";
})();
