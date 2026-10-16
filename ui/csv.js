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
    let cellStart = null;   // snapshot taken when a cell gets focus
    const HISTORY_MAX = 200;
    const MAX_ROWS = 2000;   // rows drawn at once; filter or use source mode beyond that

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
    function changed() { hooks.onChange(stringify(rows, delim)); }

    function visibleRows() {
      let idx = rows.map((_, i) => i).filter(i => i > 0);
      const f = filter.trim().toLowerCase();
      if (f) idx = idx.filter(i => rows[i].some(c => c.toLowerCase().includes(f)));
      if (sortCol >= 0) {
        const num = idx.every(i => rows[i][sortCol] === "" || !isNaN(Number(rows[i][sortCol])));
        idx.sort((a, b) => {
          const x = rows[a][sortCol], y = rows[b][sortCol];
          const cmp = num ? (Number(x) || 0) - (Number(y) || 0) : x.localeCompare(y, undefined, { numeric: true, sensitivity: "base" });
          return cmp * sortDir || a - b;
        });
      }
      return idx;
    }

    function render() {
      closeMenu(false);   // a menu open over the old rows would act on the wrong cells
      const width = rows[0].length;
      const all = visibleRows();
      const body = all.slice(0, MAX_ROWS);
      let h = "<thead><tr><th class=\"rownum\"></th>";
      for (let c = 0; c < width; c++) {
        const arrow = sortCol === c ? '<span class="sort">' + icon(sortDir > 0 ? "up" : "down") + "</span>" : "";
        h += '<th data-c="' + c + '"><div class="th-in"><span class="cell" contenteditable="plaintext-only" spellcheck="false" data-r="0" data-c="' + c + '"></span>' +
          '<button type="button" class="sort-btn" data-sort="' + c + '" title="Sort by this column">' + (arrow || icon("down")) + "</button></div></th>";
      }
      h += "</tr></thead><tbody>";
      for (const r of body) {
        h += '<tr><td class="rownum" data-r="' + r + '">' + r + "</td>";
        for (let c = 0; c < width; c++) h += '<td><span class="cell" contenteditable="plaintext-only" spellcheck="false" data-r="' + r + '" data-c="' + c + '"></span></td>';
        h += "</tr>";
      }
      table.innerHTML = h + "</tbody>";
      table.querySelectorAll(".cell").forEach(el => { el.textContent = rows[+el.dataset.r][+el.dataset.c]; });
      info.textContent = (rows.length - 1) + " rows · " + width + " columns" + (filter.trim() || all.length > body.length ? " · " + body.length + " shown" : "");
    }

    function load(text) {
      const parsed = parse(text || "");
      rows = parsed.rows.length ? parsed.rows : [[""]];
      delim = parsed.delim;
      history = []; future = []; sortCol = -1; selected = { r: 0, c: 0 };
      render();
    }

    function restore(snap) {
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

    function edit(fn) {
      const before = snapshot();
      fn();
      pushHistory(before);
      render();
      changed();
    }

    function focusCell(r, c) {
      const el = table.querySelector('.cell[data-r="' + r + '"][data-c="' + c + '"]');
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
      cellStart = snapshot();
      table.querySelectorAll(".sel").forEach(n => n.classList.remove("sel"));
      el.parentElement.closest("td, th").classList.add("sel");
    });
    table.addEventListener("input", ev => {
      const el = ev.target.closest(".cell");
      if (!el) return;
      rows[+el.dataset.r][+el.dataset.c] = el.textContent;
      changed();
    });
    table.addEventListener("focusout", ev => {
      if (ev.target.closest(".cell") && cellStart !== null && cellStart !== snapshot()) pushHistory(cellStart);
      cellStart = null;
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
      const move = (nr, nc) => { ev.preventDefault(); focusCell(nr, nc); };
      const order = visibleRows(); order.unshift(0);
      const pos = order.indexOf(r);
      if (ev.key === "Enter" && !ev.shiftKey) { const n = order[Math.min(pos + 1, order.length - 1)]; move(n, c); }
      else if (ev.key === "Enter" && ev.shiftKey) { ev.preventDefault(); document.execCommand("insertText", false, "\n"); }
      else if (isTab(ev)) {
        if (ev.shiftKey) { if (c > 0) move(r, c - 1); else if (pos > 0) move(order[pos - 1], width - 1); else ev.preventDefault(); }
        else if (c < width - 1) move(r, c + 1);
        else if (pos < order.length - 1) move(order[pos + 1], 0);
        else { ev.preventDefault(); selected = { r: rows.length - 1, c }; act("row-add"); }
      } else if (ev.key === "ArrowUp" && pos > 0) move(order[pos - 1], c);
      else if (ev.key === "ArrowDown" && pos < order.length - 1) move(order[pos + 1], c);
      else if (ev.key === "Escape") { el.blur(); }
    });
    filterInput.addEventListener("input", () => { filter = filterInput.value; render(); });
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
      load, undo, redo,
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
