/* meerail link peek: where a link in a message actually goes.

   Mail is the one place where the words in a link and the address behind it are
   routinely two different things, and the browser's own status bubble is no
   help: an HTML body is rendered inside a sandboxed srcdoc iframe, and the
   desktop shell is an Electron window, which has no status bubble at all. So
   the app draws its own, bottom-left of the reading pane, where a browser would
   have put it.

   Plain-text mail already writes the address out beside the link text (see
   hrefTag() in app.markdown.js) — this is the same promise kept for HTML mail,
   which is the half of the mailbox that "Unsubscribe from these emails" is
   actually sent in. */

App.linkpeek = (function () {
  // A phone has no pointer to hover with, and a tap fires mouseover on its way
  // to the click — which would leave the chip standing over a thread nobody is
  // pointing at, with no second mouseout coming to take it down. Peeking is a
  // pointer feature; where there is no pointer it stays out of the way.
  const hoverable = !window.matchMedia || window.matchMedia("(hover: hover)").matches;

  // Frame documents are mail all the way through, so every link in one counts.
  // In the app's own document only the message bodies do: the other anchors in
  // the pane are the app's own — attachment chips, which already say what they
  // are — and answering one with /api/attachments/1288 is noise, not an answer.
  const BODIES = ".msg-body-md, .msg-body-text, .msg-body-omitted";

  // Enough of a long tracking URL to judge it by. The chip ellipsises whatever
  // it cannot fit anyway, but that is a display width; this is what bounds the
  // text a `data:` href — which the sanitiser allows — can put into the
  // document on every hover.
  const MAX = 512;

  let chip = null;

  function el() {
    if (!chip) chip = document.getElementById("link-peek");
    return chip;
  }

  function span(cls, text) {
    const s = document.createElement("span");
    s.className = cls;
    s.textContent = text;
    return s;
  }

  const cut = (s) => (s.length > MAX ? s.slice(0, MAX) + "…" : s);

  /* Written the way a browser writes it: the host at full strength and
     everything around it dimmed. Which host a click lands on is the whole
     question here, and it is not always where the eye goes first —
     `apple.com.security-check.example` and `https://apple.com@example.com/`
     both read as Apple until you know which run of characters decides. */
  function fill(node, href) {
    node.textContent = "";
    let u = null;
    try { u = new URL(href); } catch (_) { /* mail carries worse than this */ }
    // mailto:, tel:, cid:, data: — no host to lift out, so they go as they are.
    if (!u || !u.host) { node.appendChild(span("lp-dim", cut(href))); return; }
    // Credentials before the @ are part of the disguise, not part of the host:
    // dimmed, and on the near side of the strong run, they read as what they
    // are rather than as the address.
    const auth = u.username ? u.username + (u.password ? ":" + u.password : "") + "@" : "";
    node.appendChild(span("lp-dim", u.protocol + "//" + auth));
    // new URL() hands back a punycoded host, which is exactly what should be
    // shown: a homograph domain that reads as apple.com turns up here spelled
    // xn--pple-43d.com.
    node.appendChild(span("lp-host", u.host));
    const rest = u.pathname + u.search + u.hash;
    if (rest && rest !== "/") node.appendChild(span("lp-dim", cut(rest)));
  }

  /* Fixed to the window rather than parked inside the pane: .reading-pane is
     the scroller, so anything positioned in it either scrolls away with the
     mail or has to be sticky — and a sticky strip in a short thread hangs under
     the last message instead of sitting at the foot of the pane. Both edges are
     measured off the pane instead of assumed: the divider moves the left one,
     and the narrow layout can leave the bottom one above the window's. */
  function place(node) {
    const pane = document.querySelector(".reading-pane");
    if (!pane) return;
    const r = pane.getBoundingClientRect();
    node.style.left = Math.max(0, r.left) + "px";
    node.style.bottom = Math.max(0, window.innerHeight - r.bottom) + "px";
    // Never wider than the pane it belongs to, whatever the divider is doing.
    node.style.maxWidth = Math.max(140, r.width - 16) + "px";
  }

  function show(href) {
    const node = el();
    if (!node) return;
    fill(node, href);
    place(node);
    node.hidden = false;
  }

  function hide() {
    const node = el();
    if (node && !node.hidden) node.hidden = true;
  }

  // What the pointer is over, if it is over a link worth reporting.
  function linkAt(target) {
    // The pointer lands on the text node inside the anchor as often as on the
    // element, and text nodes have no closest().
    const node = target && target.nodeType === 3 ? target.parentNode : target;
    const a = node && node.closest ? node.closest("a[href]") : null;
    if (!a) return null;
    if (a.ownerDocument === document && !a.closest(BODIES)) return null;
    const raw = a.getAttribute("href") || "";
    // A link into the message's own anchors goes nowhere worth reporting — and
    // under the frame's <base> it resolves against the app's own origin, so
    // reporting it would have the mail appear to point at meerail.
    if (!raw || raw.charAt(0) === "#") return null;
    return a.href || raw;
  }

  function onOver(e) {
    const href = linkAt(e.target);
    if (href) show(href); else hide();
  }

  function onOut(e) {
    // Left the document altogether. Walking out of a frame fires nothing in the
    // parent that would otherwise take the chip down — the parent's next
    // mouseover is the pointer arriving somewhere, which may be a while.
    if (!e.relatedTarget) hide();
  }

  /* Called once for the app's own document, and again for every message frame
     as it loads — a srcdoc document of its own, reachable only because the
     sandbox allows same-origin (see mountFrame() in app.reader.js). Frames are
     rebuilt on every redraw and their listeners go with them. */
  function watch(doc) {
    if (!hoverable || !doc) return;
    doc.addEventListener("mouseover", onOver);
    doc.addEventListener("mouseout", onOut);
  }

  if (hoverable) {
    watch(document);
    // The pane scrolls out from under a parked pointer, and what was under it a
    // moment ago is not what the chip is still describing. Down it goes; a
    // pixel of mouse movement brings it back for the link that is really there
    // now.
    const pane = document.querySelector(".reading-pane");
    if (pane) pane.addEventListener("scroll", hide, { passive: true });
  }

  // hide() is exported for the reader: a redraw throws away the frame the
  // pointer is sitting in, and a document that has been discarded never sends
  // the mouseout that would have cleared the chip.
  return { watch, hide };
})();
