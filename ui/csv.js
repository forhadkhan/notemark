/* Notemark CSV support: parser, serialiser and an editable table view.
 *
 * Exposed as window.NotemarkCsv. app.js owns the document text (#editor); this file turns that
 * text into rows, renders them as a grid, and reports edits back through the `onChange` callback.
 */
(function () {
  "use strict";

  const DELIMS = [",", ";", "\t", "|"];

  // WebKitGTK reports Shift+Tab as key "Unidentified" (the key is ISO_Left_Tab); code and keyCode stay right.
  const isTab = ev => ev.key === "Tab" || ev.code === "Tab" || ev.keyCode === 9;

  // ---------- Parse / serialise ----------
  function detectDelimiter(text) {
    // Count candidate delimiters on the first non-empty line, outside quotes.
    const first = text.split(/\r?\n/).find(l => l.trim()) || "";
    let best = ",", bestCount = 0;
    for (const d of DELIMS) {
      let n = 0, quoted = false;
      for (const ch of first) {
        if (ch === '"') quoted = !quoted;
        else if (!quoted && ch === d) n++;
      }
      if (n > bestCount) { best = d; bestCount = n; }
    }
    return best;
  }

  function parse(text, delim) {
    delim = delim || detectDelimiter(text);
    const rows = [];
    let row = [], cell = "", quoted = false, i = 0;
    const n = text.length;
    if (text.charCodeAt(0) === 0xfeff) i = 1;
    for (; i < n; i++) {
      const ch = text[i];
      if (quoted) {
        if (ch === '"') {
          if (text[i + 1] === '"') { cell += '"'; i++; } else quoted = false;
        } else cell += ch;
      } else if (ch === '"' && cell === "") {
        quoted = true;
      } else if (ch === delim) {
        row.push(cell); cell = "";
      } else if (ch === "\n" || ch === "\r") {
        if (ch === "\r" && text[i + 1] === "\n") i++;
        row.push(cell); rows.push(row); row = []; cell = "";
      } else cell += ch;
    }
    if (cell !== "" || row.length) { row.push(cell); rows.push(row); }
    // Make every row as wide as the widest one.
    const width = rows.reduce((m, r) => Math.max(m, r.length), 0);
    rows.forEach(r => { while (r.length < width) r.push(""); });
    return { rows, delim };
  }

  function quote(value, delim) {
    return /["\r\n]/.test(value) || value.includes(delim) || /^\s|\s$/.test(value)
      ? '"' + value.replace(/"/g, '""') + '"' : value;
  }

  function stringify(rows, delim) {
    delim = delim || ",";
    if (!rows.length) return "";
    return rows.map(r => r.map(c => quote(c, delim)).join(delim)).join("\n") + "\n";
  }

  function toMarkdown(rows) {
    if (!rows.length) return "";
    const esc = c => String(c).replace(/\\/g, "\\\\").replace(/\|/g, "\\|").replace(/\r?\n/g, "<br>");
    const line = r => "| " + r.map(esc).join(" | ") + " |";
    const width = rows[0].length;
    return [line(rows[0]), "| " + Array(width).fill("---").join(" | ") + " |"]
      .concat(rows.slice(1).map(line)).join("\n") + "\n";
  }

  // ---------- Table view ----------
  const ICONS = {
    plus: '<path d="M12 5v14M5 12h14"/>',
    minus: '<path d="M5 12h14"/>',
    copy: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h8"/>',
    up: '<path d="m6 15 6-6 6 6"/>',
    down: '<path d="m6 9 6 6 6-6"/>',
  };
  function icon(name) {
    return '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"' +
      ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + ICONS[name] + "</svg>";
  }

  function create(root, hooks) {
    let rows = [[""]];
    let delim = ",";
    let sortCol = -1, sortDir = 1;
    let filter = "";
    let selected = { r: 0, c: 0 };
    let history = [], future = [];
    let session = null;     // the cell being typed in: the table as it was before its first keystroke
    const HISTORY_MAX = 200;
    const MAX_ROWS = 2000;   // rows listed at once; filter or use source mode beyond that
    const ROW_EST = 36;      // px per row until the first rows have been measured
    const OVERSCAN = 12;     // rows kept in the DOM above and below the viewport
    const FILTER_MS = 100;   // pause after a filter keystroke before the rows are searched
    const CHANGE_MS = 150;   // pause after a cell keystroke before the text is rebuilt and reported
    const COL_MIN = 110, COL_MAX = 420, ROWNUM_W = 44;

    root.innerHTML =
      '<div class="grid-bar">' +
      '<button type="button" data-act="row-add" title="Add row below the selected row">' + icon("plus") + "<span>Add row</span></button>" +
      '<button type="button" data-act="col-add" title="Add column after the selected column">' + icon("plus") + "<span>Add column</span></button>" +
      '<button type="button" data-act="row-del" title="Delete the selected row">' + icon("minus") + "<span>Delete row</span></button>" +
      '<button type="button" data-act="col-del" title="Delete the selected column">' + icon("minus") + "<span>Delete column</span></button>" +
      '<span class="grid-sep"></span>' +
      '<button type="button" data-act="copy-md" title="Copy as Markdown table">' + icon("copy") + "<span>Markdown</span></button>" +
      '<button type="button" data-act="copy-csv" title="Copy as CSV">' + icon("copy") + "<span>CSV</span></button>" +
      '<input type="search" class="grid-filter" placeholder="Filter rows" spellcheck="false" autocomplete="off">' +
      '<span class="grid-info"></span></div>' +
      '<div class="grid-scroll"><table class="grid-table"></table></div>';
    const scroller = root.querySelector(".grid-scroll");
    const table = root.querySelector(".grid-table");
    const info = root.querySelector(".grid-info");
    const filterInput = root.querySelector(".grid-filter");

    const snapshot = () => JSON.stringify(rows);
    function pushHistory(before) {
      if (history.length && history[history.length - 1] === before) return;
      history.push(before);
      if (history.length > HISTORY_MAX) history.shift();
      future = [];
    }

    // Typing only marks the text stale (the title bar turns dirty at once); the CSV text is rebuilt after a pause,
    // and flush() brings it up to date for anything that reads it (save, mode switch, blur).
    let changeTimer = 0;
    function changed() {
      clearTimeout(changeTimer);
      changeTimer = 0;
      hooks.onChange(stringify(rows, delim));
    }
    function changedSoon() {
      if (hooks.onEdit) hooks.onEdit();
      clearTimeout(changeTimer);
      changeTimer = setTimeout(changed, CHANGE_MS);
    }
    function flush() { if (changeTimer) changed(); }

    // ---------- Rows: filter, sort and the visible window ----------
    // Only the rows in and around the viewport are in the DOM; two spacer rows stand in for the rest.
    // `view` lists the data rows (indices into `rows`) in display order, `offs[i]` is the top of view[i] in px.
    let view = [];
    let capped = false;   // more rows match than MAX_ROWS: `view` stops short of the last one
    let offs = new Float64Array(1);
    let winA = 0, winB = 0;          // view[winA..winB) is in the DOM
    let heights = new WeakMap();     // measured row heights, keyed by the row array so they survive inserts and sorts
    let estH = 0;                    // height assumed for rows not measured yet; fixed after the first batch
    let headH = 0;                   // the sticky header's height
    let needMeasure = false;
    const dirtyRows = new Set();      // rows typed in, to be measured again
    let lowered = new WeakMap();     // row array -> its cells lower-cased and joined, for the filter
    let colW = [];
    let tbody = null, topSp = null, botSp = null;
    const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

    const esc = s => s.replace(/[&<>\r]/g, ch => ch === "&" ? "&amp;" : ch === "<" ? "&lt;" : ch === ">" ? "&gt;" : "&#13;");

    function lowerOf(row) {
      let t = lowered.get(row);
      if (t === undefined) { t = row.map(c => c.toLowerCase()).join("\n"); lowered.set(row, t); }
      return t;
    }

    function visibleRows() {
      let idx = [];
      const f = filter.trim().toLowerCase();
      for (let i = 1; i < rows.length; i++) if (!f || lowerOf(rows[i]).includes(f)) idx.push(i);
      if (sortCol >= 0) {
        const num = idx.every(i => rows[i][sortCol] === "" || !isNaN(Number(rows[i][sortCol])));
        idx.sort((a, b) => {
          const x = rows[a][sortCol], y = rows[b][sortCol];
          const cmp = num ? (Number(x) || 0) - (Number(y) || 0) : collator.compare(x, y);
          return cmp * sortDir || a - b;
        });
      }
      return idx;
    }

    const rowEst = () => estH || ROW_EST;
    function layoutOffsets() {
      const n = view.length, est = rowEst();
      offs = new Float64Array(n + 1);
      for (let i = 0; i < n; i++) offs[i + 1] = offs[i] + (heights.get(rows[view[i]]) || est);
    }
    /** The view index of the row at height y (the last row starting at or above it). */
    function rowAt(y) {
      let lo = 0, hi = view.length - 1;
      while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (offs[mid] <= y) lo = mid; else hi = mid - 1; }
      return lo;
    }

    // Column widths come from the data, so they stay put while rows come and go from the DOM.
    let measureCtx = null;
    function textWidth(text, font) {
      if (!measureCtx) measureCtx = document.createElement("canvas").getContext("2d");
      if (!measureCtx) return text.length * 8;
      measureCtx.font = font;
      return measureCtx.measureText(text).width;
    }
    function longestLine(v) {
      if (v.indexOf("\n") < 0) return v;
      let best = "";
      for (const line of v.split("\n")) if (line.length > best.length) best = line;
      return best;
    }
    function fitColumns() {
      const cs = getComputedStyle(table), size = cs.fontSize + " " + cs.fontFamily;
      const old = colW.join();
      colW = [];
      for (let c = 0; c < rows[0].length; c++) {
        let widest = "", n = 0;
        for (let i = 1; i < rows.length; i++) {
          const v = rows[i][c];
          if (v.length > n) { const line = longestLine(v); if (line.length > n) { widest = line; n = line.length; } }
        }
        const w = Math.max(textWidth(widest, size) + 22, textWidth(rows[0][c], "650 " + size) + 46);
        colW.push(Math.min(COL_MAX, Math.max(COL_MIN, Math.ceil(w))));
      }
      if (colW.join() !== old) { heights = new WeakMap(); needMeasure = true; }
    }
    function applyColumns() {
      const cols = table.querySelectorAll("col");
      colW.forEach((w, c) => { cols[c + 1].style.width = w + "px"; });
      table.style.width = ROWNUM_W + colW.reduce((a, b) => a + b, 0) + "px";
    }

    function rowHtml(r) {
      const row = rows[r];
      let h = '<tr><td class="rownum" data-r="' + r + '">' + r + "</td>";
      for (let c = 0; c < row.length; c++) {
        h += (r === selected.r && c === selected.c ? '<td class="sel">' : "<td>") +
          '<span class="cell" contenteditable="plaintext-only" spellcheck="false" data-r="' + r + '" data-c="' + c + '">' + esc(row[c]) + "</span></td>";
      }
      return h + "</tr>";
    }

    const rowEls = () => Array.from(tbody.children).filter(el => el !== topSp && el !== botSp);
    function setSpacer(which, px) {
      const isTop = which === "top";
      let sp = isTop ? topSp : botSp;
      if (px < 0.5) { if (sp) { sp.remove(); sp = null; } }
      else {
        if (!sp) {
          sp = document.createElement("tr");
          sp.className = "vs";
          sp.innerHTML = '<td colspan="' + (rows[0].length + 1) + '"></td>';
          if (isTop) tbody.insertBefore(sp, tbody.firstChild); else tbody.appendChild(sp);
        }
        sp.firstChild.style.height = px + "px";
      }
      if (isTop) topSp = sp; else botSp = sp;
    }
    function syncSpacers() {
      setSpacer("top", offs[winA]);
      setSpacer("bot", offs[view.length] - offs[winB]);
    }

    /** Take rows out of the DOM; a cell being typed in is committed to the history first. */
    function dropRows(els) {
      const active = document.activeElement;
      if (active && els.some(el => el.contains(active))) commitCell();
      els.forEach(el => el.remove());
    }

    /** Make view[a..b) the rows in the DOM, touching only the rows that enter or leave. Returns the view ranges added. */
    function setWindow(a, b) {
      const added = [];
      if (winB <= winA || a >= winB || b <= winA) {
        dropRows(rowEls());
        let h = "";
        for (let i = a; i < b; i++) h += rowHtml(view[i]);
        if (botSp) botSp.insertAdjacentHTML("beforebegin", h); else tbody.insertAdjacentHTML("beforeend", h);
        if (b > a) added.push([a, b]);
      } else {
        const els = rowEls();
        if (a > winA) dropRows(els.slice(0, a - winA));
        if (b < winB) dropRows(els.slice(els.length - (winB - b)));
        if (a < winA) {
          let h = "";
          for (let i = a; i < winA; i++) h += rowHtml(view[i]);
          if (topSp) topSp.insertAdjacentHTML("afterend", h); else tbody.insertAdjacentHTML("afterbegin", h);
          added.push([a, winA]);
        }
        if (b > winB) {
          let h = "";
          for (let i = winB; i < b; i++) h += rowHtml(view[i]);
          if (botSp) botSp.insertAdjacentHTML("beforebegin", h); else tbody.insertAdjacentHTML("beforeend", h);
          added.push([winB, b]);
        }
      }
      winA = a; winB = b;
      return added;
    }

    /** Record the heights of the rendered rows view[from..to); true when any differs from what the offsets assume. */
    function measure(from, to) {
      from = Math.max(from, winA); to = Math.min(to, winB);
      if (to <= from) return false;
      const els = rowEls();
      const hs = [];
      for (let i = from; i < to; i++) hs.push(els[i - winA].getBoundingClientRect().height);
      let moved = false;
      if (!estH) { estH = hs.reduce((a, b) => a + b, 0) / hs.length; moved = true; }
      hs.forEach((h, k) => {
        const row = rows[view[from + k]];
        if (Math.abs(h - (heights.get(row) || rowEst())) > 0.5) moved = true;
        heights.set(row, h);
      });
      return moved;
    }

    /** Bring the DOM in line with the scroll position: render what is in view plus a margin, measure it, fix the spacers. */
    function updateWindow(force, scrollTop) {
      if (!tbody) return;   // the ResizeObserver can fire before the first render
      const n = view.length;
      for (let pass = 0; pass < 3; pass++) {
        const st = scrollTop === undefined ? scroller.scrollTop : scrollTop;
        scrollTop = undefined;
        const vh = scroller.clientHeight || 600;
        const y0 = Math.max(0, st - headH);
        const va = n ? rowAt(y0) : 0, vb = n ? Math.min(n, rowAt(Math.max(y0, st + vh - headH)) + 1) : 0;
        const keep = !force && winB > winA && (winA === 0 || va - winA >= 4) && (winB === n || winB - vb >= 4);
        let added = [];
        if (!keep) {
          added = setWindow(Math.max(0, va - OVERSCAN), Math.min(n, vb + OVERSCAN));
          force = false;
          syncSpacers();   // before any measuring: a layout with short spacers would clamp the scroll position
        }
        let moved = false;
        if (scroller.clientHeight) {   // hidden: every height would read 0
          for (const [a, b] of added) moved = measure(a, b) || moved;
          if (needMeasure) { needMeasure = false; moved = measure(winA, winB) || moved; }
          for (const r of dirtyRows) { const p = view.indexOf(r); if (p >= 0) moved = measure(p, p + 1) || moved; }
          dirtyRows.clear();
        }
        if (!moved) break;
        const anchor = offs[va];
        layoutOffsets();
        syncSpacers();
        if (Math.abs(offs[va] - anchor) > 0.5) scroller.scrollTop = st + offs[va] - anchor;
        scrollTop = scroller.scrollTop;
      }
    }

    let frame = 0, frameTimer = 0;
    function schedule() {
      if (frame) return;
      const run = () => { cancelAnimationFrame(frame); clearTimeout(frameTimer); frame = 0; updateWindow(false); };
      frame = requestAnimationFrame(run);
      frameTimer = setTimeout(run, 100);   // a frame never comes while the view is hidden or throttled
    }

    /** Scroll so the row is visible below the sticky header, rendering it if it is outside the window. */
    function revealRow(r) {
      const pos = view.indexOf(r);
      if (pos < 0) return false;
      const st = scroller.scrollTop, vh = scroller.clientHeight || 600;
      let next = st;
      if (offs[pos] < st) next = offs[pos];
      else if (headH + offs[pos + 1] > st + vh) next = headH + offs[pos + 1] - vh;
      const outside = pos < winA || pos >= winB;
      if (next !== st || outside) {
        scroller.scrollTop = next;
        updateWindow(outside, scroller.scrollTop);
      }
      return true;
    }

    let filterTimer = 0;
    function render() {
      closeMenu(false);   // a menu open over the old rows would act on the wrong cells
      commitCell();
      if (filterTimer) { clearTimeout(filterTimer); filterTimer = 0; }
      filter = filterInput.value;
      const keepTop = scroller.scrollTop;
      const width = rows[0].length;
      const all = visibleRows();
      capped = all.length > MAX_ROWS;
      view = capped ? all.slice(0, MAX_ROWS) : all;
      fitColumns();
      layoutOffsets();
      let h = '<colgroup><col style="width:' + ROWNUM_W + 'px">' + colW.map(w => '<col style="width:' + w + 'px">').join("") +
        '</colgroup><thead><tr><th class="rownum"></th>';
      for (let c = 0; c < width; c++) {
        const arrow = sortCol === c ? '<span class="sort">' + icon(sortDir > 0 ? "up" : "down") + "</span>" : "";
        h += '<th data-c="' + c + '"><div class="th-in"><span class="cell" contenteditable="plaintext-only" spellcheck="false" data-r="0" data-c="' + c + '">' + esc(rows[0][c]) + "</span>" +
          '<button type="button" class="sort-btn" data-sort="' + c + '" title="Sort by this column">' + (arrow || icon("down")) + "</button></div></th>";
      }
      table.innerHTML = h + "</thead><tbody></tbody>";
      table.style.width = ROWNUM_W + colW.reduce((a, b) => a + b, 0) + "px";
      tbody = table.tBodies[0];
      topSp = botSp = null;
      winA = winB = 0;
      updateWindow(true, keepTop);
      if (scroller.clientHeight) headH = table.tHead.getBoundingClientRect().height;
      if (scroller.scrollTop !== keepTop) scroller.scrollTop = keepTop;   // the emptied table may have clamped it
      info.textContent = (rows.length - 1) + " rows · " + width + " columns" + (filter.trim() || all.length > view.length ? " · " + view.length + " shown" : "");
    }

    function load(text) {
      const parsed = parse(text || "");
      rows = parsed.rows.length ? parsed.rows : [[""]];
      delim = parsed.delim;
      history = []; future = []; session = null; sortCol = -1; selected = { r: 0, c: 0 };
      heights = new WeakMap(); estH = 0;
      scroller.scrollTop = 0;
      render();
    }

    function restore(snap) {
      session = null;
      rows = JSON.parse(snap);
      render();
      changed();
    }
    function undo() {
      if (!history.length) return;
      future.push(snapshot());
      restore(history.pop());
    }
    function redo() {
      if (!future.length) return;
      history.push(snapshot());
      restore(future.pop());
    }

    /** End the cell session: it becomes one undo step if the cell's text really changed. */
    function commitCell() {
      const s = session;
      session = null;
      if (!s || s.row[s.c] === s.value) return;
      pushHistory(s.before);
      fitCell(s.row[s.c], s.c);
    }
    /** A cell edited longer than its column widens the column (a column never narrows while editing). */
    function fitCell(value, c) {
      if (!(c < colW.length)) return;
      const cs = getComputedStyle(table);
      const w = Math.min(COL_MAX, Math.ceil(textWidth(longestLine(value), cs.fontSize + " " + cs.fontFamily) + 22));
      if (w <= colW[c]) return;
      colW[c] = w;
      const cols = table.querySelectorAll("col");
      cols[c + 1].style.width = w + "px";
      table.style.width = ROWNUM_W + colW.reduce((a, b) => a + b, 0) + "px";
      heights = new WeakMap();
      needMeasure = true;
      schedule();
    }

    function edit(fn) {
      commitCell();
      const before = snapshot();
      fn();
      pushHistory(before);
      lowered = new WeakMap();
      render();
      changed();
    }

    function cellEl(r, c) {
      return table.querySelector('.cell[data-r="' + r + '"][data-c="' + c + '"]');
    }
    function focusCell(r, c) {
      if (r > 0 && !revealRow(r)) return;
      const el = cellEl(r, c);
      if (el) { el.focus(); const sel = getSelection(), range = document.createRange(); range.selectNodeContents(el); range.collapse(false); sel.removeAllRanges(); sel.addRange(range); }
    }


    // Structural edits, shared by the toolbar, the keyboard and the right-click menu.
    // Row 0 is the header: it can't have a row inserted above it or be deleted.
    function insertRow(at, c) {
      if (at < 1) return;
      edit(() => rows.splice(at, 0, Array(rows[0].length).fill("")));
      focusCell(at, c);
    }
    function insertCol(at, r) {
      edit(() => {
        rows.forEach(row => row.splice(at, 0, ""));
        if (sortCol >= at) sortCol++;
      });
      focusCell(r, at);
    }
    function deleteRow(at, c) {
      if (at < 1 || rows.length < 2) return;
      edit(() => rows.splice(at, 1));
      focusCell(Math.min(at, rows.length - 1), c);
    }
    function deleteCol(at, r) {
      if (rows[0].length < 2) return;
      edit(() => {
        rows.forEach(row => row.splice(at, 1));
        if (sortCol === at) sortCol = -1;
        else if (sortCol > at) sortCol--;
      });
      focusCell(r, Math.min(at, rows[0].length - 1));
    }

    function act(name) {
      const { r, c } = selected;
      if (name === "row-add") insertRow(r + 1, c);
      else if (name === "col-add") insertCol(c + 1, r);
      else if (name === "row-del") deleteRow(r, c);
      else if (name === "col-del") deleteCol(c, r);
      else if (name === "copy-md") hooks.copy(toMarkdown(rows), "Copied as a Markdown table");
      else if (name === "copy-csv") hooks.copy(stringify(rows, delim), "Copied as CSV");
    }

    // ---------- Right-click menu (also Shift+F10 and the Menu key) ----------
    let menu = null, menuItems = [], menuCell = null;

    function menuEntries(r, c) {
      const header = r === 0;
      return [
        { label: "Insert row above", icon: "plus", off: header, run: () => insertRow(r, c) },
        { label: "Insert row below", icon: "plus", run: () => insertRow(r + 1, c) },
        { label: "Insert column left", icon: "plus", run: () => insertCol(c, r) },
        { label: "Insert column right", icon: "plus", run: () => insertCol(c + 1, r) },
        null,
        { label: "Delete row", icon: "minus", off: header || rows.length < 2, run: () => deleteRow(r, c) },
        { label: "Delete column", icon: "minus", off: rows[0].length < 2, run: () => deleteCol(c, r) },
      ];
    }

    function closeMenu(refocus) {
      if (!menu) return;
      menu.remove();
      menu = null;
      document.removeEventListener("mousedown", closeMenuOnOutside, true);
      document.removeEventListener("keydown", menuKey, true);
      window.removeEventListener("resize", closeMenuNow);
      window.removeEventListener("scroll", closeMenuNow, true);
      if (refocus && menuCell) focusCell(menuCell.r, menuCell.c);
    }
    const closeMenuNow = () => closeMenu(false);
    function closeMenuOnOutside(ev) { if (menu && !menu.contains(ev.target)) closeMenu(false); }

    function menuKey(ev) {
      if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); closeMenu(true); }
      else if (ev.key === "Tab") closeMenu(false);
      else if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
        ev.preventDefault(); ev.stopPropagation();
        const items = [...menu.querySelectorAll("button:not([disabled])")];
        if (!items.length) return;
        const i = items.indexOf(document.activeElement);
        const next = ev.key === "ArrowDown" ? (i + 1) % items.length : (i <= 0 ? items.length - 1 : i - 1);
        items[next].focus();
      }
    }

    function openMenu(x, y, r, c, fromKeyboard) {
      closeMenu(false);
      selected = { r, c };
      menuCell = { r, c };
      const entries = menuEntries(r, c);
      menuItems = entries.filter(Boolean);
      menu = document.createElement("div");
      menu.className = "ctx-menu";
      menu.setAttribute("role", "menu");
      menu.innerHTML = entries.map(e => e === null
        ? '<div class="ctx-sep" role="separator"></div>'
        : '<button type="button" role="menuitem" data-i="' + menuItems.indexOf(e) + '"' + (e.off ? " disabled" : "") + ">" +
          icon(e.icon) + "<span>" + e.label + "</span></button>").join("");
      menu.addEventListener("click", ev => {
        const b = ev.target.closest("button[data-i]");
        if (!b || b.disabled) return;
        const item = menuItems[+b.dataset.i];
        closeMenu(false);
        item.run();
      });
      document.body.appendChild(menu);
      const w = menu.offsetWidth, h = menu.offsetHeight;
      menu.style.left = Math.max(4, Math.min(x, innerWidth - w - 4)) + "px";
      menu.style.top = Math.max(4, Math.min(y, innerHeight - h - 4)) + "px";
      document.addEventListener("mousedown", closeMenuOnOutside, true);
      document.addEventListener("keydown", menuKey, true);
      window.addEventListener("resize", closeMenuNow);
      window.addEventListener("scroll", closeMenuNow, true);
      if (fromKeyboard) { const first = menu.querySelector("button:not([disabled])"); if (first) first.focus(); }
    }

    root.addEventListener("contextmenu", ev => {
      const cell = ev.target.closest(".cell");
      const num = ev.target.closest("td.rownum");
      if (!cell && !num) return;
      ev.preventDefault();
      if (cell) openMenu(ev.clientX, ev.clientY, +cell.dataset.r, +cell.dataset.c);
      else { focusCell(+num.dataset.r, selected.c); openMenu(ev.clientX, ev.clientY, +num.dataset.r, selected.c); }
    });

    root.addEventListener("click", ev => {
      const sortBtn = ev.target.closest("[data-sort]");
      if (sortBtn) {
        const c = +sortBtn.dataset.sort;
        if (sortCol === c) { if (sortDir === 1) sortDir = -1; else { sortCol = -1; sortDir = 1; } } else { sortCol = c; sortDir = 1; }
        render();
        return;
      }
      const btn = ev.target.closest("[data-act]");
      if (btn) act(btn.dataset.act);
    });
    // Keep focus (and so the selected cell) when a toolbar button is pressed.
    root.addEventListener("mousedown", ev => { if (ev.target.closest("[data-act]")) ev.preventDefault(); });


    table.addEventListener("focusin", ev => {
      const el = ev.target.closest(".cell");
      if (!el) return;
      selected = { r: +el.dataset.r, c: +el.dataset.c };
      table.querySelectorAll(".sel").forEach(n => n.classList.remove("sel"));
      el.parentElement.closest("td, th").classList.add("sel");
    });
    table.addEventListener("input", ev => {
      const el = ev.target.closest(".cell");
      if (!el) return;
      const r = +el.dataset.r, c = +el.dataset.c;
      if (session && (session.row !== rows[r] || session.c !== c)) commitCell();
      // The first keystroke in a cell takes the undo snapshot (the model still holds the old text here).
      if (!session) session = { before: snapshot(), row: rows[r], c, value: rows[r][c] };
      rows[r][c] = el.textContent;
      lowered.delete(rows[r]);
      if (r > 0) { dirtyRows.add(r); schedule(); }
      changedSoon();
    });
    table.addEventListener("focusout", ev => {
      if (!ev.target.closest(".cell")) return;
      commitCell();
      if (!table.contains(ev.relatedTarget)) flush();   // focus left the grid: bring the text up to date
    });
    table.addEventListener("keydown", ev => {
      const el = ev.target.closest(".cell");
      if (!el) return;
      const r = +el.dataset.r, c = +el.dataset.c, width = rows[0].length;
      if (ev.key === "ContextMenu" || (ev.key === "F10" && ev.shiftKey)) {
        ev.preventDefault();
        const box = el.getBoundingClientRect();
        openMenu(box.left + 8, box.bottom, r, c, true);
        return;
      }
      const nav = ev.key === "Enter" || isTab(ev) || ev.key === "ArrowUp" || ev.key === "ArrowDown";
      if (!nav) { if (ev.key === "Escape") el.blur(); return; }
      const move = (nr, nc) => { ev.preventDefault(); focusCell(nr, nc); };
      // Positions run 0 (the header) then the listed rows; rows that are not rendered yet are scrolled to by focusCell.
      const len = view.length + 1;
      const at = i => i === 0 ? 0 : view[i - 1];
      const pos = r === 0 ? 0 : view.indexOf(r) + 1 || -1;
      if (ev.key === "Enter" && !ev.shiftKey) { move(at(Math.min(pos + 1, len - 1)), c); }
      else if (ev.key === "Enter" && ev.shiftKey) { ev.preventDefault(); document.execCommand("insertText", false, "\n"); }
      else if (isTab(ev)) {
        if (ev.shiftKey) { if (c > 0) move(r, c - 1); else if (pos > 0) move(at(pos - 1), width - 1); else ev.preventDefault(); }
        else if (c < width - 1) move(r, c + 1);
        else if (pos < len - 1) move(at(pos + 1), 0);
        else { ev.preventDefault(); if (!capped) { selected = { r: rows.length - 1, c }; act("row-add"); } }   // not a row nobody can see
      } else if (ev.key === "ArrowUp" && pos > 0) move(at(pos - 1), c);
      else if (ev.key === "ArrowDown" && pos < len - 1) move(at(pos + 1), c);
    });
    filterInput.addEventListener("input", () => {
      clearTimeout(filterTimer);
      filterTimer = setTimeout(() => { scroller.scrollTop = 0; render(); }, FILTER_MS);
    });
    scroller.addEventListener("scroll", schedule, { passive: true });
    if (window.ResizeObserver) {
      new ResizeObserver(() => {
        if (scroller.clientHeight) headH = table.tHead ? table.tHead.getBoundingClientRect().height : headH;
        needMeasure = true;
        schedule();
      }).observe(scroller);
    }
    table.addEventListener("paste", ev => {
      const text = ev.clipboardData && ev.clipboardData.getData("text/plain");
      if (!text) return;
      ev.preventDefault();
      const el = ev.target.closest(".cell");
      if (!el) return;
      if (/[\t\n]/.test(text.replace(/\n$/, ""))) {
        // A block of cells (for example copied from a spreadsheet) fills the grid from the selected cell.
        const block = parse(text.replace(/\r?\n$/, ""), "\t").rows;
        const r0 = +el.dataset.r, c0 = +el.dataset.c;
        edit(() => {
          block.forEach((line, i) => {
            while (rows.length <= r0 + i) rows.push(Array(rows[0].length).fill(""));
            line.forEach((v, j) => {
              while (rows[0].length <= c0 + j) rows.forEach(row => row.push(""));
              rows[r0 + i][c0 + j] = v;
            });
          });
        });
      } else {
        document.execCommand("insertText", false, text);
      }
    });

    return {
      load, undo, redo, flush,
      get text() { return stringify(rows, delim); },
      get rows() { return rows; },
      get delimiter() { return delim; },
      hasHistory: () => history.length > 0 || future.length > 0,
      focus() { focusCell(0, 0); },
      contains: node => root.contains(node),
    };
  }

  window.NotemarkCsv = { parse, stringify, toMarkdown, detectDelimiter, create };
})();

