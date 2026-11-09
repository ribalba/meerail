/* meerail composefind: Ctrl/Cmd+F while writing a message.

   The key used to fall through to the browser, whose find bar searches the
   whole page, the list and the conversation behind the composer's backdrop
   included. With the keyboard on one of the window's buttons it went to the
   reader's find box instead, which sits behind the backdrop where nobody can
   see it. So here the key means "in this message", from anywhere in the window.

   It looks through the text of the message and nothing else. The query is taken
   literally, in any case, spaces included. The reader's find splits a query
   into words, because it is skimming mail for a topic. In a draft you are after
   one place in your own text, and a `"` or a `-` is a character to look for.

   The matches are drawn the way the spelling underlines are, for the reasons in
   the header of app.grammar.js. They are Ranges registered with the CSS Custom
   Highlight API, computed from offsets into the source and drawn again after
   every repaint. Nothing is ever inserted into the editor, so the text
   that is sent cannot pick up anything of ours. A browser without the API gets
   the count and the scrolling, and still lands on the match with Escape. */

App.composeFind = (function () {
  const $ = (s) => document.querySelector(s);

  const HL = (typeof Highlight === "function" && window.CSS && CSS.highlights)
    ? { all: new Highlight(), on: new Highlight() }
    : null;

  let editor = null;    // the composer's App.markdown editor
  let el = null;        // #compose-body
  let bar = null;       // #compose-find
  let input = null;     // #compose-find-input
  let hits = [];        // [start, end) pairs of offsets into the draft, in order
  let at = -1;          // which of them the find is standing on
  let anchor = 0;       // where in the draft a search starts looking
  let cameFrom = null;  // what had the keyboard when the bar opened, as {node, caret}

  // A regex rather than lower-casing both sides, which can change a string's
  // length and would put every offset after that letter in the wrong place.
  // The non-breaking spaces go the way the editor's own reading of the draft
  // sends them, or a pasted one would never match.
  function findAll() {
    const q = input.value.replace(/\xa0/g, " ");
    if (!q) return [];
    const re = new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi");
    return Array.from(editor.getText().matchAll(re), (m) => [m.index, m.index + m[0].length]);
  }

  function paintCurrent() {
    if (!HL) return;
    HL.on.clear();
    if (at < 0 || bar.hidden) return;
    const r = editor.rangeFor(hits[at][0], hits[at][1]);
    if (r) HL.on.add(r);
  }

  function paint() {
    if (!HL) return;
    HL.all.clear();
    if (!bar.hidden) for (const r of editor.rangesFor(hits)) HL.all.add(r);
    paintCurrent();
  }

  // The editor's own scrollbar and no other. scrollIntoView would also move the
  // window and the page around it to get the match to the edge it asked for.
  // A match that is already in view stays where it is, and one that is not goes
  // to the middle, so the sentence around it comes along.
  function reveal() {
    if (at < 0) return;
    const r = editor.rangeFor(hits[at][0], hits[at][1]);
    if (!r) return;
    const box = el.getBoundingClientRect();
    const spot = r.getBoundingClientRect();
    if (spot.top >= box.top && spot.bottom <= box.bottom) return;
    el.scrollTop += spot.top - box.top - el.clientHeight / 2;
  }

  // "1/12" and "0", the way the reader's find box counts.
  function renderCount() {
    const asked = !!input.value;
    bar.classList.toggle("no-hits", asked && !hits.length);
    $("#compose-find-count").textContent = !asked ? "" : hits.length ? `${at + 1}/${hits.length}` : "0";
    for (const b of bar.querySelectorAll(".find-prev, .find-next")) b.disabled = !hits.length;
  }

  // The first match at or after the anchor, round to the top if there is none
  // below it. The anchor stays put while the query is typed, so each letter
  // narrows the search from the same place instead of walking down the draft.
  function refresh(scroll) {
    hits = findAll();
    at = hits.length ? Math.max(hits.findIndex(([start]) => start >= anchor), 0) : -1;
    paint();
    renderCount();
    if (scroll) reveal();
  }

  function step(dir) {
    if (!hits.length) return;
    at = (at + dir + hits.length) % hits.length;
    anchor = hits[at][0];
    paintCurrent();
    renderCount();
    reveal();
  }

  // Called by the editor after every repaint, so the text may have changed and
  // the nodes the Ranges pointed into are gone either way. Not scrolled: the
  // user is typing in the draft, and the view belongs to them. "Next" carries
  // on from where they are writing, which is the only thing that can have moved.
  function onRender() {
    if (bar.hidden) return;
    if (document.activeElement === el) {
      const caret = editor.caret();
      if (caret >= 0) anchor = caret;
    }
    refresh(false);
  }

  // Ctrl/Cmd+F, from app.keys.js. Selects the query rather than only focusing,
  // so pressing it again types over the last one. Text selected in the draft is
  // what the key is being asked about, as long as it is one line, since the box
  // cannot hold more. The selection is read before the focus moves, because
  // with the caret in the box there is none left in the draft to ask about.
  //
  // Answers false only while Send & Ticket's question is up over the draft.
  function open() {
    if (!$("#ticket-modal").hidden) return false;
    const from = document.activeElement;
    if (from !== input) {
      const picked = from === el ? editor.selection() : null;
      const word = picked ? editor.getText().slice(picked[0], picked[1]) : "";
      if (word && !word.includes("\n")) input.value = word;
      anchor = picked ? picked[0] : 0;
      cameFrom = { node: from, caret: picked ? picked[0] : -1 };
    }
    bar.hidden = false;
    refresh(true);
    input.focus();
    input.select();
    return true;
  }

  // The bar and its highlights go together: yellow left in the draft with the
  // count that explains it gone would read as formatting. The query stays in
  // the box for the next Ctrl+F in this draft.
  function close() {
    bar.hidden = true;
    paint();
  }

  // Escape or the × from inside the bar: back to writing, standing on what was
  // found, selected so that it can be typed over or copied. With nothing found
  // the keyboard goes back to where it came from. Moved before the bar is
  // hidden, because hiding the focused field would hand it to the page, where
  // every letter is a shortcut for the conversation behind the draft.
  function dismiss() {
    const hit = hits[at];
    if (hit) {
      el.focus({ preventScroll: true });
      const r = editor.rangeFor(hit[0], hit[1]);
      // One call rather than removeAllRanges() and addRange(), for the input
      // method's sake. See placeInLine in app.markdown.js.
      if (r) window.getSelection().setBaseAndExtent(r.startContainer, r.startOffset, r.endContainer, r.endOffset);
    } else if (cameFrom && cameFrom.node !== document.body) {
      cameFrom.node.focus({ preventScroll: true });
      if (cameFrom.node === el && cameFrom.caret >= 0) editor.setCaret(cameFrom.caret);
    } else {
      el.focus({ preventScroll: true });
    }
    close();
  }

  function onKey(e) {
    if (e.isComposing || e.keyCode === 229) return;       // the input method's key
    if (e.key === "Escape") {
      e.preventDefault();         // which is also what tells App.keys the key is spent
      dismiss();
    } else if (e.key === "Enter" && !e.ctrlKey && !e.metaKey && !e.altKey) {
      // With a modifier held it is one of the send keys, and those stay App.keys's.
      e.preventDefault();
      step(e.shiftKey ? -1 : 1);
    }
  }

  // From close() and minimize() in App.compose. The next draft in the window is
  // a different text, so nothing about this search is any use to it.
  function onHide() {
    close();
    input.value = "";
    hits = [];
    at = -1;
    cameFrom = null;
    renderCount();
  }

  function init() {
    editor = App.compose.editor();
    el = $("#compose-body");
    bar = $("#compose-find");
    input = $("#compose-find-input");
    bar.querySelector(".find-icon").innerHTML = App.icon("search", 14);
    $("#compose-find-prev").innerHTML = App.icon("chevron", 14);
    $("#compose-find-next").innerHTML = App.icon("chevron", 14);
    $("#compose-find-close").innerHTML = App.icon("close", 14);
    if (HL) {
      // Over the spell checker's highlights, whose background would otherwise
      // win on the word its suggestions are open for.
      HL.all.priority = 1;
      HL.on.priority = 2;
      CSS.highlights.set("compose-find-all", HL.all);
      CSS.highlights.set("compose-find-on", HL.on);
    }
    editor.onRender(onRender);
    input.addEventListener("input", () => refresh(true));
    input.addEventListener("keydown", onKey);
    // Stopped at the bar: the composer counts these inside its window as writing
    // in the draft, and a reply that was only searched is not a draft to keep.
    // Not `drop`, which the window needs to see so a file dropped here still
    // attaches instead of the browser navigating to it.
    ["input", "change", "cut", "paste", "keyup"].forEach((t) =>
      bar.addEventListener(t, (e) => e.stopPropagation()));
    // The arrows must not take the caret out of the box. mousedown is where the
    // focus moves, and the click still arrives.
    bar.addEventListener("mousedown", (e) => {
      if (e.target.closest("button")) e.preventDefault();
    });
    $("#compose-find-prev").addEventListener("click", () => step(-1));
    $("#compose-find-next").addEventListener("click", () => step(1));
    $("#compose-find-close").addEventListener("click", dismiss);
  }

  return { init, open, close, onHide, isOpen: () => !bar.hidden };
})();
