/* meerail markdown: one parser, three consumers.

   Markdown is a *convention* inside plain text, and unless the composer is
   asked for otherwise, that plain text is the mail: the source the user typed
   goes out as text/plain, markers and all.

     toHtml(text)  — the reader. Markers are consumed: `**a**` becomes bold "a".
     editor(el)    — the composer. Markers are KEPT and dimmed: `**a**` shows
                     the asterisks alongside bold "a", so what you see is still
                     literally what the recipient receives.
     toMail(text)  — the composer again, on request. The reader's rendering,
                     packaged to be sent *as* the message. The only consumer
                     that decides what the recipient gets.

   Everything is escaped on the way in and the HTML is assembled here, so no
   message content ever reaches innerHTML unescaped. */

App.markdown = (function () {
  const esc = App.esc;

  // --- Inline spans -----------------------------------------------------
  // `keep` is what separates the two consumers: it decides whether a marker
  // is emitted as dimmed text or swallowed. Every rule must round-trip when
  // keep is on — the editor relies on textContent still equalling the source.

  function mark(s, keep) {
    return keep ? `<span class="md-mark">${esc(s)}</span>` : "";
  }

  // javascript: and data: URLs must never survive into an href.
  function safeUrl(u) {
    return /^(https?:|mailto:)/i.test(u) ? u : "#";
  }

  // A link's address, written out after its text.
  //
  // The reader is the only place a plain-text mail hides where a link goes:
  // anyone opening the same message in another client reads the URL, and so
  // does the flattened view of an HTML mail. Hiding it here is what makes
  // "click here" unjudgeable. Skipped when the text already is the address,
  // which is most links — repeating it would be noise.
  const bareUrl = (s) => String(s).replace(/^(?:https?:\/\/|mailto:)?(?:www\.)?/i, "")
    .replace(/\/+$/, "").toLowerCase();

  // The separating space lives *inside* the span, not before it. The mail
  // renderer drops this whole element (see toMail), and a space left outside it
  // would strand itself in front of whatever punctuation followed the link.
  function hrefTag(text, url) {
    if (safeUrl(url) === "#" || bareUrl(text.trim()) === bareUrl(url)) return "";
    return `<span class="md-href"> &lt;${esc(url)}&gt;</span>`;
  }

  function wrap(tag, delim, inner, keep) {
    return mark(delim, keep) + `<${tag}>` + inlineHtml(inner, keep) + `</${tag}>` + mark(delim, keep);
  }

  const RULES = [
    // Code first — nothing inside a code span is markdown.
    { re: /`([^`\n]+)`/,
      build: (m, k) => mark("`", k) + `<code class="md-code">${esc(m[1])}</code>` + mark("`", k) },
    // The trailing lookahead makes the lazy match skip a closing run that is
    // really a longer delimiter, so `**bold *and em***` closes where it should.
    { re: /\*\*(\S(?:[^\n]*?\S)?)\*\*(?!\*)/, build: (m, k) => wrap("strong", "**", m[1], k) },
    { re: /~~(\S(?:[^\n]*?\S)?)~~(?!~)/,        build: (m, k) => wrap("del", "~~", m[1], k) },
    // Emphasis needs the delimiter to sit on a word boundary, or every
    // snake_case identifier and *.txt glob in a mail turns italic.
    { re: /(?<![\w*])\*(\S(?:[^\n*]*?\S)?)\*(?![\w*])/, build: (m, k) => wrap("em", "*", m[1], k) },
    { re: /(?<![\w_])_(\S(?:[^\n_]*?\S)?)_(?![\w_])/,   build: (m, k) => wrap("em", "_", m[1], k) },
    { re: /\[([^\]\n]*)\]\(([^)\s]+)\)/,
      build: (m, k) => k
        ? mark("[", k) + `<span class="md-link">${inlineHtml(m[1], k)}</span>` + mark(`](${m[2]})`, k)
        : `<a href="${esc(safeUrl(m[2]))}" target="_blank" rel="noopener noreferrer">${inlineHtml(m[1], k)}</a>`
          + hrefTag(m[1], m[2]) },
    // Bare URLs. The trailing-character class keeps sentence punctuation out
    // of the link, which otherwise swallows the full stop after a URL.
    { re: /(?<![\w@.])(https?:\/\/[^\s<>()[\]]*[^\s<>()[\].,;:!?'"])/,
      build: (m, k) => k
        ? `<span class="md-link">${esc(m[1])}</span>`
        : `<a href="${esc(safeUrl(m[1]))}" target="_blank" rel="noopener noreferrer">${esc(m[1])}</a>` },
  ];

  function inlineHtml(src, keep) {
    let out = "", rest = String(src == null ? "" : src);
    while (rest) {
      let best = null;
      for (const rule of RULES) {
        const m = rule.re.exec(rest);
        if (m && (!best || m.index < best.m.index)) best = { rule, m };
      }
      if (!best) return out + esc(rest);
      out += esc(rest.slice(0, best.m.index)) + best.rule.build(best.m, keep);
      rest = rest.slice(best.m.index + best.m[0].length);
    }
    return out;
  }

  // --- Line classification ---------------------------------------------
  // Shared by both consumers so a line can never mean one thing while being
  // typed and another once sent.

  const RE = {
    fence:   /^\s*```/,
    heading: /^(#{1,6})(\s+)(.*)$/,
    quote:   /^(\s*>+)(\s?)(.*)$/,
    bullet:  /^(\s*)([-*+])(\s+)(.*)$/,
    ordered: /^(\s*)(\d{1,9}[.)])(\s+)(.*)$/,
    hr:      /^\s*([-*_])(?:\s*\1){2,}\s*$/,
  };

  // Which lines sit inside a ``` block. Needed as a whole-body pass because a
  // line's meaning depends on how many fences precede it.
  function fenceFlags(lines) {
    const out = [];
    let inside = false;
    for (const line of lines) {
      const delim = RE.fence.test(line);
      out.push(delim ? "delim" : inside ? "in" : "");
      if (delim) inside = !inside;
    }
    return out;
  }

  // --- Reader: markdown -> HTML ----------------------------------------

  function listHtml(items, ordered) {
    const tag = ordered ? "ol" : "ul";
    return `<${tag}>${items.map((t) => `<li>${inlineHtml(t, false)}</li>`).join("")}</${tag}>`;
  }

  function toHtml(text) {
    const lines = String(text == null ? "" : text).split(/\r?\n/);
    const flags = fenceFlags(lines);
    const out = [];
    let i = 0;

    while (i < lines.length) {
      const line = lines[i];

      if (flags[i] === "delim") {                       // fenced code block
        const body = [];
        i++;
        while (i < lines.length && flags[i] === "in") body.push(lines[i++]);
        if (i < lines.length && flags[i] === "delim") i++;
        out.push(`<pre class="md-pre"><code>${esc(body.join("\n"))}</code></pre>`);
        continue;
      }
      if (!line.trim()) { i++; continue; }
      if (RE.hr.test(line)) { out.push("<hr>"); i++; continue; }

      const h = RE.heading.exec(line);
      if (h) {
        const n = h[1].length;
        out.push(`<h${n}>${inlineHtml(h[3], false)}</h${n}>`);
        i++;
        continue;
      }

      if (RE.quote.test(line)) {                        // quoted reply blocks
        const inner = [];
        while (i < lines.length && RE.quote.test(lines[i])) {
          inner.push(RE.quote.exec(lines[i])[3]);
          i++;
        }
        out.push(`<blockquote>${toHtml(inner.join("\n"))}</blockquote>`);
        continue;
      }

      const isItem = (s) => RE.bullet.exec(s) || RE.ordered.exec(s);
      if (isItem(line)) {
        const ordered = !!RE.ordered.exec(line);
        const items = [];
        let m;
        while (i < lines.length && (m = isItem(lines[i])) && !!RE.ordered.exec(lines[i]) === ordered) {
          items.push(m[4]);
          i++;
        }
        out.push(listHtml(items, ordered));
        continue;
      }

      // Paragraph. Line breaks are preserved rather than reflowed: plain-text
      // mail is hard-wrapped, and joining those lines would run signatures and
      // addresses together.
      const para = [];
      while (i < lines.length && lines[i].trim() && flags[i] === "" &&
             !RE.hr.test(lines[i]) && !RE.heading.test(lines[i]) &&
             !RE.quote.test(lines[i]) && !isItem(lines[i])) {
        para.push(inlineHtml(lines[i], false));
        i++;
      }
      out.push(`<p>${para.join("<br>")}</p>`);
    }
    return out.join("");
  }

  // --- Mail: markdown -> the message body -------------------------------
  //
  // This becomes the message when "Send as HTML email" is on — not an
  // alternative offered next to the source. Sending both is the textbook shape
  // and was tried first; it does not survive delivery, so the composer has to
  // pick one. See _build_mime in app/routers/compose.py.
  //
  // The markup is the reader's own, so a message arrives looking like the
  // preview it was written in. Two things change once the HTML is somebody
  // else's to render:
  //
  //   * The address printed after a link goes. It is there so a plain-text
  //     reader can see where a link points before following it (see hrefTag);
  //     in HTML the link already answers that, and saying it twice reads as a
  //     mistake rather than as care.
  //   * Every rule is inlined. A mail client is under no obligation to keep a
  //     stylesheet and several drop one outright, so a class name is the one
  //     thing that certainly will not arrive. The colours are literals for the
  //     same reason, chosen against the white a mail client renders an HTML
  //     part on — the app's own custom properties mean nothing over there.
  //
  // Sizes are all em, relative to the one px value below: rem would resolve
  // against a root element that belongs to the reader's client, not to us.

  const MAIL_HEAD = "margin:1.1em 0 .45em;line-height:1.3;";
  const MAIL_MONO = "font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.88em;";

  const MAIL_STYLE = {
    p:  "margin:0 0 .8em",
    h1: MAIL_HEAD + "font-size:1.4em",
    h2: MAIL_HEAD + "font-size:1.22em",
    h3: MAIL_HEAD + "font-size:1.08em",
    h4: MAIL_HEAD + "font-size:1em",
    h5: MAIL_HEAD + "font-size:1em",
    h6: MAIL_HEAD + "font-size:1em",
    ul: "margin:0 0 .8em;padding-left:1.5em",
    ol: "margin:0 0 .8em;padding-left:1.5em",
    li: "margin:.15em 0",
    blockquote: "margin:0 0 .8em;padding-left:.8em;border-left:3px solid #d0d7de;color:#57606a",
    hr: "border:none;border-top:1px solid #d0d7de;margin:1.1em 0",
    a:  "color:#0969da",
    code: MAIL_MONO + "background:#f0f2f5;border-radius:4px;padding:.08em .3em",
    pre: "background:#f6f8fa;border:1px solid #d0d7de;border-radius:8px;" +
         "margin:0 0 .8em;padding:.6em .75em;overflow-x:auto",
  };
  // A code span inside a fence is the block's own text — it must not draw a
  // second box inside the one already around it.
  const MAIL_PRE_CODE = MAIL_MONO + "background:none;border:none;padding:0";
  // Everything the message needs sits on a wrapper div rather than on <body>:
  // several clients (Gmail among them) drop the <body> element and re-parent
  // its children, taking any styling on it along with it. A div survives that.
  //
  // The padding is the whole reason the wrapper is worth having. A mail client
  // gives an HTML part no margin of its own, so without it every line starts
  // hard against the left edge of the window — which reads as broken rather
  // than as plain.
  const MAIL_WRAP = "padding:16px 18px;color:#1f2328;font-size:14px;line-height:1.55;" +
    "word-wrap:break-word;overflow-wrap:break-word;" +
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif";

  function toMail(text) {
    const root = document.createElement("div");
    root.innerHTML = toHtml(text);
    root.querySelectorAll("span.md-href").forEach((n) => n.remove());
    for (const node of root.querySelectorAll("*")) {
      node.removeAttribute("class");
      const css = MAIL_STYLE[node.nodeName.toLowerCase()];
      if (css) node.setAttribute("style", css);
    }
    root.querySelectorAll("pre code").forEach((n) => n.setAttribute("style", MAIL_PRE_CODE));
    // Nothing follows the last block, so its bottom margin is only a gap above
    // whatever the recipient's client draws next — its quote rule, its reply
    // box — and reads as the message trailing off.
    if (root.lastElementChild) root.lastElementChild.style.marginBottom = "0";
    return `<!DOCTYPE html>\n<html><body style="margin:0">` +
      `<div style="${MAIL_WRAP}">${root.innerHTML}</div></body></html>`;
  }

  // --- Composer: live-preview editor -----------------------------------
  // The DOM is one `div.md-line` per line of source. That model is what keeps
  // this tractable: a newline is a block boundary rather than a character, so
  // typing only ever repaints the line under the caret, and reading the draft
  // back is a join over the children.

  function lineParts(text, fence) {
    if (fence === "in")    return { cls: "md-in-code", html: esc(text) };
    if (fence === "delim") return { cls: "md-fence", html: mark(text, true) };
    if (RE.hr.test(text))  return { cls: "md-hr", html: mark(text, true) };

    const h = RE.heading.exec(text);
    if (h) return { cls: `md-h md-h${h[1].length}`, html: mark(h[1], true) + esc(h[2]) + inlineHtml(h[3], true) };

    const q = RE.quote.exec(text);
    if (q) return { cls: "md-quote", html: mark(q[1], true) + esc(q[2]) + inlineHtml(q[3], true) };

    const li = RE.bullet.exec(text) || RE.ordered.exec(text);
    if (li) return { cls: "md-li", html: esc(li[1]) + mark(li[2], true) + esc(li[3]) + inlineHtml(li[4], true) };

    return { cls: "", html: inlineHtml(text, true) };
  }

  function editor(el) {
    // Non-breaking spaces are what contenteditable leaves behind for a trailing
    // space; normalise on the way out so the recipient gets a real space.
    const norm = (s) => String(s).replace(/\u00a0/g, " ");
    const lineDivs = () => Array.from(el.children);

    let hist = [{ text: "", caret: 0 }];
    let hidx = 0;
    let histTimer = null;

    function paint(div, text, fence) {
      const { cls, html } = lineParts(text, fence);
      div.className = "md-line" + (cls ? " " + cls : "");
      div.dataset.src = text;
      div.dataset.fence = fence;
      div.innerHTML = html || "<br>";     // an empty div has no height without it
    }

    // --- caret <-> character offset ---
    function closestLine(node) {
      let n = node && node.nodeType === 3 ? node.parentNode : node;
      while (n && n !== el && n.parentNode !== el) n = n.parentNode;
      return n && n.parentNode === el ? n : null;
    }

    function globalOffset(node, off) {
      const divs = lineDivs();
      if (node === el) {                                 // selection on the root
        return divs.slice(0, off).reduce((n, d) => n + norm(d.textContent).length + 1, 0);
      }
      const line = closestLine(node);
      if (!line) return -1;
      let base = 0;
      for (const d of divs) {
        if (d === line) break;
        base += norm(d.textContent).length + 1;
      }
      const r = document.createRange();
      r.selectNodeContents(line);
      r.setEnd(node, off);
      return base + norm(r.toString()).length;
    }

    function caretOffset() {
      const sel = window.getSelection();
      if (!sel || !sel.rangeCount || !el.contains(sel.anchorNode)) return -1;
      return globalOffset(sel.anchorNode, sel.anchorOffset);
    }

    function selectionRange() {
      const sel = window.getSelection();
      if (!sel || !sel.rangeCount || !el.contains(sel.anchorNode)) return null;
      const r = sel.getRangeAt(0);
      const a = globalOffset(r.startContainer, r.startOffset);
      const b = globalOffset(r.endContainer, r.endOffset);
      return a < 0 || b < 0 ? null : [Math.min(a, b), Math.max(a, b)];
    }

    function placeInLine(div, off) {
      const walk = document.createTreeWalker(div, NodeFilter.SHOW_TEXT);
      let node = null, at = 0, seen = 0, n;
      while ((n = walk.nextNode())) {
        if (seen + n.data.length >= off) { node = n; at = off - seen; break; }
        seen += n.data.length;
      }
      const r = document.createRange();
      if (node) r.setStart(node, Math.max(0, Math.min(at, node.data.length)));
      else { r.selectNodeContents(div); r.collapse(false); }
      r.collapse(true);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(r);
    }

    function placeGlobal(off) {
      const divs = lineDivs();
      for (const d of divs) {
        const len = norm(d.textContent).length;
        if (off <= len) return placeInLine(d, off);
        off -= len + 1;
      }
      const last = divs[divs.length - 1];
      if (last) placeInLine(last, norm(last.textContent).length);
    }

    // --- reading / writing the whole draft ---
    // This is the one place that decides what the user actually typed, so it
    // walks textContent rather than innerText. innerText reports the <br> that
    // gives an empty line its height as a real line break, which would turn
    // every blank line into two — and since sync() compares against
    // textContent, the two readings would disagree and edits would land at the
    // wrong offset.
    const BLOCK = /^(DIV|P|LI|UL|OL|BLOCKQUOTE|PRE|H[1-6])$/;

    function collect(node, lines) {
      for (const n of node.childNodes) {
        if (n.nodeType === 3) { lines[lines.length - 1] += norm(n.data); continue; }
        if (n.nodeType !== 1) continue;
        if (n.nodeName === "BR") {
          // Alone in a block it is only a height placeholder, not a break.
          if (n.previousSibling || n.nextSibling) lines.push("");
          continue;
        }
        if (BLOCK.test(n.nodeName) && lines[lines.length - 1] !== "") lines.push("");
        collect(n, lines);
      }
      return lines;
    }

    function readAll() {
      const lines = [];
      for (const n of el.childNodes) {
        if (n.nodeType === 3) lines.push(norm(n.data));
        else if (n.nodeName === "BR") lines.push("");
        else if (n.nodeType === 1) lines.push(...collect(n, [""]));
      }
      return lines.join("\n");
    }

    function rebuild(text, caret) {
      const lines = String(text).split(/\r?\n/);
      const flags = fenceFlags(lines);
      el.innerHTML = "";
      lines.forEach((t, i) => {
        const d = document.createElement("div");
        paint(d, t, flags[i]);
        el.appendChild(d);
      });
      el.classList.toggle("is-empty", lines.length === 1 && lines[0] === "");
      if (caret >= 0) placeGlobal(caret);
    }

    // Repaint whatever changed. The fast path touches only lines whose text or
    // fence state actually moved; the rebuild is a safety net for the times the
    // browser restructures the DOM out from under us (drag-drop, some IMEs).
    function sync() {
      const divs = lineDivs();
      const canonical = divs.length > 0 && divs.length === el.childNodes.length &&
        divs.every((d) => d.nodeName === "DIV" && !d.querySelector("div,p"));
      if (!canonical) return rebuild(readAll(), caretOffset());

      const texts = divs.map((d) => norm(d.textContent));
      const flags = fenceFlags(texts);
      const sel = window.getSelection();
      const active = sel && sel.rangeCount && el.contains(sel.anchorNode) ? closestLine(sel.anchorNode) : null;

      divs.forEach((d, i) => {
        if (d.dataset.src === texts[i] && d.dataset.fence === flags[i]) return;
        const off = d === active ? globalOffsetInLine(d) : -1;
        paint(d, texts[i], flags[i]);
        if (off >= 0) placeInLine(d, Math.min(off, texts[i].length));
      });
      el.classList.toggle("is-empty", texts.length === 1 && texts[0] === "");
    }

    function globalOffsetInLine(div) {
      const sel = window.getSelection();
      const r = document.createRange();
      r.selectNodeContents(div);
      r.setEnd(sel.anchorNode, sel.anchorOffset);
      return norm(r.toString()).length;
    }

    // Replace the selection with `str`, splitting it into lines. Enter and
    // paste both route through here so the browser never gets to invent its
    // own block structure.
    function insertText(str) {
      const range = selectionRange();
      if (!range) return;
      const text = readAll();
      const next = text.slice(0, range[0]) + str + text.slice(range[1]);
      rebuild(next, range[0] + str.length);
      record(true);
    }

    // --- undo/redo ---
    // Rewriting innerHTML on every keystroke throws away the browser's own undo
    // stack, so the composer keeps one. Without this Ctrl-Z silently does
    // nothing while writing a mail, which is worse than no live preview at all.
    function record(immediate) {
      clearTimeout(histTimer);
      const commit = () => {
        const text = readAll();
        if (text === hist[hidx].text) return;
        hist = hist.slice(0, hidx + 1);
        hist.push({ text, caret: caretOffset() });
        if (hist.length > 300) hist.shift();
        hidx = hist.length - 1;
      };
      if (immediate) commit(); else histTimer = setTimeout(commit, 250);
    }

    function undo() {
      clearTimeout(histTimer);
      const text = readAll();
      if (text !== hist[hidx].text) {                    // fold in the un-committed edit
        hist = hist.slice(0, hidx + 1);
        hist.push({ text, caret: caretOffset() });
        hidx = hist.length - 1;
      }
      if (hidx === 0) return;
      hidx--;
      rebuild(hist[hidx].text, hist[hidx].caret);
    }

    function redo() {
      if (hidx >= hist.length - 1) return;
      hidx++;
      rebuild(hist[hidx].text, hist[hidx].caret);
    }

    // --- list continuation ---
    // Enter inside a list carries the marker to the next line so a list can be
    // typed straight through. An item that is still empty ends the list
    // instead: the marker is taken away and you carry on in plain text, which
    // is the only way out that doesn't involve deleting it by hand.
    function currentItem() {
      const sel = window.getSelection();
      if (!sel || !sel.rangeCount || !el.contains(sel.anchorNode)) return null;
      const line = closestLine(sel.anchorNode);
      if (!line) return null;
      const text = norm(line.textContent);
      const ordered = RE.ordered.test(text);
      const m = ordered ? RE.ordered.exec(text) : RE.bullet.exec(text);
      if (!m) return null;
      // 1. -> 2. and 1) -> 2), keeping whichever delimiter was used.
      const marker = ordered ? (parseInt(m[2], 10) + 1) + m[2].slice(-1) : m[2];
      return { line, empty: m[4] === "", prefix: m[1] + marker + m[3] };
    }

    function replaceLine(line, text) {
      const divs = lineDivs();
      const idx = divs.indexOf(line);
      if (idx < 0) return;
      let start = 0;
      for (let i = 0; i < idx; i++) start += norm(divs[i].textContent).length + 1;
      const all = readAll();
      const end = start + norm(line.textContent).length;
      rebuild(all.slice(0, start) + text + all.slice(end), start + text.length);
      record(true);
    }

    function onEnter() {
      const item = currentItem();
      if (!item) return insertText("\n");
      if (item.empty) return replaceLine(item.line, "");
      insertText("\n" + item.prefix);
    }

    // --- wiring ---
    el.addEventListener("input", () => { sync(); record(false); });

    el.addEventListener("keydown", (e) => {
      const mod = e.metaKey || e.ctrlKey;
      if (mod && (e.key === "z" || e.key === "Z")) {
        e.preventDefault();
        e.shiftKey ? redo() : undo();
        return;
      }
      if (mod && (e.key === "y" || e.key === "Y")) { e.preventDefault(); redo(); return; }
      // Cmd/Ctrl-Enter is "send the default way" and Alt-Enter is plain send:
      // both belong to App.keys, so they must reach it unhandled.
      if (e.key === "Enter" && !mod && !e.altKey) { e.preventDefault(); onEnter(); }
    });

    el.addEventListener("paste", (e) => {
      e.preventDefault();
      insertText((e.clipboardData || window.clipboardData).getData("text/plain").replace(/\r\n?/g, "\n"));
    });

    // Clicking the padding below the last line should land the caret in the
    // draft, not do nothing.
    el.addEventListener("mousedown", (e) => {
      if (e.target !== el) return;
      e.preventDefault();
      el.focus();
      const last = el.lastElementChild;
      if (last) placeInLine(last, norm(last.textContent).length);
    });

    function setText(text) {
      rebuild(text || "", -1);
      hist = [{ text: text || "", caret: 0 }];
      hidx = 0;
    }

    setText("");

    return {
      getText: readAll,
      setText,
      // Rewrite the draft as a single undoable edit. setText() is for opening a
      // fresh draft and throws the history away with the old text; this is for
      // changing text the user is already working on, which they must be able
      // to take back with Ctrl-Z.
      replaceText(text, caret) {
        rebuild(text || "", caret >= 0 ? caret : -1);
        record(true);
      },
      focus(atEnd) {
        el.focus();
        const last = el.lastElementChild;
        if (atEnd && last) placeInLine(last, norm(last.textContent).length);
      },
    };
  }

  return { toHtml, toMail, editor, inlineHtml };
})();
