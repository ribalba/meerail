/* meerail highlight: marks the active search term inside a rendered thread.

   The list shows you which conversations matched; the reader has to show you
   *where*. Everything here works on live text nodes rather than on HTML
   strings — the bodies are already-sanitised DOM (or an iframe document), and
   regexing their markup would happily wrap a `<mark>` around half a tag. */

App.highlight = (function () {
  // Mirrors app/routers/search.py: keyword = AND of case-insensitive
  // substrings, regex = the pattern itself. Postgres POSIX and JS RegExp
  // disagree on the exotic corners; a pattern only one of them accepts costs a
  // missing highlight, never a wrong search result.
  function patterns(q, mode) {
    q = (q || "").trim();
    if (!q) return [];
    try {
      if (mode === "regex") return [new RegExp(q, "gi")];
      return q.split(/\s+/).map((t) => new RegExp(t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi"));
    } catch (_) { return []; }  // half-typed regex — nothing to mark yet
  }

  // Tags whose text is not prose: rewriting inside them either does nothing
  // visible or breaks the element outright.
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEXTAREA", "MARK"]);

  function spans(text, pats) {
    const found = [];
    for (const p of pats) {
      p.lastIndex = 0;
      let m;
      while ((m = p.exec(text)) !== null) {
        if (m[0].length === 0) { p.lastIndex++; continue; }  // zero-width regex
        found.push([m.index, m.index + m[0].length]);
      }
    }
    // Overlapping hits from different terms would nest marks; keep the first.
    found.sort((a, b) => a[0] - b[0]);
    const out = [];
    let end = -1;
    for (const s of found) {
      if (s[0] < end) continue;
      out.push(s);
      end = s[1];
    }
    return out;
  }

  /* Walks `root` and wraps every match in <mark class="hit">. Returns the
     number of hits, so the caller can tell "this message matched" from "this
     message is just part of the thread". */
  function mark(root, pats) {
    if (!root || !pats.length) return 0;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => (n.parentNode && SKIP.has(n.parentNode.nodeName)) || !n.nodeValue.trim()
        ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
    });
    // Collected before mutating: splitting text nodes underneath a live walker
    // makes it revisit the halves it just produced.
    const nodes = [];
    for (let n = walker.nextNode(); n; n = walker.nextNode()) nodes.push(n);

    let count = 0;
    const doc = root.ownerDocument || document;
    for (const node of nodes) {
      const hits = spans(node.nodeValue, pats);
      if (!hits.length) continue;
      const frag = doc.createDocumentFragment();
      let at = 0;
      for (const [start, stop] of hits) {
        if (start > at) frag.appendChild(doc.createTextNode(node.nodeValue.slice(at, start)));
        const el = doc.createElement("mark");
        el.className = "hit";
        el.textContent = node.nodeValue.slice(start, stop);
        frag.appendChild(el);
        at = stop;
        count++;
      }
      if (at < node.nodeValue.length) frag.appendChild(doc.createTextNode(node.nodeValue.slice(at)));
      node.parentNode.replaceChild(frag, node);
    }
    return count;
  }

  // The iframe bodies are srcdoc documents and inherit nothing from mail.css,
  // so they need their own copy of the one rule that matters.
  const FRAME_CSS = `mark.hit{background:#ffd84d;color:#1d1d1f;border-radius:2px;padding:0 1px}`;

  return { patterns, mark, FRAME_CSS };
})();
