/* meerail quotes: folds the history a reply carries behind a ••• button.

   Every reply in a long conversation quotes the whole conversation again, so a
   thread of ten messages is the first message ten times over, and reading down
   it means scrolling past the same text again and again. n and N jump between
   messages, which helps, but a message that is two lines of reply and four
   screens of history is still four screens of history. Gmail and Apple Mail
   both fold that history away behind a button, and this is that button.

   What gets folded is decided by two questions, asked in this order.

   1. Where does the history start? Mail clients mark it in a handful of ways
      and no two agree: Gmail and Proton wrap it in a classed <div>, Apple Mail,
      Thunderbird and meerail itself put it in a <blockquote> under an "On …
      wrote:" line, plain-text mail has `>` lines (which app.markdown.js turns
      into the same <blockquote>), and Outlook has no container at all. It draws
      a rule and a From/Sent/To/Subject block, and everything after that, down
      to the end of the mail, is the quoted message, not indented, not marked.
      The first two kinds fold the container. The Outlook kind folds everything
      from the header to the end.

   2. Is it really history? This is the question that keeps the button honest.
      The text inside the fold has to be text the conversation already shows,
      in a message above this one. That is what Apple Mail does in a
      conversation, and why it folds some replies and not others. It is also
      what makes a wrong answer to the first question harmless. A newsletter's
      blockquote, a forward of mail you never had, a reply to a thread you were
      added to late (whose history is the only copy you hold), and Outlook's
      "answers below in bold", where the new text sits inside the quote, all
      carry words that are not anywhere above, and all stay open.

   The comparison is by words, not by markup. The same message reaches the
   thread as HTML, as its text part, quoted with `>` by a plain-text client,
   rewrapped at 72 columns, or with its links spelled out, and the one thing
   all of those agree on is the run of words. So both sides are reduced to
   lower-case words, header and "wrote:" lines are dropped from both (a reply's
   own "On … wrote:" line is in no earlier message), and the words of the
   quote have to sit in runs of three that an earlier message also has, all
   but a stray few (see MAX_RUN).

   The rules below were tuned on 2026-10-26 against 400 threads (1,617
   messages) of a real mailbox. 979 of the messages fold. Every one of a
   sample of 32 folds started where the history starts, and what the folds
   hide that is not above them comes to one to three words in 36 of them,
   mostly words that markup glued together. What kept the rest open was
   mostly real: replies without a quote, answers written into the quote, and
   history not in the thread. Most of the noise that kept folds shut before
   tuning now has a rule of its own below (attachment names, wrapped
   attributions, external-mail banners, signatures under the quote).

   Only the reader's own DOM is touched. Bodies are rendered in sandboxed
   frames with no scripts in them, and the parent reaches in the same way the
   search highlighting does, so the button's listener runs out here. */

