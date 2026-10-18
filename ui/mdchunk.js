/* Notemark chunked Markdown parse.
 *
 * marked.parse is quadratic in WebKit's JavaScriptCore (150 KB takes seconds), so a large document
 * is cut into chunks of a few KB at blank lines, each chunk is parsed on its own and the HTML is
 * joined. The cut points are chosen so the result equals one parse of the whole text: never inside
 * a fenced code block or a raw HTML block that may hold blank lines, never before an indented line
 * (code or list continuation), never inside a list. When in doubt it does not cut, and a document
 * it cannot split safely (an odd link reference definition) is parsed whole.
 *
 * Exposed as window.NotemarkChunk (and module.exports for the Node tests).
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.NotemarkChunk = factory();
}(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // Documents shorter than MIN_LENGTH take the plain parse; longer ones are cut once a chunk reaches TARGET.
  const MIN_LENGTH = 8 * 1024;
  const TARGET = 5 * 1024;

  const FENCE_OPEN = /^ {0,3}(`{3,}|~{3,})(.*)$/;
  const HTML_OPEN = /^ {0,3}<(?:(script|pre|style|textarea)(?=[\s>]|$)|(!--)|(\?)|(!\[CDATA\[)|(![A-Za-z]))/i;
  const HTML_END = { comment: /-->/, pi: /\?>/, cdata: /\]\]>/, decl: />/ };
  const LIST_FENCE = /^ {0,3}(?:[-*+]|\d{1,9}[.)])[ \t]+(?:(?:[-*+]|\d{1,9}[.)]|>)[ \t]*)*(?:`{3,}|~{3,})/;
  // A line that may start an HTML block (types 6 and 7 end at a blank line, so a fence-like line inside one is content).
  const LT_LINE = /^\s*(?:(?:[-*+]|\d{1,9}[.)])[ \t]+)*</;
  const THEMATIC_BREAK = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
  const LIST_MARKER = /^ {0,3}(?:[-*+]|\d{1,9}[.)])(?:[ \t]|$)/;
  // Link reference definitions: one-line ones are copied into every chunk that may use them; anything else bails.
  const DEF_LIKE = / {0,3}\[(?:[^\]\\]|\\.){0,999}\]:/y;   // sticky: a label may span lines
  const DEF_IN_CONTAINER = /^(?:[ \t]*>[ \t>]*|[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+|(?: {4,}|\t)[ \t]*)\[(?:[^\]\\\n]|\\.)*\]:/;
  const DEF_LINE = /^ {0,3}\[((?=[^\]]*\S)(?:[^\[\]\\\n]|\\.)+)\]:[ \t]*(?:<(?:[^<>\n\\]|\\.)*>|[^\s<]\S*)(?:[ \t]+(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|\((?:[^()\\]|\\.)*\)))?[ \t]*$/;
  const TITLE_START = /^[ \t]*["'(]/;

  // Labels match ignoring case and runs of whitespace (and Unicode case folding, as in "ẞ" vs "SS").
  const fold = s => s.toLowerCase().toUpperCase().toLowerCase().replace(/\s+/g, " ");

  /** The chunks of `src`, or null when it is not worth (or not safe) to split. `src` must use \n line ends. */
  function split(src, target) {
    const chunks = [];
    const defs = [];   // { label, line }
    let chunkStart = 0;
    let pos = 0;
    let fence = null;      // while inside a fenced code block: how it closes and how far its list item is indented
    let htmlEnd = null;    // regex that ends the raw HTML block we are in
    let ltRun = false;     // a line since the last blank line starts with "<"
    let listOpen = false;  // a list marker was seen and no unindented paragraph has closed it since
    let itemIndent = 0;    // column where the text of the last list item starts
    let sawBlank = true;
    let prevDef = false;

    while (pos < src.length) {
      let eol = src.indexOf("\n", pos);
      if (eol < 0) eol = src.length;
      const line = src.slice(pos, eol);
      const lineStart = pos;
      pos = eol + 1;

      if (fence) {
        if (!line.trim()) continue;
        const m = fence.close.exec(line);
        // Less indented than its list item, a line ends the item and the fence with it: not worth working out.
        if (fence.minIndent && line.length - line.trimStart().length < fence.minIndent) return null;
        if (m && m[1].length <= 3) fence = null;
        else if (m && m[1].length <= fence.itemClose) return null;   // closes in a list item, or is code in a top-level fence
        continue;
      }
      if (htmlEnd) {
        if (htmlEnd.test(line)) htmlEnd = null;
        continue;
      }
      if (!line.trim()) { sawBlank = true; ltRun = false; continue; }

      const indented = line[0] === " " || line[0] === "\t";
      const rule = THEMATIC_BREAK.test(line);
      const marker = !rule && LIST_MARKER.test(line);

      // Cut here: a blank line came before, the line is a new top-level block, and no open list can swallow it.
      if (sawBlank && !indented && (!marker || !listOpen) && lineStart > chunkStart && lineStart - chunkStart >= target) {
        chunks.push(src.slice(chunkStart, lineStart));
        chunkStart = lineStart;
      }
      if (marker) {
        const lead = /^( *)([-*+]|\d{1,9}[.)])( *)(\S?)/.exec(line);
        const gap = lead[4] && lead[3].length <= 4 ? lead[3].length : 1;   // 5+ spaces or nothing: the item text starts 1 after the marker
        listOpen = true;
        itemIndent = lead[1].length + lead[2].length + gap;
      } else if (!indented && (sawBlank || rule)) {
        listOpen = false;
      }

      let m;
      const opener = (m = FENCE_OPEN.exec(line)) && !(m[1][0] === "`" && m[2].includes("`"));
      // Whether a fence or raw-HTML opener right after a "<" line starts a block or is HTML content depends on that line.
      const html = !opener && (m = HTML_OPEN.exec(line));
      if ((opener || html) && ltRun) return null;
      // Right after a text line it may be a paragraph or setext heading line instead, depending on details of marked.
      if (html && !sawBlank) return null;
      if (opener) {
        const indent = line.length - line.trimStart().length;
        const inItem = listOpen && indent > 0;
        if (inItem && indent < itemIndent) return null;
        // marked closes on the opening run followed by any more ` or ~. In a list item the closing fence may be
        // indented further than 3, but a stale list state can make a top-level fence look like one: then give up.
        fence = {
          close: new RegExp("^( *)" + m[1][0] + "{" + m[1].length + "}[~`]* *$"),
          itemClose: inItem ? itemIndent + 3 : 0,
          minIndent: inItem ? itemIndent : 0,
        };
      } else if (html) {
        // marked ends <script>, <pre>, <style> and <textarea> at their own closing tag only.
        const end = m[1] ? new RegExp("</" + m[1] + ">", "i") : HTML_END[m[2] ? "comment" : m[3] ? "pi" : m[4] ? "cdata" : "decl"];
        // Look for the end after the opener itself, so the block is never closed early.
        if (!end.test(line.slice(m[0].length))) htmlEnd = end;
      }
      if (LT_LINE.test(line)) ltRun = true;
      if (LIST_FENCE.test(line)) return null;   // where such a fence closes depends on the item's indent

      if (DEF_IN_CONTAINER.test(line)) return null;
      DEF_LIKE.lastIndex = lineStart;
      const like = line.trimStart()[0] === "[" ? DEF_LIKE.exec(src) : null;
      if (like && like[0].includes("\n")) return null;
      if (like) {
        const d = DEF_LINE.exec(line);
        if (!d || !(sawBlank || prevDef)) return null;
        defs.push({ label: fold(d[1].trim()), line: line.trim() });
        prevDef = true;
      } else {
        if (prevDef && TITLE_START.test(line)) return null;   // a title on the next line belongs to the definition
        prevDef = false;
      }
      sawBlank = false;
    }
    if (!chunks.length) return null;
    chunks.push(src.slice(chunkStart));

    // References may be defined in a different chunk from where they are used.
    if (defs.length) {
      return chunks.map(text => {
        if (!text.includes("[")) return text;
        const flat = fold(text);
        const own = defs.filter(d => flat.includes(d.label)).map(d => d.line);
        return own.length ? own.join("\n") + "\n\n" + text : text;
      });
    }
    return chunks;
  }

  /** parseFn (marked.parse) applied chunk by chunk; same HTML as parseFn(src). */
  function parse(src, parseFn, opts) {
    const min = opts && opts.min != null ? opts.min : MIN_LENGTH;
    if (src.length < min) return parseFn(src);
    const chunks = split(src.replace(/\r\n?/g, "\n"), opts && opts.target != null ? opts.target : TARGET);
    if (!chunks) return parseFn(src);
    let html = "";
    for (const chunk of chunks) html += parseFn(chunk);
    return html;
  }

  return { parse, split, MIN_LENGTH, TARGET };
}));