App.quotes = (function () {
  // Elements that start a new line of text. Everything else is inline and runs
  // on with its neighbours, which is how the words come out in the order a
  // reader sees them, and the header lines come out one per line.
  const BLOCK = new Set([
    "ADDRESS", "ARTICLE", "ASIDE", "BLOCKQUOTE", "CAPTION", "CENTER", "DD", "DIV",
    "DL", "DT", "FIGCAPTION", "FIGURE", "FOOTER", "H1", "H2", "H3", "H4", "H5",
    "H6", "HEADER", "HR", "LI", "MAIN", "NAV", "OL", "P", "PRE", "SECTION",
    "TABLE", "TBODY", "TD", "TFOOT", "TH", "THEAD", "TR", "UL",
  ]);
  // A button drawn before one of these would land in the table's own
  // structure, which the browser lays out somewhere other than where it was
  // put. History that starts there is left alone rather than folded oddly.
  const TABLE_PARTS = new Set(["TABLE", "TBODY", "THEAD", "TFOOT", "TR"]);

  // The containers. Gmail's and Proton's hold the "wrote:" line and the quote
  // both; Yahoo's and Zoho's likewise. A bare <blockquote> is everyone else.
  const CONTAINER = "blockquote, .gmail_quote, .protonmail_quote, .yahoo_quoted, .zmail_extra";
  // The lines a client writes above the quote: Thunderbird's own class, and
  // Gmail's (inside its container, so only ever needed to recognise the line).
  const ATTR_CLASS = /\b(?:moz-cite-prefix|gmail_attr)\b/;

  // "On Mon, 26 Oct 2026 at 08:55, Michael <m@x.de> wrote:" in the languages
  // the mail here is written in, plus the near neighbours. Anchored at the end,
  // where every one of them puts the colon, and bounded in length, so a
  // sentence that merely mentions what somebody wrote does not count.
  const ATTRIBUTION = /\b(?:wrote|writes|schrieb|schreibt|geschrieben|a écrit|escribió|ha scritto|schreef|skrev|napisał|kirjoitti)\b[^\n]{0,200}:$/i;
  // Gmail's text part wraps a long attribution: "On …, Michael <" on one line,
  // "m@x.de> wrote:" on the next. The first half is recognised by its opening.
  const ATTR_OPENING = /^(?:on|am|le|el|il|op|den|em|w dniu)\b/i;
  // One header line of a quoted message, as Outlook and the forward formats
  // write them. The label is sometimes bold, which arrives as *Label:* when a
  // plain-text client has flattened it.
  const HEADER = /^[*_]*(?:from|von|de|da|van|fra|från|od|sent|gesendet|envoyé|enviado|inviato|verzonden|date|datum|fecha|data|to|an|à|para|a|aan|till|do|cc|kopie|bcc|subject|betreff|objet|asunto|oggetto|onderwerp|ämne|temat|reply-to|antwort an)[*_]*\s*:/i;
  const FROM = /^[*_]*(?:from|von|de|da|van|fra|från|od)[*_]*\s*:/i;
  const SUBJECT = /^[*_]*(?:subject|betreff|objet|asunto|oggetto|onderwerp|ämne|temat)[*_]*\s*:/i;
  const SENT = /^[*_]*(?:sent|gesendet|envoyé|enviado|inviato|verzonden|date|datum|fecha|data)[*_]*\s*:/i;
  // The rule lines clients put above a quoted or forwarded message.
  // A few words may follow ("Original Message processed by david®").
  const SEPARATOR = /^[-_=*\s]*(?:original message|ursprüngliche nachricht|originalnachricht|forwarded message|weitergeleitete nachricht|(?:begin|end of) forwarded message|(?:anfang|ende) der weitergeleiteten nachricht|message d'origine|message transféré|mensaje original|messaggio originale|oorspronkelijk bericht)\b[^\n]{0,40}$/i;
  // The warnings a recipient's mail server writes into the top of mail from
  // outside, which come back in the quote of a message that never had them:
  // Microsoft's "You don't often get email from …", and the many "CAUTION:
  // this email originated from outside the organisation" banners.
  const BANNER = /you don'?t often get email from|learn why this is important|sie erhalten nicht häufig e-?mails von|erfahren sie,? warum dies wichtig ist|^\W*(?:caution|warning|achtung|vorsicht|attention|external|extern)\b.{0,200}\b(?:outside|external|extern|externen|außerhalb|ausserhalb)\b/i;
  const RULE = /^[-_=]{8,}$/;
  // "-- " above a signature. Some clients put the signature under the quote
  // rather than above it, and a mailing list puts its footer there.
  // GitHub's notifications draw theirs as a single dash.
  const SIG = /^(?:[—–]|[-—–_]{2,})$/;

  // Noise that differs between two renderings of the same words: a link's
  // address spelled out by one client and not another, an inline image named
  // by its content id, and the invisible characters that split a word in two.
  const URLS = /\b(?:https?|ftp|mailto):[^\s<>"')\]]+/gi;
  const CIDS = /\[cid:[^\]]*\]/gi;
  // Attachments. Apple Mail and others write the files of the message they
  // quote into the quote, as "<Screenshot 2026-05-08 at 08.00.32.png>", and in
  // the message itself those were attachments, not words.
  const EXT = "(?:pdf|png|jpe?g|gif|heic|webp|tiff?|bmp|svg|od[tsp]|docx?|xlsx?|pptx?|csv|txt|zip|7z|rar|gz|ics|vcf|eml|msg|mp3|mp4|mov|m4a|wav|key|pages|numbers|xml|json|asc|p7s|p7m|gpg|sig)";
  const FILES = new RegExp(`<[^<>]{1,200}\\.${EXT}>|\\[[^\\[\\]]{1,200}\\.${EXT}\\]|[^\\s<>\\[\\]"']+\\.${EXT}\\b`, "gi");
  const FILE_LINE = new RegExp(`^[^<>\\[\\]]{1,200}\\.${EXT}$`, "i");
  const INVISIBLE = /[\u00ad\u200b-\u200d\u2060\ufeff]/g;
  const WORD = /[\p{L}\p{N}]+/gu;

  // How strict "already in the thread" is. A word is accounted for when some
  // run of three around it is in an earlier message. Allowed through: a stray
  // word or two that one rendering has and another lacks (a footnote number,
  // a name a client spells out). Not allowed: anything that reads like a
  // sentence of its own, which is what an answer written into the quote is.
  const GRAM = 3;
  const MAX_RUN = 3;        // new words in a row
  const MAX_SHARE = 0.08;   // new words in all

  const folds = new WeakMap();    // root -> the fold drawn in it

  // --- Text, line by line -------------------------------------------------

  // The text under `root` as lines, the way it reads on screen, each with the
  // first text node it starts at. Lines are where the header and attribution
  // rules apply, and the nodes are how a line found to start the history is
  // turned back into a place in the document.
  function lines(root) {
    const out = [];
    let text = "";
    let first = null;
    const flush = () => {
      const t = text.replace(/\s+/g, " ").trim();
      if (t) out.push({ text: t, node: first });
      text = "";
      first = null;
    };
    const add = (s, node) => {
      if (!first && s.trim()) first = node;
      text += s;
    };
    (function walk(node, pre) {
      for (let n = node.firstChild; n; n = n.nextSibling) {
        if (n.nodeType === 3) {
          if (!pre) { add(n.nodeValue, n); continue; }
          // In <pre> the line breaks are the text's own.
          n.nodeValue.split("\n").forEach((part, i) => { if (i) flush(); add(part, n); });
          continue;
        }
        if (n.nodeType !== 1) continue;
        const tag = n.nodeName;
        if (tag === "BR") { flush(); continue; }
        if (tag === "SCRIPT" || tag === "STYLE") continue;
        const block = BLOCK.has(tag);
        if (block) flush();
        walk(n, pre || tag === "PRE");
        if (block) flush();
      }
    })(root, false);
    flush();
    return out;
  }

  // A line with the quote markers of plain-text mail taken off the front.
  function bare(s) {
    return s.replace(/^[\s>|]+/, "").trim();
  }

  function words(s) {
    return s.toLowerCase().replace(CIDS, " ").replace(URLS, " ").replace(FILES, " ")
      .replace(INVISIBLE, "").match(WORD) || [];
  }

  // The words of the lines that are somebody's writing: header blocks,
  // attributions and separator rules dropped. Done the same way for the quote
  // and for the messages it is compared with, so the words either side of a
  // dropped line meet up in both and the seam does not read as new text.
  function prose(list) {
    const out = [];
    for (let i = 0; i < list.length; i++) {
      const t = bare(list[i]);
      // A whole header block in one go, From down to Subject, so the lines a
      // long To or Cc list wraps onto go with it. They carry no label of their
      // own to be recognised by.
      if (FROM.test(t)) {
        const end = list.slice(i + 1, i + 9).findIndex((x) => SUBJECT.test(bare(x)));
        if (end >= 0) { i += end + 1; continue; }
      }
      // A label on a line of its own has its value on the next one, which is
      // how Thunderbird's forward header comes out of its table.
      if (HEADER.test(t) && !t.replace(HEADER, "").trim()) { i += 1; continue; }
      if (HEADER.test(t) || isAttribution(t) || SEPARATOR.test(t) || RULE.test(t)
          || FILE_LINE.test(t) || BANNER.test(t)) continue;
      // An attribution that wrapped, every line of it.
      const k = wrappedAt([t, ...list.slice(i + 1, i + 3).map(bare)], 0);
      if (k) { i += k - 1; continue; }
      for (const w of words(t)) out.push(w);
    }
    return out;
  }

  // --- What the thread already says ----------------------------------------

  // Everything a message says, as runs of three words, for the messages below
  // it to be checked against. Both of its bodies count, since the reply quoting
  // it may have been made from either: the HTML (parsed without being rendered
  // — DOMParser documents load nothing) and the text part. `flat` answers the
  // same question for a quote too short to have a run of three.
  function print(m) {
    const texts = [];
    if (m.body_html) {
      const doc = new DOMParser().parseFromString(m.body_html, "text/html");
      texts.push(lines(doc.body).map((l) => l.text));
    }
    if (m.body_text) texts.push(m.body_text.split(/\r?\n/));
    const grams = new Set();
    let flat = " ";
    for (const list of texts) {
      const w = prose(list);
      for (let i = 0; i + GRAM <= w.length; i++) grams.add(w.slice(i, i + GRAM).join(" "));
      flat += w.join(" ") + " ";
    }
    return { grams, flat };
  }

  // How much of `list` (lines of text) is not in any of `prints`: the number
  // of words, how many of them are new, and the longest run of new ones.
  function novelty(list, prints) {
    const w = prose(list);
    const n = w.length;
    if (!n) return { n, novel: 0, run: 0 };
    const seen = new Array(n).fill(false);
    if (n < GRAM) {
      const s = " " + w.join(" ") + " ";
      if (prints.some((p) => p.flat.includes(s))) seen.fill(true);
    } else {
      for (let i = 0; i + GRAM <= n; i++) {
        const g = w.slice(i, i + GRAM).join(" ");
        if (prints.some((p) => p.grams.has(g))) seen.fill(true, i, i + GRAM);
      }
    }
    let novel = 0, run = 0, cur = 0;
    for (const s of seen) {
      if (s) { cur = 0; continue; }
      novel += 1;
      cur += 1;
      if (cur > run) run = cur;
    }
    return { n, novel, run };
  }

  // --- Where the history starts ---------------------------------------------

  function hasContent(n) {
    if (n.nodeType === 3) return !!n.nodeValue.trim();
    if (n.nodeType !== 1) return false;
    if (n.nodeName === "IMG" || n.nodeName === "HR") return true;
    return !!n.textContent.trim() || !!n.querySelector("img, hr");
  }

  // The sibling before `n` that has something in it, stepping over whitespace,
  // line breaks and empty wrappers (Outlook's <div id="appendonsend"></div>,
  // a <div><br></div> spacer) — and the ones stepped over, which belong to
  // whatever the found sibling turns out to be.
  function before(n) {
    const passed = [];
    let p = n.previousSibling;
    while (p && !hasContent(p)) { passed.push(p); p = p.previousSibling; }
    return { node: p, passed };
  }

  // The line that ends just before `n`, when it is a line of text run in with
  // other things rather than an element of its own: `Thanks!<br><br>On Mon,
  // <a>Michael</a> wrote:<br><blockquote>`. Collected back to the line break
  // or block before it, so a name in a link does not split it in two.
  function lineBefore(p) {
    const out = [];
    for (let n = p; n; n = n.previousSibling) {
      if (n.nodeType === 1 && (n.nodeName === "BR" || BLOCK.has(n.nodeName))) break;
      if (n.nodeType === 1 || n.nodeType === 3) out.unshift(n);
    }
    return out;
  }

  // "On Mon, 26 Oct 2026, Michael <m@x.de> wrote:" as one line. Every client
  // dates it or names an address, and asking for one of the two keeps out the
  // sentence of your own that happens to end in "wrote:".
  function isAttribution(t) {
    return t.length <= 400 && ATTRIBUTION.test(t) && /[\d@]/.test(t);
  }

  // The same for lines of text: one attribution, or one that wrapped onto the
  // next line or two ("On …, Michael <" and "m@x.de> wrote:", and Outlook for
  // Mac's three), and nothing else. `Thanks!<br><br>On … wrote:` in one <div>
  // is not an attribution, it is a reply with one at the end, and hiding the
  // whole <div> would hide the reply.
  function attributionOf(texts) {
    if (texts.length === 1) return isAttribution(texts[0]);
    return texts.length > 1 && texts.length <= 3 && texts[0].length < 160 && ATTR_OPENING.test(texts[0])
      && isAttribution(texts.join(" "));
  }

  // How many lines from `i` on make up a wrapped attribution: 0 when none do.
  function wrappedAt(texts, i) {
    for (const k of [2, 3]) {
      if (i + k <= texts.length && attributionOf(texts.slice(i, i + k))) return k;
    }
    return 0;
  }

  // The first piece of text inside `el`, or `el` itself when that is one.
  function firstText(el) {
    if (el.nodeType === 3) return el;
    const w = el.ownerDocument.createTreeWalker(el, NodeFilter.SHOW_TEXT,
      { acceptNode: (t) => (t.nodeValue.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP) });
    return w.nextNode();
  }

  // The outermost element that begins with `node`: the line Outlook's header
  // starts on is in a <span> in a <b> in a <p> in the <div> that draws the rule
  // above it, and it is that <div> the fold has to start at.
  function outermost(node, root) {
    let a = node;
    while (a.parentNode && a.parentNode !== root && firstText(a.parentNode) === node) a = a.parentNode;
    return a;
  }

  // The line just above `start`, when it is the rule a client draws over a
  // quote: Outlook's <hr>, a line of underscores where the mail was text once,
  // or "-------- Original Message --------" above a "wrote:" line. It goes
  // into the fold too, or it would be left standing over the button.
  function ruleAbove(start) {
    const prev = before(start);
    const p = prev.node;
    if (!p || (p.nodeType === 1 && BLOCK.has(p.nodeName) && p.nodeName !== "HR"
               && lines(p).length > 1)) return null;
    const t = p.textContent.replace(/\s+/g, " ").trim();
    if (p.nodeName === "HR" || RULE.test(t) || SEPARATOR.test(t)) {
      return [p, ...prev.passed.filter((x) => x.nodeType === 1).reverse()];
    }
    return null;
  }

  // Every place the history could start, as { kind, start, nodes }: `start` is
  // where the button goes and `nodes` what it hides, text nodes included. A
  // "box" hides the container and the line above it, a "tail" hides `start` and
  // everything after it to the end of the mail.
  function candidates(root, all) {
    const out = [];
    const texts = all.map((l) => l.text);
    // Containers, outermost only: Gmail's <div> holds its own <blockquote>.
    for (const box of root.querySelectorAll(CONTAINER)) {
      const outer = box.parentNode.closest && box.parentNode.closest(CONTAINER);
      if (outer && root.contains(outer)) continue;
      const nodes = [box];
      let start = box;
      const prev = before(box);
      const p = prev.node;
      // The line above it, when that line is an attribution: an element of its
      // own (Apple Mail's <div>, Thunderbird's moz-cite-prefix, a <p> from
      // app.markdown.js), or a run of text ending in the line break above it.
      let attr = null;
      if (p && p.nodeType === 1 && BLOCK.has(p.nodeName)) {
        if (ATTR_CLASS.test(p.className || "") || attributionOf(lines(p).map((l) => l.text))) attr = [p];
      } else if (p) {
        const line = lineBefore(p);
        const text = line.map((n) => n.textContent).join("").replace(/\s+/g, " ").trim();
        if (line.length && attributionOf([text])) attr = line;
      }
      if (attr) {
        start = attr[0];
        nodes.unshift(...attr, ...prev.passed.filter((x) => x.nodeType === 1).reverse());
      }
      const rule = ruleAbove(start);
      if (rule) { start = rule[0]; nodes.unshift(...rule); }
      out.push({ kind: "box", start, nodes });
    }
    // Outlook's header, and a "wrote:" line with the quote run on beneath it
    // unmarked. Both are recognised as lines of text, wherever they sit.
    for (let i = 0; i < all.length; i++) {
      const t = all[i].text;
      // A From line with a Subject a few lines under it, or a separator rule
      // with header lines under it (meerail's own forward has no Date, and
      // Thunderbird's starts with the Subject).
      const next = all.slice(i + 1, i + 9).map((l) => l.text);
      const header = (FROM.test(t) && next.some((x) => SUBJECT.test(x)))
        || (SEPARATOR.test(t) && next.slice(0, 2).some((x) => HEADER.test(x)));
      // An attribution that wrapped is found from its first line. Its last
      // line may pass for one on its own as well, which is harmless: that
      // start lies inside the fold the first one makes.
      if (!header && !isAttribution(t) && !wrappedAt(texts, i)) continue;
      let start = outermost(all[i].node, root);
      const rule = ruleAbove(start);
      if (rule) start = rule[0];
      out.push({ kind: "tail", start, nodes: null });
    }
    return out;
  }

  // Everything from `start` to the end of `root`: `start`, its later siblings,
  // its parent's later siblings, and so on up. Whitespace between them is left
  // where it is, there being nothing in it to hide.
  function tail(start, root) {
    const out = [];
    for (let n = start; n && n !== root; n = n.parentNode) {
      for (let s = n === start ? n : n.nextSibling; s; s = s.nextSibling) {
        if (s.nodeType === 1 || (s.nodeType === 3 && s.nodeValue.trim())) out.push(s);
      }
    }
    return out;
  }

  // Where a node sits relative to the history: in it (0), before it (-1) or
  // after it (1). A tail runs to the end, so for one that is only a question
  // of which side of its start the node is on, and asked that way it costs
  // the same for a long Outlook thread as for a short one.
  function placeOf(node, cand) {
    const s = cand.start;
    if (s === node || s.contains(node)) return 0;
    const ahead = s.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING;
    if (cand.kind === "tail") return ahead ? 0 : -1;
    if (cand.nodes.some((x) => x === node || x.contains(node))) return 0;
    return ahead ? 1 : -1;
  }

  // The fold this body should have, if any, with the numbers that decided it.
  // Reads the document and changes nothing, so the decision can be looked at
  // without drawing anything.
  function inspect(root, prints) {
    const all = lines(root);
    const list = candidates(root, all);
    if (!list.length) return { why: "no quote" };
    // The first in reading order, which is where its text begins: a tail
    // found from a "wrote:" line can start on the <div> that also holds the
    // blockquote below it. When a box and a tail begin at the same words, the
    // box is the better answer, because it knows where the quote ends. Of two
    // boxes that do, the one that holds the other: iOS Mail's "wrote:" line is
    // a <blockquote> in a <div>, the quote is the <blockquote> after that
    // <div>, and the second box, which takes the <div> as its "wrote:" line,
    // is the one that covers both.
    const at = (c) => firstText(c.start) || c.start;
    const rank = (a, b) => {
      if (a.kind !== b.kind) return a.kind === "box" ? -1 : 1;
      if (a.start === b.start) return 0;
      return a.start.contains(b.start) ? -1 : b.start.contains(a.start) ? 1 : 0;
    };
    list.sort((a, b) => (at(a) === at(b) ? rank(a, b)
      : at(a).compareDocumentPosition(at(b)) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1));
    const cand = { ...list[0] };
    if (TABLE_PARTS.has(cand.start.parentNode.nodeName)) return { why: "in a table" };
    if (cand.kind === "tail") cand.nodes = tail(cand.start, root);
    // Boxes that follow with nothing between them are one quote. iOS Mail puts
    // its "wrote:" line in a <blockquote> of its own, and the quote in the
    // next one.
    else {
      for (const c of list.slice(1)) {
        if (placeOf(c.start, cand) <= 0) continue;
        if (c.kind !== "box") break;
        const between = all.some((l) => placeOf(l.node, cand) > 0
          && (l.node.compareDocumentPosition(c.start) & Node.DOCUMENT_POSITION_FOLLOWING)
          && !c.nodes.some((x) => x === l.node || x.contains(l.node)));
        if (between) break;
        cand.nodes = cand.nodes.concat(c.nodes);
      }
    }
    const own = [], inside = [], after = [];
    for (const l of all) {
      const at = placeOf(l.node, cand);
      (at < 0 ? own : at > 0 ? after : inside).push(l.text);
    }
    const stats = { kind: cand.kind, own: prose(own).length, after: prose(after).length,
                    ...novelty(inside, prints) };
    // Nothing of its own above the quote: a forward with no note, or mail that
    // answers underneath. Folding it would leave a button and nothing else.
    // Asked of the characters, not the words, so a reply that is a link or
    // an emoji counts as one.
    if (!own.some((t) => /[^\s\-—–_=*>|]/.test(t))) return { why: "nothing above", ...stats };
    // Writing after the quote is an answer underneath it, or answers in
    // between several quotes, and the quote is what it answers. Except for a
    // signature (which some clients put under the quote) and words the thread
    // already has (a mailing list's footer, under every message it sends).
    if (list.some((c) => placeOf(c.start, cand) > 0)) return { why: "text below", ...stats };
    if (stats.after > 5 && !SIG.test(bare(after[0]))) {
      const below = novelty(after, prints);
      if (below.run > MAX_RUN || below.novel > below.n * MAX_SHARE) return { why: "text below", ...stats };
    }
    if (!stats.n) return { why: "empty quote", ...stats };
    if (stats.run > MAX_RUN || stats.novel > stats.n * MAX_SHARE) return { why: "new text", ...stats };
    return { why: "fold", cand, ...stats };
  }

  // A bare text node cannot carry a class, so one that has to be hidden is put
  // in a <span> of its own first, and the <span> is what gets hidden.
  function hideable(n) {
    if (n.nodeType === 1) return n;
    const span = n.ownerDocument.createElement("span");
    n.parentNode.insertBefore(span, n);
    span.appendChild(n);
    return span;
  }

  // --- Drawing it -------------------------------------------------------------

  function setOpen(f, open) {
    f.open = open;
    for (const n of f.nodes) n.classList.toggle("mr-q-hide", !open);
    f.button.setAttribute("aria-expanded", open ? "true" : "false");
    const label = open ? "Hide quoted text" : "Show quoted text";
    f.button.title = label;
    f.button.setAttribute("aria-label", label);
  }

  /* Folds the history in `root` (a frame's body, or the plain-text view's
     <div>) when `prints` — the messages above this one — already say it.
     `open` is how it should be drawn, for a message whose history was opened
     before a redraw. `onToggle(open)` hears every press. Returns whether a
     button was drawn. */
  function fold(root, prints, open, onToggle) {
    if (!root || folds.has(root)) return folds.has(root);
    const d = inspect(root, prints || []);
    if (d.why !== "fold") return false;
    const doc = root.ownerDocument;
    const row = doc.createElement("div");
    row.className = "mr-q-row";
    row.dataset.nohit = "";
    const button = doc.createElement("button");
    button.type = "button";
    button.className = "mr-q-toggle";
    row.appendChild(button);
    d.cand.start.parentNode.insertBefore(row, d.cand.start);
    const f = { nodes: d.cand.nodes.map(hideable), button, open: false, onToggle };
    folds.set(root, f);
    setOpen(f, !!open);
    const flip = () => turn(f, !f.open);
    button.addEventListener("click", (e) => { e.preventDefault(); flip(); });
    // Ahead of the reader's own keys, which would take Space as "scroll the
    // pane": on the button it means the button.
    button.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      e.preventDefault();
      e.stopPropagation();
      flip();
    });
    return true;
  }

  // Every way a fold opens or shuts goes through here, so the reader hears
  // about all of them: the body has changed height, and the state has to
  // survive the next redraw.
  function turn(f, open) {
    setOpen(f, open);
    if (f.onToggle) f.onToggle(open);
  }

  // Opens or shuts the fold in `root`, for the keyboard. Answers with its
  // button, which is where the change happened, or null when there is none.
  function toggle(root) {
    const f = root && folds.get(root);
    if (!f) return null;
    turn(f, !f.open);
    return f.button;
  }

  // The fold `el` is hidden in, if it is hidden in one.
  function foldOf(el) {
    const doc = el.ownerDocument;
    for (let n = el; n; n = n.parentNode) {
      const f = folds.get(n);
      if (f) return !f.open && f.nodes.some((x) => x.contains(el)) ? f : null;
      if (n === doc.body) return null;
    }
    return null;
  }

  function hidden(el) { return !!foldOf(el); }

  // Opens the fold `el` is hidden in: a match the find box is about to go to
  // must not be somewhere nobody can see it. Answers whether there was one.
  function reveal(el) {
    const f = foldOf(el);
    if (!f) return false;
    turn(f, true);
    return true;
  }

  // Runs `fn` with the history in `root` showing and the button gone, then
  // puts both back: what a copy of the whole message reads its text from.
  function withHistory(root, fn) {
    const f = root && folds.get(root);
    if (!f) return fn();
    const was = f.open;
    setOpen(f, true);
    f.button.parentNode.hidden = true;
    try { return fn(); } finally {
      f.button.parentNode.hidden = false;
      setOpen(f, was);
    }
  }

  // The frames are srcdoc documents and inherit nothing from mail.css, so the
  // button brings its own look, the way the search marks do. The label is drawn
  // by CSS rather than written into the button, so a copy of the message, a
  // find in it and the word count above never see it.
  const FRAME_CSS = `.mr-q-hide{display:none!important}`
    + `.mr-q-row{margin:.5em 0}`
    + `.mr-q-toggle{display:inline-block;margin:0;padding:0 7px;height:14px;border:1px solid #d2d2d7;`
    + `border-radius:7px;background:#f0f0f3;color:#57606a;cursor:pointer;vertical-align:middle;`
    + `font:700 11px/12px -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;letter-spacing:1px}`
    + `.mr-q-toggle::before{content:"•••"}`
    + `.mr-q-toggle:hover,.mr-q-toggle:focus-visible{background:#e3e3e8;border-color:#b0b0b8}`
    + `.mr-q-toggle[aria-expanded="true"]{background:#e8f0fe;border-color:#a8c4f7;color:#1d6ff2}`
    + `.mr-q-row[hidden]{display:none}`
    // A finger needs more to aim at than a pointer does.
    + `@media (pointer:coarse){.mr-q-toggle{height:22px;padding:0 14px;border-radius:11px;line-height:20px}}`;

  return { print, fold, inspect, hidden, reveal, toggle, withHistory, FRAME_CSS };
})();
