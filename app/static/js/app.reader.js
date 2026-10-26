/* meerail reader: renders a conversation thread in the reading pane. */

App.reader = (function () {
  let currentThread = null;
  let openRequest = 0;
  let loading = 0;              // threads fetched but not yet on screen — see isBusy()
  let collapsed = new Set();    // message ids folded shut; everything else is open
  let imagesFor = new Set();    // message ids with remote images loaded
  let plainFor = new Set();     // message ids switched to their plain-text part
  let allTo = new Set();        // message ids showing their full recipient list
  let historyFor = new Set();   // message ids with their quoted history unfolded
  // What each message of the open thread says, by id, as App.quotes.print()
  // reads it: what the messages below it are checked against before their
  // history is folded. Worked out once a message is needed as one, not per draw.
  let prints = new Map();
  let dropped = new Set();      // message ids this pane took off itself — see mailArrived()
  let threadSearch = null;      // the search the open thread was fetched with
  let arrivalTimer = null;      // mail landed, the open thread is due a look
  let fetchingNew = false;      // ...and that look is on its way
  let keyFocus = false;         // are the arrow keys scrolling this pane?
  // The search that led here, captured when the thread opened. Held rather than
  // read live off the search box so a rerender mid-typing keeps marking the
  // term you actually opened the conversation on.
  let marks = [];
  // ...and the find box in the action bar, which is the same idea at thread
  // scope. It wins over the search while it has a query in it, so what is lit
  // is always the last thing you asked for; emptying the box puts the search's
  // own hits back rather than leaving the thread dark.
  let findQ = "";
  let findMarks = [];
  // Is the box on screen? Only ever false below the phone breakpoint, where it
  // lives behind a button rather than on the bar — see openFind().
  let findOpen = false;
  let hits = [];    // every lit mark in the thread, in reading order
  let hitAt = -1;   // which of them the find box is standing on
  // Which messages have actually been on screen, by id. A thread opens on its
  // newest message, so everything earlier is off the top of the pane with
  // nothing to say so — this is what the "earlier messages" chip counts. It has
  // to be viewport history rather than the seen flag, which openThread sets on
  // the whole conversation the moment it opens.
  //
  // Kept across visits rather than only for the open conversation: the chip is
  // there to point out mail you scrolled past, and one you have already read
  // down through has nothing left to say the second time you walk in. See
  // loadViewed().
  let viewed = new Set();
  let renderId = 0;    // which draw a body frame belongs to — see mountFrame()
  let frames = 0;      // its frames mounted but not yet measured
  let settled = false; // ...so nothing in the pane is where it will end up yet

  // Which patterns the thread is painted with right now. Everything that marks
  // asks here rather than reading `marks`, so a message drawn mid-find — a
  // folded one opening, a body switched to plain text — comes up lit the same
  // way as the ones already on screen.
  function marksNow() { return findMarks.length ? findMarks : marks; }

  // Every action — toolbar or keyboard — applies to the newest message in the
  // conversation. That is the one you are replying to, and it keeps the single
  // toolbar honest: no hidden "which message is selected" state to guess at.
  function targetMsg() {
    const msgs = currentThread ? currentThread.messages : [];
    return msgs.length ? msgs[msgs.length - 1] : null;
  }

  // The accounts the open conversation is being read in. Usually one. The
  // unified lists fold the same thread at two of your addresses into one row,
  // and opening that row asks for it in both (see App.memberAccounts), so
  // everything that names the conversation (filing it, dropping its row,
  // fetching it again) has to name all of them. Set once in openThread from
  // the server's answer, so it does not shift as cards are taken off the pane.
  function threadAccounts() {
    return currentThread && currentThread.account_ids ? currentThread.account_ids : [];
  }

  // The other copies of a message the server folded into one card: the same
  // mail delivered to a second account. Read, flagged or filed, they go with
  // the card, or the copy nobody can see goes on saying unread in All Inboxes.
  function twinIds(m) { return (m.twins || []).map((t) => t.id); }

  // One message from each of the conversation's other accounts, for the verbs
  // the server resolves into a whole conversation from a single message (park,
  // remind, taking a reminder back). The target names its own account's half,
  // and these name the rest. Empty for a conversation in one account.
  function alsoMembers(target) {
    return ((currentThread && currentThread.members) || [])
      .filter((x) => x.account_id !== target.account_id).map((x) => x.id);
  }

  function frameDoc(html) {
    // Emails are rendered on white (most assume it), inside a script-less iframe.
    // The base href makes relative cid: image URLs resolve against the server
    // (srcdoc documents otherwise have no usable base URL); target opens links out.
    return `<!doctype html><html><head><meta charset="utf-8">
      <base href="${location.origin}/" target="_blank">
      <style>
        html,body{margin:0}
        body{background:#fff;color:#1d1d1f;padding:12px 22px;
          font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
          word-wrap:break-word;overflow-wrap:break-word;}
        img{max-width:100%;height:auto}
        a{color:#1d6ff2}
        table{max-width:100%}
        /* Outlook and gromox quote a plain-text reply as one <pre> per source
           line, so a single unwrapped 500-character paragraph asks for a 4500px
           document and fitFrame() scales the whole message down to 17% to fit
           it. body's overflow-wrap cannot reach inside white-space:pre, and of
           the wide things mail carries, <pre> is the one that can reflow without
           losing anything: a table at that width really is that shape, a quoted
           paragraph never was. Code in mail wraps instead of scrolling,
           which beats a nested scrollbar in a frame with scrolling="no". */
        pre{white-space:pre-wrap;overflow-wrap:break-word}
        blockquote{margin:0 0 0 .8em;padding-left:.8em;border-left:3px solid #d2d2d7;color:#555}
        ${App.highlight.FRAME_CSS}
        ${App.quotes.FRAME_CSS}
      </style></head><body>${html || ""}</body></html>`;
  }

  // Set for the duration of a render that should land on the newest message.
  // Bodies live in iframes that only get their real height once loaded, so the
  // scroll has to be redone as each one settles — a fresh object per render, so
  // frames left over from an earlier one fail the identity check and stay put.
  let pin = null;

  // Where that history lives between visits. Bounded to the conversations most
  // recently read, so a mailbox worked through over months cannot grow this
  // without end — falling off the list only costs you a chip you have already
  // dismissed once.
  const VIEWED_KEY = "meerail.reader.viewed";
  const VIEWED_THREADS = 300;

  // A conversation is its server thread id where it has one; a message that
  // belongs to no thread stands in with its own. Both are scoped by account,
  // which is what the ids themselves are unique within. A thread read in two
  // accounts at once is filed under the lower account id. A thread read in one
  // account keeps the key it always had, so the history saved before threads
  // could span accounts is still found.
  function viewedKey() {
    const msgs = currentThread ? currentThread.messages : [];
    if (!msgs.length) return null;
    const accounts = threadAccounts();
    return currentThread.thread_id
      ? `t${accounts.length ? Math.min(...accounts) : msgs[0].account_id}:${currentThread.thread_id}`
      : `m${msgs[0].account_id}:${msgs[0].id}`;
  }

  function viewedStore() {
    try { return JSON.parse(localStorage.getItem(VIEWED_KEY)) || {}; }
    catch { return {}; }   // private mode, or something else wrote the key
  }

  function loadViewed() {
    const key = viewedKey();
    if (!key) return new Set();
    const ids = viewedStore()[key];
    // dataset values are strings, and JSON hands back whatever went in.
    return new Set(Array.isArray(ids) ? ids.map(String) : []);
  }

  // Read back before every write, so a second tab reading a different
  // conversation does not save over the history this one has been building.
  function saveViewed() {
    const key = viewedKey();
    if (!key) return;
    const store = viewedStore();
    // Re-inserted at the end rather than updated in place: an object keeps its
    // keys in insertion order, which makes that order the least-recently-read
    // list to trim from the front of.
    delete store[key];
    store[key] = Array.from(viewed);
    const keys = Object.keys(store);
    for (const k of keys.slice(0, Math.max(0, keys.length - VIEWED_THREADS))) delete store[k];
    try { localStorage.setItem(VIEWED_KEY, JSON.stringify(store)); }
    catch { /* private mode, or the quota is up */ }
  }

  // How much of the pane's top edge the sticky bars are covering. Both stick to
  // it on a narrow layout, so what anything scrolled to the top has to clear is
  // however much of them is actually up — offsetHeight is 0 for the nav at
  // desktop widths, where it is display:none.
  function stuckTop() {
    const bar = document.getElementById("reader-bar");
    const nav = document.getElementById("mobile-nav");
    return (bar ? bar.offsetHeight : 0) + (nav ? nav.offsetHeight : 0);
  }

  // Brings a message to the top of the pane, clear of those bars.
  function scrollToMsg(el, smooth) {
    const pane = document.querySelector(".reading-pane");
    if (!pane || !el) return;
    const top = el.getBoundingClientRect().top - pane.getBoundingClientRect().top;
    const to = Math.max(0, pane.scrollTop + top - stuckTop() - 10);
    if (smooth) pane.scrollTo({ top: to, behavior: "smooth" });
    else pane.scrollTop = to;
  }

  // Where an opening thread lands: the first message carrying a search hit
  // when you got here from a search, the newest one otherwise. Re-derived on
  // every call rather than resolved once, because iframe bodies only report
  // their hits as they load — each load nudges the target forward.
  function landOn() {
    const host = document.getElementById("reader-content");
    if (!host) return;
    scrollToMsg((marksNow().length && host.querySelector(".thread-msg.has-hit"))
      || host.lastElementChild, false);
  }

  // The messages currently drawn, oldest first.
  function rows() {
    return currentThread
      ? Array.from(document.querySelectorAll("#reader-content .thread-msg")) : [];
  }

  // Nothing in the pane is where it will end up until the last body frame has
  // been measured — an unloaded iframe is a couple of hundred pixels of a
  // message that turns out to be ten screens. Counting what is "on screen"
  // before then would mark half the thread looked at, so the tally starts here.
  function settle() {
    settled = true;
    updateEarlier();
    // The bodies mark themselves as they load, so until the last frame is up
    // the find box has been counting a thread it could only half see.
    if (findQ) { collectHits(); renderFind(); }
  }

  // The chip that floats under the action bar: how many messages sit above
  // anything you have actually had on screen. Recomputed on every scroll —
  // cheap, and scrolling is the only thing that changes the answer.
  //
  // Counted rather than merely flagged because the number is the point: a
  // conversation that opens on its last message looks the same whether there is
  // one reply above it or twenty.
  function updateEarlier() {
    const rail = document.getElementById("earlier-rail");
    const pane = document.querySelector(".reading-pane");
    if (!rail || !pane) return;
    const msgs = rows();
    // Below the fold is only worth saying with a fold to be below.
    if (!settled || msgs.length < 2) { rail.hidden = true; return; }
    const stuck = stuckTop();
    rail.style.top = stuck + "px";
    const box = pane.getBoundingClientRect();
    const top = box.top + stuck;
    // Any part of a message showing counts as having seen it — a long one is
    // read by scrolling through it, not by having it fit.
    let first = msgs.length;
    let grew = false;
    msgs.forEach((row, i) => {
      const r = row.getBoundingClientRect();
      if (r.bottom <= top || r.top >= box.bottom) return;
      if (!viewed.has(row.dataset.mid)) { viewed.add(row.dataset.mid); grew = true; }
      if (i < first) first = i;
    });
    // Only on the scrolls that actually turned something up — which is at most
    // once per message, however far the thread is scrolled back and forth.
    if (grew) saveViewed();
    // Everything above what is on screen that has never been on screen. Scrolled
    // back down past mail you have already read, the chip stays away.
    const missed = msgs.slice(0, first).filter((r) => !viewed.has(r.dataset.mid));
    rail.hidden = missed.length === 0;
    if (rail.hidden) return;
    const pill = document.getElementById("earlier-pill");
    pill.textContent = `↑ ${missed.length} earlier message${missed.length === 1 ? "" : "s"}`;
    pill.title = "Jump to the oldest message you have not seen yet";
  }

  // Sizes a loaded body frame to its document.
  //
  // Mail laid out for a desktop window does not reflow: a 600px table is still
  // 600px in a 360px-wide frame, and `scrolling="no"` clips that overflow
  // rather than offering a scrollbar — the right-hand third of the message
  // simply is not there. So give the frame the width its document actually
  // wants and scale the whole thing down to fit, which is the one option that
  // loses nothing off the edge. On a wide enough pane the scale is 1 and this
  // is just the height measurement it always was.
  function fitFrame(frame, doc) {
    const avail = frame.clientWidth;                    // read before we resize it
    const want = doc.documentElement.scrollWidth;
    const scale = avail > 0 && want > avail ? avail / want : 1;

    frame.style.width = scale < 1 ? want + "px" : "";
    frame.style.transform = scale < 1 ? `scale(${scale})` : "";
    // Kept because it cannot be asked for again: the width this was measured
    // against is the one the line above just overwrote, so a second call would
    // compare the mail's width with itself, answer "it fits", and drop the
    // scaling. refitFrame() and the find box's hitTop() read it back instead.
    frame.dataset.scale = String(scale);
    // Height comes after the width, so it is measured against the reflowed
    // document rather than the squeezed one.
    const h = doc.documentElement.scrollHeight + 4;
    frame.style.height = h + "px";
    // A transform is drawn small but laid out full size, so without this the
    // message trails a gap the height of everything the scale took off.
    frame.style.marginBottom = scale < 1 ? -h * (1 - scale) + "px" : "";
  }

  // Re-measures a frame that has already been fitted once. Only the height:
  // marking a term cannot change how wide the mail wants to be, but it can put
  // one more line on the end of a paragraph, and a frame is only as tall as it
  // was last measured — the tail of the message would simply be clipped off.
  function refitFrame(frame, doc) {
    const scale = Number(frame.dataset.scale) || 1;
    const h = doc.documentElement.scrollHeight + 4;
    frame.style.height = h + "px";
    frame.style.marginBottom = scale < 1 ? -h * (1 - scale) + "px" : "";
  }

  // Measures a frame again from the start, width and all. Unfolding a quoted
  // history can bring in mail laid out wider than the reply above it, and that
  // wants scaling down as much as it would have on the first fit. The width
  // fitFrame() overwrote has to go first, or it would measure the mail
  // against itself.
  function refitWide(frame, doc) {
    frame.style.width = "";
    frame.style.transform = "";
    frame.style.marginBottom = "";
    fitFrame(frame, doc);
  }

  // How many of the marks in `root` are on screen. One inside a folded
  // history is a match nobody can see, and the same words are on screen in
  // the message it was quoted from, which is where a search should land.
  function shown(root) {
    let n = 0;
    for (const el of root.querySelectorAll("mark.hit")) if (!App.quotes.hidden(el)) n += 1;
    return n;
  }

  // `fold` puts the quoted history away (see foldHistory), and is handed a
  // way to measure the frame again for when it comes back out.
  function mountFrame(container, html, onHit, fold) {
    const p = pin;
    // Frames from an earlier draw go on loading into a document that no longer
    // holds them, so the tally they report back to is stamped with their draw.
    const r = renderId;
    frames += 1;
    const frame = document.createElement("iframe");
    frame.className = "msg-body-frame";
    frame.setAttribute("sandbox", "allow-same-origin allow-popups allow-popups-to-escape-sandbox");
    frame.scrolling = "no";
    frame.srcdoc = frameDoc(html);
    frame.addEventListener("load", () => {
      try {
        const doc = frame.contentDocument;
        // Folded before anything is measured, so the frame is fitted to the
        // height it is drawn at.
        if (fold) fold(doc.body, () => refitWide(frame, doc));
        // Marked before the height is measured, so a term that wraps a line
        // does not leave the frame short by one.
        if (App.highlight.mark(doc.body, marksNow()) && shown(doc.body) && onHit) onHit();
        fitFrame(frame, doc);
        // Once you click inside a message body the iframe owns the keyboard and
        // shortcuts would silently stop working. Forward them back out — this
        // reaches across only because the sandbox allows same-origin.
        if (App.keys) doc.addEventListener("keydown", App.keys.handle);
        // Ctrl+A's selection reaches into every body (see selectAll), so a copy
        // taken in here, and a click that starts a selection of its own, are its
        // business too. A press in here takes the keyboard to the reader.
        doc.addEventListener("copy", onCopy);
        doc.addEventListener("selectionchange", onSelectionChange);
        doc.addEventListener("pointerdown", onPress);
        // Same reach, for the reason mail needs it most: the link text in an
        // HTML body is the sender's to write, and the address behind it is not
        // visible anywhere else in the frame.
        if (App.linkpeek) App.linkpeek.watch(doc);
      } catch (_) { frame.style.height = "400px"; }
      // Only a draw that asked to land (see rerender) has a pin to compare.
      // Without the first test, null matched null, and every redraw that keeps
      // the view where it was (a flag, a fold, mail arriving) threw the pane
      // down to the newest message as each frame loaded.
      if (p && p === pin) landOn();
      if (r === renderId && --frames <= 0) settle();
    });
    container.appendChild(frame);
  }

  // One toolbar for the whole pane, in Apple Mail's order: compose, then the
  // three reply verbs, then the filing verbs. "New Message" is the only button
  // that means anything with no conversation open, so the rest go disabled
  // rather than disappearing — the bar keeps its shape as you move around.
  const BAR_BUTTONS = [
    { act: "new", icon: "edit", title: "New Message" },
    { sep: true },
    { act: "reply", icon: "reply", title: "Reply" },
    { act: "replyall", icon: "replyAll", title: "Reply All" },
    { act: "forward", icon: "forward", title: "Forward" },
    { sep: true },
    { act: "archive", icon: "archive", title: "Archive" },
    { act: "trash", icon: "trash", title: "Delete" },
    { act: "move", icon: "move", title: "Move to folder" },
    // Takes the conversation off the inbox like a filing verb, without filing
    // it anywhere a mail server would know about. The title and the lit state
    // follow which way it would go (see parkWay).
    { act: "park", icon: "parked", title: "Park" },
    // Beside the filing verbs because it is one: "remind me" files the
    // conversation away too, and differs only in coming back by itself.
    { act: "remind", icon: "bell", title: "Remind me later" },
    { act: "flag", icon: "flag", title: "Flag" },
    // Only drawn once a Meerato URL is configured — see App.tasks. An install
    // with no task tracker should not carry a button that can only fail.
    { sep: true, tasks: true },
    { act: "task", icon: "task", title: "Add Task", tasks: true },
    // Same rule, for the same reason: no model configured, no robot. See App.ai.
    { sep: true, ai: true },
    { act: "ai", icon: "robot", title: "Ask about this conversation", ai: true },
  ];

  // The bar at the top always means "the newest message". This row means "this
  // one" — it is how you reply to something halfway up a long thread without
  // the reply silently going to the last mail instead.
  function msgToolbar(m) {
    // The switch is always drawn, so the toolbar keeps one shape as you move
    // down a thread and the escape hatch is never somewhere you have to look
    // for it. It only has work to do on HTML mail, though — text mail is
    // already showing the very thing it would switch to — so there it goes
    // disabled rather than missing, the same way the top bar handles a verb
    // that does not apply.
    const on = plainFor.has(m.id);
    const off = !m.body_html;
    const hint = off ? "This message is already plain text"
      : on ? "Show the formatted message" : "Show the plain text version";
    // View source rides next to the plain-text switch: both answer "show me
    // what this message really is", one a step further than the other. It goes
    // disabled rather than missing when the original bytes were never kept or
    // have been pruned, for the same reason the switch does.
    const noSrc = !m.has_source;
    const srcHint = noSrc ? "The original message bytes are not stored"
      : "View the message source (opens in a new tab)";
    // Two groups, and which verb sits in which is the whole layout. The left
    // one is what survives a narrow pane: the three ways to answer a message,
    // and the two ways to get it out of the way. Those five are the ones you
    // reach for without looking, so they stay under the cursor at every width.
    // Everything else is in .tb-right, which folds into the ⋯ menu when the row
    // runs out of room — see fitToolbar.
    return `<div class="msg-toolbar" data-msg="${m.id}">
      <button class="tb-btn" data-act="reply" title="Reply">${App.icon("reply", 16)} Reply</button>
      <button class="tb-btn" data-act="replyall" title="Reply All">${App.icon("replyAll", 16)} Reply All</button>
      <button class="tb-btn" data-act="forward" title="Forward">${App.icon("forward", 16)} Forward</button>
      <!-- "this message", spelled out in the tooltips: these two look exactly
           like the pair in the bar above, which files the whole conversation,
           and the icon alone cannot say which of the two a click will do. -->
      <button class="tb-btn" data-act="archive" title="Archive this message"
        >${App.icon("archive", 16)}</button>
      <button class="tb-btn" data-act="trash" title="Delete this message"
        >${App.icon("trash", 16)}</button>
      <span class="tb-spacer"></span>
      <span class="tb-right">
      <!-- Icon-only, all of them, and not only to keep the row short: this
           group is what the ⋯ menu is built out of, and a button carrying its
           own label would arrive in the menu with that label inside the icon
           slot and the same words beside it. -->
      ${tasksOn() ? `<button class="tb-btn" data-act="task" title="Add Task"
        >${App.icon("task", 16)}</button>` : ""}
      ${aiOn() ? `<button class="tb-btn" data-act="ai" data-label="Ask AI about this"
        title="Ask about this conversation">${App.icon("robot", 16)}</button>` : ""}
      <button class="tb-btn${on ? " on" : ""}" data-act="plain" aria-pressed="${on}"
        title="${hint}" aria-label="${hint}"${off ? " disabled" : ""}
        >${App.icon("plaintext", 16)}</button>
      <!-- data-label is what the overflow menu calls it. The title is written
           for a tooltip, where saying that it opens a tab is worth the words;
           as a menu row it only gets ellipsised. Buttons without one are named
           by their title, which for the rest of these is already the right
           length. -->
      <button class="tb-btn" data-act="source" data-label="View the message source"
        title="${srcHint}" aria-label="${srcHint}"${noSrc ? " disabled" : ""}
        >${App.icon("code", 16)}</button>
      <button class="tb-btn ${m.flagged ? "on" : ""}" data-act="flag" title="Flag">${App.icon("flag", 16, m.flagged)}</button>
      <button class="tb-btn" data-act="move" title="Move this message to a folder"
        >${App.icon("move", 16)}</button>
      <button class="tb-btn" data-act="remind" title="Remind me later">${App.icon("bell", 16)}</button>
      <button class="tb-btn" data-act="unread" title="Mark as unread">${App.icon("markunread", 16)}</button>
      </span>
      <!-- Only drawn when the row above has run out of width — see fitToolbar.
           It holds exactly what .tb-right holds, read back off those buttons, so
           the menu cannot drift from the toolbar it stands in for. -->
      <button class="tb-btn tb-more" data-act="more" title="More actions"
        aria-label="More actions" aria-haspopup="true">${App.icon("more", 16, true)}</button>
    </div>`;
  }

  // --- Making the row fit -----------------------------------------------
  //
  // The per-message row is the widest thing in the reading pane — three labelled
  // verbs and up to ten icons — and that pane is resizable, so "too narrow" is a
  // state you drag into rather than a device size a media query could catch.
  // Left alone the buttons squashed into each other and the filing verbs ran off
  // the edge, which is what "does not break" looked like.
  //
  // Two steps out of it, taken in this order:
  //
  //   1. the labels go, leaving icons — which is what the phone layout does at
  //      the foot of the stylesheet, and what a toolbar you use every day
  //      stops needing;
  //   2. only then does .tb-right fold into the ⋯ menu.
  //
  // Losing the words costs recognition, losing a button costs a click — and a
  // row you know by heart is cheap to read by shape. So the words go first,
  // even at widths where folding the group would have saved more.
  //
  // What the row needs, in pixels, before and after step 1.
  //
  // Read with the row pinned to zero width, which is the only way to get an
  // answer out of it: the spacer between the two halves grows to fill whatever
  // is left over, so a row that fits reports its *container's* width as its
  // own — no answer at all to "does this fit?". At zero there is nothing to
  // fill, every other child is flex:0 0 auto, and scrollWidth is the sum of
  // them. scrollWidth leaves out the padding on the far side, so that is added
  // back rather than quietly costing the last button its right-hand margin.
  function widths(bar) {
    const pad = parseFloat(getComputedStyle(bar).paddingRight) || 0;
    const was = bar.style.width;
    bar.style.width = "0";
    bar.classList.remove("icons-only", "compact");
    const full = bar.scrollWidth + pad;
    bar.classList.add("icons-only");
    const icons = bar.scrollWidth + pad;
    bar.classList.remove("icons-only");
    bar.style.width = was;
    return { full, icons };
  }

  // Measured on every fit rather than cached, because the numbers are not
  // constants: crossing the phone breakpoint drops the labels from underneath,
  // and a cached desktop width would then fold a row that had plenty of room.
  // It is safe to mutate here — the observer below watches the *pane*, whose
  // width nothing in this function can change, so there is no loop to fall into
  // and every write lands in the same frame as the read, before any paint.
  function fitToolbar(bar) {
    const avail = bar.clientWidth;
    // No width yet: not laid out, or in a pane that is hidden. Deciding from
    // that would fold every row in the thread shut.
    if (!avail) return;
    const { full, icons } = widths(bar);
    const dropLabels = full > avail + 1;
    bar.classList.toggle("icons-only", dropLabels);
    // Folding is the last resort, so there is nothing to measure past it: if
    // the icons alone do not fit, the group goes whether or not that is enough.
    bar.classList.toggle("compact", dropLabels && icons > avail + 1);
  }

  function fitToolbars() {
    document.querySelectorAll("#reader-content .msg-toolbar").forEach(fitToolbar);
  }

  function tasksOn() { return !!(App.tasks && App.tasks.enabled()); }
  function aiOn() { return !!(App.ai && App.ai.enabled()); }
  function calendarOn() { return !!(App.calendar && App.calendar.enabled()); }

  // Is there anything in this attachment a model could be asked about? Text the
  // server extracted (`has_text`, decided there because only the server can see
  // it), or a picture, which goes to the provider as a picture. Everything else
  // — a zip, a signature blob, a font — gets no robot, because the only honest
  // answer it could give is "there is nothing here".
  function explainable(a) {
    if (!aiOn() || a.stored === false) return false;
    return !!a.has_text || /^image\//.test(a.content_type || "");
  }

  function renderBar() {
    const bar = document.getElementById("reader-bar");
    // The verbs only — the find box beside them is wired once and left alone,
    // or a redraw mid-search would take the caret out of it.
    const acts = document.getElementById("reader-actions");
    const m = targetMsg();
    const way = parkWay();
    acts.innerHTML = BAR_BUTTONS.filter(
      (b) => (!b.tasks || tasksOn()) && (!b.ai || aiOn())
    ).map((b) => {
      if (b.sep) return `<span class="tb-sep"></span>`;
      const flagged = b.act === "flag" && m && m.flagged;
      // Lit while the conversation is in Parked, like Flag while it is flagged.
      // Not filled, though. A filled square would swallow the P.
      const parked = b.act === "park" && way === "unpark";
      // Park has nothing to act on when no part of the conversation is in an
      // inbox (Archive, Sent), and a button that can only answer with a refusal
      // is greyed out instead.
      const off = (b.act !== "new" && !m) || (b.act === "park" && !way);
      // Delete is a different verb in Trash — there is nowhere left to file the
      // conversation, so the button destroys it — and the tooltip is where that
      // has to be said before the click rather than after it.
      const title = b.act === "trash" && inTrash() ? "Delete forever"
        : parked ? "Back to Inbox" : b.title;
      return `<button class="tb-btn${flagged || parked ? " on" : ""}" data-act="${b.act}"
        title="${title}" aria-label="${title}"${off ? " disabled" : ""}
        >${App.icon(b.icon, 18, !!flagged)}</button>`;
    }).join("");
    // The "arrows scroll here" marker rides in the bar rather than being an
    // outline around the pane: the bar is sticky and opaque, so it paints over
    // the pane's top edge, and the right edge hides under the scrollbar. It
    // leads the bar rather than trailing it — the trailing corner is the last
    // place you look while reading, and it tints the whole bar with it, since
    // a chip alone is small enough to miss when your eyes are on the message.
    bar.classList.toggle("kb-on", keyFocus);
    if (keyFocus) acts.insertAdjacentHTML("afterbegin",
      `<span class="tb-keys" title="Arrow keys scroll this thread — Esc goes back to the list"
        >↑↓<span class="tb-keys-label">scroll</span></span>`);
    // Whether there is a thread to look in is the same question the verbs above
    // just answered, so the box is drawn from here too.
    renderFind();
  }

  // --- Find in this thread ---
  //
  // The browser's own Ctrl+F is no use here: an HTML mail is rendered inside a
  // sandboxed iframe, and the page's find does not reach into one — so on a
  // thread the word is plainly in, the built-in find reports nothing. This is
  // that find, done over the same text nodes app.highlight already knows how to
  // walk, iframes included.
  //
  // It re-marks in place rather than going through rerender(). A redraw
  // remounts every body frame, which would throw the reading position and a
  // second of loading away on each keystroke.

  function findInput() { return document.getElementById("reader-find-input"); }

  // The roots renderMsg marks, and the only ones re-marking may touch. The
  // toolbars are ours and would light up on "reply"; the quoted attachment hits
  // arrive from the server pre-marked, and unmarking those would throw away the
  // only evidence the reader has that a PDF is why the thread matched.
  function markRoots(wrap) {
    const out = [];
    const head = wrap.querySelector(".msg-head");
    if (head) out.push({ root: head });
    const md = wrap.querySelector(".msg-body-md");
    if (md) out.push({ root: md });
    for (const frame of wrap.querySelectorAll(".msg-body-frame")) {
      let doc = null;
      // A frame that has not loaded yet has no body to mark — it marks itself
      // from marksNow() when it lands. Wrapped because a frame mid-navigation
      // throws rather than answering null.
      try { doc = frame.contentDocument; } catch (_) { /* not ours to read */ }
      if (doc && doc.body) out.push({ root: doc.body, frame });
    }
    return out;
  }

  // Repaints the whole open thread from marksNow(). Called when the query
  // changes — including to nothing, which is what puts the opening search's
  // hits back.
  function remark() {
    const pats = marksNow();
    for (const wrap of document.querySelectorAll("#reader-content .thread-msg")) {
      let n = 0;
      for (const { root, frame } of markRoots(wrap)) {
        App.highlight.unmark(root);
        if (App.highlight.mark(root, pats)) n += shown(root);
        // A marked term can wrap a line that used to fit, which makes the
        // document taller than the frame was measured for — so measure again.
        if (frame) refitFrame(frame, root.ownerDocument);
      }
      // Text the server quoted out of an attachment counts as a hit too, but
      // only for the search that found it: a find typed in here never looked
      // inside the PDF.
      const att = !findMarks.length && wrap.querySelector(".att-hits");
      wrap.classList.toggle("has-hit", n > 0 || !!att);
    }
  }

  // Read back off the document rather than remembered from the last marking
  // pass: a frame that finished loading, a message unfolded, a body switched to
  // plain text all add marks without going through remark().
  function collectHits() {
    hits = [];
    for (const wrap of document.querySelectorAll("#reader-content .thread-msg")) {
      for (const { root, frame } of markRoots(wrap)) {
        for (const el of root.querySelectorAll("mark.hit")) hits.push({ el, frame });
      }
    }
    // Where the marker is, asked of the marks themselves — the element it was
    // on may have been redrawn out from under us since.
    hitAt = hits.findIndex((h) => h.el.classList.contains("hit-on"));
    return hits;
  }

  // Where a hit sits in the pane's coordinates. A body frame is scaled down
  // when the mail was laid out wider than the pane (see fitFrame), and the
  // marks inside it are positioned in the document's own unscaled coordinates.
  // Points within a transformed box run from its transformed top edge at the
  // transform's scale, whatever the transform-origin happens to be, so the two
  // compose without needing to know which corner it grew from.
  function hitTop(h) {
    const top = h.el.getBoundingClientRect().top;
    if (!h.frame) return top;
    return h.frame.getBoundingClientRect().top + top * (Number(h.frame.dataset.scale) || 1);
  }

  function goToHit(i) {
    if (!hits.length) return;
    for (const h of hits) h.el.classList.remove("hit-on");
    hitAt = (i % hits.length + hits.length) % hits.length;
    const h = hits[hitAt];
    h.el.classList.add("hit-on");
    // Every match is counted, folded history included, so a find never
    // reports fewer than are there. Stepping onto one in a fold opens it, and
    // the frame is measured again (foldHistory) before the scroll is worked out.
    App.quotes.reveal(h.el);
    const p = pane();
    if (p) {
      // A little further down than scrollToMsg puts a message: a match landing
      // hard against the action bar is difficult to read, and the line above it
      // is usually the half of the sentence that says what it means.
      const top = hitTop(h) - p.getBoundingClientRect().top;
      p.scrollTo({ top: Math.max(0, p.scrollTop + top - stuckTop() - 60), behavior: "smooth" });
    }
    renderFind();
  }

  // Walks the matches, wrapping at both ends — the last one is the one before
  // the first, which is how a find box gets you back to the top of a long
  // thread without a second keystroke.
  function stepHit(dir) {
    collectHits();
    if (!hits.length) return renderFind();
    goToHit(hitAt < 0 ? (dir > 0 ? 0 : hits.length - 1) : hitAt + dir);
  }

  // The box's own state: the tally, and which of its buttons have work to do.
  function renderFind() {
    const box = document.getElementById("reader-find");
    if (!box) return;
    // Away entirely with no conversation open. The verbs beside it go disabled
    // instead, so the bar keeps its shape as you move around — but a greyed
    // text field reads as something broken rather than as something waiting.
    box.hidden = !currentThread;
    // The phone's half of it: which of the two — button or box — the bar is
    // showing. Painted here for every width, and simply ignored by the CSS
    // above the breakpoint, where both are always out.
    const toggle = document.getElementById("reader-find-toggle");
    const open = findOpen && !!currentThread;
    document.getElementById("reader-bar").classList.toggle("find-open", open);
    if (toggle) {
      toggle.hidden = !currentThread;
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    }
    box.classList.toggle("has-q", !!findQ);
    box.classList.toggle("no-hits", !!findQ && !hits.length);
    // "1/12" before you have stepped anywhere, because that is where the jump
    // on the last keystroke already put you.
    box.querySelector(".find-count").textContent =
      !findQ ? "" : hits.length ? `${Math.max(hitAt, 0) + 1}/${hits.length}` : "0";
    for (const b of box.querySelectorAll(".find-prev, .find-next")) b.disabled = !hits.length;
    box.querySelector(".find-clear").hidden = !findQ;
  }

  // The one way the query changes. Keyword rules whatever the search box is set
  // to: a regex typed into a box this small, against a thread you can see all
  // of, is not what anyone means by "find this word". The search box's
  // `-terms` do not apply either. A find box has nothing to leave out, so `-1`
  // typed here looks for the dash.
  function setFind(q) {
    findQ = (q || "").trim();
    findMarks = findQ ? App.highlight.patterns(findQ, "find") : [];
    remark();
    collectHits();
    // Straight to the first match, the way every find box does — typing is the
    // whole gesture, and asking for a second keystroke to be shown the thing
    // you just asked for reads as the box not having worked.
    if (findQ && hits.length) goToHit(0); else renderFind();
  }

  function clearFind() {
    const input = findInput();
    if (input) input.value = "";
    setFind("");
  }

  // Out from behind the button, on a phone. A no-op anywhere wider, where the
  // box is on the bar to begin with and the flag decides nothing.
  function openFind() {
    if (findOpen) return;
    findOpen = true;
    renderFind();
  }

  // ...and back behind it. The query goes with it: marks left lit under a
  // closed box, with the tally that explains them gone, read as the thread
  // having been highlighted by something else.
  function closeFind() {
    findOpen = false;
    const input = findInput();
    if (input) input.blur();
    clearFind();
  }

  // A new conversation is a new place to look, so the box does not carry the
  // last one's query into it. Straight to the state rather than through
  // setFind(), which would re-mark a thread that is about to be redrawn anyway.
  function resetFind() {
    findQ = "";
    findMarks = [];
    findOpen = false;
    hits = [];
    hitAt = -1;
    const input = findInput();
    if (input) input.value = "";
  }

  // Ctrl/Cmd+F, from app.keys.js. Answers false when the key is not ours to
  // take — no thread to look in, or the caret is in some other field, where
  // find-the-page is still what it should mean. Selects rather than merely
  // focusing, so pressing it again types over the last query the way it does
  // everywhere else.
  function focusFind() {
    const input = findInput();
    if (!currentThread || !input) return false;
    const at = document.activeElement;
    if (at && at !== input
        && (at.tagName === "INPUT" || at.tagName === "TEXTAREA" || at.isContentEditable)) return false;
    // A keyboard on a phone is a keyboard: the shortcut opens the box first
    // rather than putting the caret in something nobody can see.
    openFind();
    input.focus();
    input.select();
    return true;
  }

  // Owned here, and re-rendered from the flag, so a redraw of the bar cannot
  // drop the marker on the floor.
  function setKeyFocus(state) {
    if (keyFocus === state) return;
    keyFocus = state;
    renderBar();
  }

  // --- Select all (Ctrl/Cmd+A, from app.keys.js) ---
  //
  // The first press selects the message you are on, a second press the whole
  // conversation. It used to tick every row on the list instead, which is what
  // the key still does with the keyboard on the list. Here it is the mail you
  // are looking at that you want, mostly to copy it out.
  //
  // A selection belongs to one document, and every HTML body is a document of
  // its own, so no single range can hold a message, let alone a thread. Each
  // document gets a range of its own, and they are remembered here. The
  // browser's copy would still only take the part in the document that has the
  // focus, which is the header without the body or the body without the header.
  // So while the selection is still the one made here, a copy writes the text
  // out itself, in the From/To/Date/Subject shape app/threadtext.py gives the AI.
  let picked = null;    // { ids, ranges } of the last press, or null
  // The message the mouse last went down in, by id so that folding a card (a
  // redraw) does not lose it. Remembered rather than read off the caret, because
  // a click inside a selection leaves no caret at all in Chrome. It clears the
  // selection and puts nothing in its place.
  let pressedMid = null;

  function bodyFrames(wraps) {
    const out = [];
    for (const wrap of wraps) {
      for (const frame of wrap.querySelectorAll(".msg-body-frame")) {
        let doc = null;
        // Same caution as markRoots(), since a frame mid-navigation throws.
        try { doc = frame.contentDocument; } catch (_) { /* not ours to read */ }
        if (doc && doc.body) out.push(doc);
      }
    }
    return out;
  }

  // Compared by its boundaries rather than by asking whether anything is
  // selected, because the question is whether you have changed it since.
  function ours(entry) {
    // A frame that has been redrawn since has no window, and so no selection.
    const sel = entry.doc.defaultView ? entry.doc.getSelection() : null;
    if (!sel || sel.rangeCount !== 1) return false;
    const r = sel.getRangeAt(0);
    return r.startContainer === entry.sc && r.startOffset === entry.so
      && r.endContainer === entry.ec && r.endOffset === entry.eo;
  }

  function stillPicked() { return !!picked && picked.ranges.every(ours); }

  // A click that starts a selection of your own only clears the document it
  // lands in. The others would go on showing the rest of the thread selected,
  // so they are taken down with it. Only where the range is still ours, so the
  // selection you have just started is left alone.
  function onSelectionChange() {
    if (!picked || stillPicked()) return;
    const was = picked;
    picked = null;
    for (const entry of was.ranges) if (ours(entry)) entry.doc.getSelection().removeAllRanges();
  }

  function onCopy(e) {
    if (!stillPicked() || !e.clipboardData) return;
    e.clipboardData.setData("text/plain", copyText(picked.ids));
    e.preventDefault();
  }

  // A press inside the conversation takes the keyboard with it, the way a click
  // on a row takes it to the list (see App.keys.focus). Without it a click into
  // a mail left the arrows walking the list behind it, and Ctrl+A ticking every
  // row on it rather than selecting the mail you had just clicked into.
  //
  // A press inside a body frame arrives on the frame's own document. The frame
  // element is its place in this page, and so in a message.
  function onPress(e) {
    const doc = e.target.ownerDocument;
    const el = doc === document ? e.target : doc.defaultView && doc.defaultView.frameElement;
    const wrap = el && el.closest && el.closest(".thread-msg");
    pressedMid = wrap ? wrap.dataset.mid : null;
    if (currentThread && App.keys) App.keys.focus("reader");
  }

  function onScreen(el) {
    const p = pane();
    if (!p) return false;
    const box = p.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    return r.bottom > box.top + stuckTop() && r.top < box.bottom;
  }

  // Which message "the one you are on" is. The one you last clicked in, while
  // it is still on screen. A click in one you have since scrolled away from is
  // not where you are, and n/N and PgUp/PgDn forget it (see scrollMsg), since
  // they say where you are going in so many words.
  function msgAt() {
    const msgs = rows();
    if (!msgs.length) return null;
    const pressed = msgs.find((el) => el.dataset.mid === pressedMid);
    if (pressed && onScreen(pressed)) return pressed;
    // Failing that, the message being read. That is the one at the top of the
    // pane, on the same line scrollMsg() parks a header on. Scrolled to the end
    // it is the newest one, even when that is too short to reach the top, which
    // is how a conversation opens.
    const p = pane();
    if (p.scrollTop + p.clientHeight >= p.scrollHeight - 2) return msgs[msgs.length - 1];
    const line = p.getBoundingClientRect().top + stuckTop() + 10;
    return msgs.filter((el) => el.getBoundingClientRect().top <= line + 4).pop() || msgs[0];
  }

  // Answers false when there is no conversation, so the key goes back to the
  // browser.
  function selectAll() {
    if (!currentThread) return false;
    const all = rows();
    if (!all.length) return false;
    // A second press widens it to the thread, as long as the selection is still
    // the one the first press made and you are still on that message. Having
    // moved on to another one, n/N say, starts over there instead. A third press
    // has nowhere wider to go and keeps the thread.
    const here = msgAt();
    const wide = stillPicked() && picked.ids.includes(here.dataset.mid);
    const wraps = wide ? all : [here];
    const ranges = [];
    const put = (doc, r) => {
      const sel = doc.getSelection();
      sel.removeAllRanges();
      sel.addRange(r);
      ranges.push({ doc, sc: r.startContainer, so: r.startOffset,
                    ec: r.endContainer, eo: r.endOffset });
    };
    const top = document.createRange();
    top.setStartBefore(wraps[0]);
    top.setEndAfter(wraps[wraps.length - 1]);
    put(document, top);
    const inside = bodyFrames(wraps);
    for (const doc of inside) {
      const r = doc.createRange();
      r.selectNodeContents(doc.body);
      put(doc, r);
    }
    // Anything you had selected in another body by hand would otherwise stay lit
    // beside the message this press picked, and read as part of it.
    for (const doc of bodyFrames(all)) {
      if (!inside.includes(doc)) doc.getSelection().removeAllRanges();
    }
    picked = { ids: wraps.map((w) => w.dataset.mid), ranges };
    return true;
  }

  // What a copy of the selection writes. The body is the text as it is drawn
  // where there is a frame to read it from. Everywhere else it is body_plain as
  // the server sent it, which is the plain text the reader shows, kept with its
  // quote markers rather than read back off the rendered markdown. A folded
  // message has no body on screen and is copied whole all the same, because
  // "the whole thread" means the mail, not what happens to be unfolded.
  function bodyText(m) {
    if (m.content_status && m.content_status !== "full") {
      return "[the body of this message is not stored here]";
    }
    let text = "";
    const wrap = document.querySelector(`#reader-content .thread-msg[data-mid="${m.id}"]`);
    const frame = wrap && wrap.querySelector(".msg-body-frame");
    // Folded history is part of the mail all the same, for the same reason, so
    // it is shown for the moment the text is read.
    if (frame) {
      try {
        const body = frame.contentDocument.body;
        text = App.quotes.withHistory(body, () => body.innerText);
      } catch (_) { /* fall through */ }
    }
    if (!text.trim()) text = m.body_plain || "";
    return text.trim() || "[no text content]";
  }

  function copyText(ids) {
    const who = (name, address) => (name && address ? `${name} <${address}>` : address || name || "");
    const msgs = currentThread.messages.filter((m) => ids.includes(String(m.id)));
    return msgs.map((m, i) => {
      const head = msgs.length > 1 ? [`--- Message ${i + 1} of ${msgs.length} ---`] : [];
      head.push(`From: ${who(m.from_name, m.from_addr)}`);
      for (const [kind, label] of [["to", "To"], ["cc", "Cc"]]) {
        const list = (m.recipients[kind] || []).map((r) => who(r.name, r.address)).filter(Boolean);
        if (list.length) head.push(`${label}: ${list.join(", ")}`);
      }
      if (m.date) head.push(`Date: ${App.fmtDateFull(m.date)}`);
      head.push(`Subject: ${m.subject || "(no subject)"}`);
      const files = (m.attachments || []).map((a) => a.filename).filter(Boolean);
      if (files.length) head.push(`Attachments: ${files.join(", ")}`);
      return head.join("\n") + "\n\n" + bodyText(m);
    }).join("\n\n");
  }

  // Which folder placement a move acts on: the folder you are looking at, else
  // the inbox copy, else wherever the message happens to live.
  function sourceOf(m) {
    const selectedMailbox = App.shell && App.shell.currentMailboxId();
    const selectedLocation = m.locations.find((loc) => loc.mailbox_id === selectedMailbox);
    const inboxLocation = m.locations.find((loc) => loc.role === "inbox");
    return (selectedLocation || inboxLocation || m.locations[0] || {}).mailbox_id;
  }

  // Archive/trash act on the whole conversation — mail arrives as a thread and
  // "get this out of my way" means all of it, including the replies that only
  // live in Sent. Each message moves out of its own folder, so nothing is left
  // behind in a corner of the thread you weren't looking at.
  function moveTargets() {
    return currentThread.messages
      .map((x) => ({ m: x, source: sourceOf(x) }))
      .filter((t) => t.source);
  }

  // --- Move-to-folder menu ---
  // One menu at a time, mounted on <body> rather than inside the toolbar: the
  // toolbar is a sticky, overflow-clipped strip, so a child menu would be cut
  // off at its bottom edge.
  let openMenu = null;

  function closeMoveMenu() {
    if (!openMenu) return;
    openMenu.el.remove();
    document.removeEventListener("mousedown", openMenu.onOutside, true);
    document.removeEventListener("keydown", openMenu.onKey, true);
    document.removeEventListener("scroll", closeMoveMenu, true);
    window.removeEventListener("resize", closeMoveMenu);
    openMenu = null;
  }

  // Put a popup on <body> under `anchor` and wire the three ways out of it.
  // Shared by the move menu and the toolbar's overflow menu: both hang off a
  // button in a sticky, overflow-clipped strip, which is why neither can simply
  // be a child of the strip.
  function mountMenu(el, anchor) {
    document.body.appendChild(el);
    // Under the button, nudged back on screen when the menu would run off the
    // bottom or the right.
    const r = anchor.getBoundingClientRect();
    el.style.top = Math.min(r.bottom + 4, window.innerHeight - el.offsetHeight - 8) + "px";
    el.style.left = Math.max(8, Math.min(r.left, window.innerWidth - el.offsetWidth - 8)) + "px";

    openMenu = {
      el,
      onOutside: (e) => { if (!el.contains(e.target) && e.target !== anchor) closeMoveMenu(); },
      onKey: (e) => { if (e.key === "Escape") { e.stopPropagation(); closeMoveMenu(); } },
    };
    document.addEventListener("mousedown", openMenu.onOutside, true);
    document.addEventListener("keydown", openMenu.onKey, true);
    // Fixed positioning means the menu would otherwise sit still while the
    // reading pane scrolls out from under it.
    document.addEventListener("scroll", closeMoveMenu, true);
    window.addEventListener("resize", closeMoveMenu);
  }

  // The buttons that did not fit, as a menu. Read back off the toolbar itself
  // rather than listed here a second time: the icons, the titles, which of them
  // are disabled and which is lit all live on those buttons already, and a copy
  // of that table would be wrong the first time one of them changed. It is also
  // what gives the menu its wording for free — the plain-text switch says
  // "Show the plain text version" or "Show the formatted message" depending on
  // which way it currently is.
  function openOverflowMenu(m, anchor, one) {
    closeMoveMenu();
    const buttons = [...anchor.closest(".msg-toolbar").querySelectorAll(".tb-right .tb-btn")];
    const el = document.createElement("div");
    el.className = "move-menu";
    el.innerHTML = buttons.map((b, i) => `<button class="move-item${b.classList.contains("on") ? " on" : ""}"
        data-i="${i}"${b.disabled ? " disabled" : ""}>
        <span class="mm-icon">${b.innerHTML}</span>
        <span class="mm-name">${App.esc(b.dataset.label || b.title)}</span></button>`).join("");

    el.addEventListener("click", (e) => {
      const item = e.target.closest("[data-i]");
      if (!item) return;
      const act = buttons[Number(item.dataset.i)].dataset.act;
      closeMoveMenu();
      // Anchored to the ⋯ rather than to the button it stands for: Move and
      // Remind open menus of their own, and the button theirs would have hung
      // off is the one that is not on screen. `one` travels with it: the ⋯ is
      // only ever drawn on a message's own toolbar, and a verb reached through
      // it has to mean what it would have meant as a button.
      handleAction(act, m, anchor, one);
    });
    mountMenu(el, anchor);
  }

  // The copy of a card's mail that is filed in `accountId`: the card's own
  // message, or a twin the server folded into it. Null when the mail never
  // reached that account. Twins carry their own `locations`, so sourceOf()
  // reads them the same way it reads a message.
  function copyIn(m, accountId) {
    if (m.account_id === accountId) return m;
    return (m.twins || []).find((t) => t.account_id === accountId) || null;
  }

  function openMoveMenu(m, anchor, one) {
    closeMoveMenu();
    // Whose folders are offered. A message by itself can go to a folder of any
    // account it reached, a conversation to a folder of any account it is
    // being read in. Nearly always that is one account, and the menu is the
    // one it always was.
    let accounts = one
      ? [m.account_id, ...(m.twins || []).map((t) => t.account_id)]
      : threadAccounts();
    accounts = accounts.filter((id, i) => accounts.indexOf(id) === i);
    if (!accounts.length) accounts = [m.account_id];
    const folders = accounts.flatMap((accountId) => {
      // The folder it already sits in is not a destination; neither is a folder
      // it is already filed under, which IMAP would take but which reads as a
      // move that did nothing. Asked of the copy in that account, since that is
      // the copy a folder there would move.
      const copy = copyIn(m, accountId);
      const source = copy ? sourceOf(copy) : null;
      const here = new Set(copy ? copy.locations.map((loc) => loc.mailbox_id) : []);
      return (App.shell ? App.shell.mailboxesFor(accountId) : [])
        .filter((mb) => mb.id !== source && !here.has(mb.id))
        .map((mb) => ({ mb, accountId }));
    });

    const el = document.createElement("div");
    el.className = "move-menu";
    el.innerHTML = folders.length
      ? App.moveMenuItems(folders, accounts.length > 1)
      : `<div class="move-empty">No other folders</div>`;

    el.addEventListener("click", (e) => {
      const item = e.target.closest("[data-mailbox]");
      if (!item) return;
      const mailboxId = Number(item.dataset.mailbox);
      const accountId = Number(item.dataset.account);
      closeMoveMenu();
      if (one) moveMessageTo(m, mailboxId, accountId); else moveThreadTo(mailboxId, accountId);
    });
    mountMenu(el, anchor);
  }

  // Like archive and trash, a move takes the whole conversation with it — each
  // message leaves its own folder, so no reply is stranded behind.
  //
  // A folder belongs to one account, so in a conversation read in two of them
  // the folder picked decides which half moves: every message filed in that
  // account, whether it is a card's own copy or a twin folded into one. The
  // other account's copies stay where they are. That is deliberate, since no
  // IMAP command carries mail from one account to another. Cards with no copy
  // in the chosen account stay on the pane, and the list row is left to the
  // reload to settle, because the other half may well still be listed.
  function moveThreadTo(mailboxId, accountId) {
    try {
      const going = [];
      const targets = [];
      for (const m of currentThread.messages) {
        const copy = copyIn(m, accountId);
        const source = copy && sourceOf(copy);
        if (!source) continue;
        going.push(m);
        targets.push({ id: copy.id, source });
      }
      if (!targets.length) throw new Error("No source mailbox for this message");
      finishRemove(going, (async () => {
        for (const t of targets) await App.api.moveMsg(t.id, mailboxId, t.source);
      })(), threadAccounts().length > 1);
    } catch (e) { alert(e.message || "Move failed"); }
  }

  // The same verb from a message's own toolbar: this one message, and no source
  // folder — see removeMessage. On a card that is the same mail in two
  // accounts only the copy in the chosen folder's account moves, and the other
  // copy stays where it is, for the reason moveThreadTo gives.
  function moveMessageTo(m, mailboxId, accountId) {
    const copy = copyIn(m, accountId) || m;
    finishRemove([m], App.api.moveMsg(copy.id, mailboxId), true);
  }

  // Archive/trash the whole conversation. Split out of handleAction so the
  // composer can archive the thread it just replied to without going through a
  // toolbar button that may not be the one the user is looking at.
  //
  // One call for the conversation rather than one per message on screen: the
  // server resolves the thread fresh and empties every folder it is filed
  // under, so a message that arrived after this pane was drawn — or a second
  // placement under a label — can't hold the row in the list.
  //
  // In every account the conversation is being read in, in that one call, so
  // the keypress is one operation to undo, and a missing Archive or Trash in
  // either account refuses it before anything has moved.
  function removeThread(act) {
    const msgs = currentThread ? currentThread.messages : [];
    if (!msgs.length) return;
    const accounts = threadAccounts();
    // Only a threaded conversation has an id to act on. A message that never
    // got threaded stands alone, so its single placement is the whole job,
    // along with the copies of it in other accounts.
    if (currentThread.thread_id) {
      const threadId = currentThread.thread_id;
      return finishRemove(msgs.slice(),
        act === "archive" ? App.api.archiveThread(threadId, accounts)
                          : App.api.trashThread(threadId, accounts));
    }
    const targets = moveTargets();
    if (!targets.length) throw new Error("No source mailbox for this message");
    finishRemove(targets.map((t) => t.m), (async () => {
      for (const t of targets) {
        if (act === "archive") await App.api.archiveMsg(t.m.id, t.source, twinIds(t.m));
        else await App.api.trashMsg(t.m.id, t.source, twinIds(t.m));
      }
    })());
  }

  // Archive/trash *one* message out of the conversation — the pair of icons on
  // a message card, as against the pair in the bar above it. Trashing the
  // auto-reply that landed in the middle of a long thread is the whole point of
  // them being there, and until this they went through removeThread and put the
  // entire conversation in the Trash: on Proton that is a delete timer on every
  // mail in it, which is what github.com/ribalba/meerail/issues/19 reported.
  //
  // No source folder goes with the call. "This one, out of my way" is about the
  // mail rather than about the copy of it this pane happened to draw, and on a
  // label server that same mail is also in \All and under every label it wears
  // — leaving those behind is the delete not having happened. The server clears
  // every placement it has; see app/routers/actions.py::_message_move.
  //
  // A card that is the same mail in two accounts is filed in both, each copy
  // to its own account's Archive or Trash, for the same reason. The copy left
  // behind would be the delete not having happened in the other inbox.
  function removeMessage(act, m) {
    const also = twinIds(m);
    finishRemove([m],
      act === "archive" ? App.api.archiveMsg(m.id, null, also)
                        : App.api.trashMsg(m.id, null, also), true);
  }

  // Is the folder on screen the one Delete would otherwise file this into? The
  // same question app.bulk.js asks of the bulk bar, and answered the same way:
  // off the folder, not off the message, because a search result or the unified
  // inbox has no one folder to be standing in and Delete means Trash there.
  // Guarded, because this file's own boot draws the toolbar once and app.shell.js
  // loads after it: an unguarded read here threw at load time, which took
  // App.reader down with it and left every keyboard binding pointing at nothing.
  function inTrash() { return !!(App.shell && App.shell.currentRole() === "trash"); }

  // Delete, in Trash. The move this used to be was a move to the folder the
  // conversation was already in: the server answered "This is already in Trash"
  // and the row came back on the next refresh, which is the Delete button
  // appearing not to work with an error popup for company. The only thing left
  // for it to mean here is destroy the mail, so that is what it does — after
  // saying so, in the words of whichever kind of account this is. Imported mail
  // is gone when the rows go; mail with a server behind it is expunged from the
  // Trash the server itself is holding. See app/routers/actions.py::bulk_purge.
  //
  // `one` is a message asked for by itself, from its own toolbar: it is named
  // by itself, the rest of the conversation stays where it is, and the question
  // says which of the two is about to happen. Everything else about it — what
  // "delete" costs on this kind of account, that there is no undo — is the same
  // either way. See removeMessage for why the toolbars mean different things.
  //
  // A message by itself is that one copy and not its twins. Delete forever is
  // only offered standing in a Trash, which is one account's folder, so the
  // thread on screen was opened in that account alone and has no twins to
  // fold. Reaching past it would also be the one way a purge could land on an
  // imported copy filed somewhere other than Trash, which it destroys whole.
  function deleteForever(one) {
    const msgs = currentThread ? currentThread.messages : [];
    if (!msgs.length) return;
    const going = one ? [one] : msgs.slice();
    const accounts = one ? [one.account_id]
      : (threadAccounts().length ? threadAccounts() : [going[0].account_id]);
    // The imported wording only when every account involved is imported. A
    // mail server behind any of them makes the other sentence the true one.
    const local = (id) => {
      const acc = App.shell.accounts().find((a) => a.id === id);
      return !!(acc && acc.local);
    };
    const what = accounts.every(local)
      ? "This mail was imported, so meerail holds the only copy of it."
      : "This deletes it from the mail server.";
    const subject = one ? "this message" : "this conversation";
    if (!confirm(`Permanently delete ${subject}?\n\n${what} It cannot be undone.`)) return;
    // One item for the conversation where there is one, so a reply that landed
    // after this pane was drawn goes with it — the same rule removeThread
    // follows, and for the same reason. One per account it is being read in.
    let items;
    if (one) items = [{ account_id: one.account_id, message_id: one.id }];
    else if (currentThread.thread_id) {
      items = accounts.map((id) => ({ account_id: id, thread_id: currentThread.thread_id }));
    } else items = msgs.map((m) => ({ account_id: m.account_id, message_id: m.id }));
    finishRemove(going, App.api.bulkPurge(items), !!one);
  }

  // --- Parked -------------------------------------------------------------
  // Which way Park goes for the conversation on screen. "park" while any of it
  // sits in an inbox unparked, "unpark" while every inbox copy of it is in
  // Parked, and "" when none of it is in an inbox at all, so there is nothing
  // to park. Read off the same marks the server lists by. One unparked inbox
  // copy (a reply that has just arrived) is the conversation back in the inbox
  // as a whole. See parked_out in core/models.py.
  //
  // The copies folded into a card count too. The same mail at your second
  // address sits in that account's inbox, parked or not, and Park reaches it
  // through `also` (see parkThread), so it has a say in which way Park goes.
  function parkWay() {
    const msgs = currentThread ? currentThread.messages : [];
    let inbox = false;
    let waiting = false;
    for (const m of msgs) {
      for (const copy of [m, ...(m.twins || [])]) {
        for (const l of copy.locations || []) {
          if (l.role !== "inbox") continue;
          inbox = true;
          if (!l.parked) waiting = true;
        }
      }
    }
    return !inbox ? "" : waiting ? "park" : "unpark";
  }

  // One call for the conversation, named by its newest message. The server
  // resolves the thread itself, so a reply that landed after this pane was
  // drawn goes with it.
  //
  // Out of the list it was opened from when that list is the inbox and the
  // conversation is being parked, or Parked and it is being put back. There it
  // goes the way an archive does, before the server answers (finishRemove).
  // Anywhere else (Flagged, a search, the other way round in an inbox) the
  // row stays where it is, so the pane is redrawn from the server's answer
  // instead, the way taking a reminder back is.
  //
  // A conversation read in two accounts names one message from each of the
  // others as `also`, so both halves are parked in one operation.
  async function parkThread() {
    const way = parkWay();
    if (!way) return;
    const msgs = currentThread.messages;
    const target = msgs[msgs.length - 1];
    const call = App.api.park(target.id, way === "park", alsoMembers(target));
    if (App.shell && App.shell.parkWay() === way) return finishRemove(msgs.slice(), call);
    const threadId = currentThread.thread_id;
    const accounts = threadAccounts();
    try {
      await call;
    } catch (e) {
      return alert(e.message || "Could not park this conversation");
    }
    if (threadId) await openThread(threadId, accounts, target.id);
    else {
      for (const m of msgs) {
        for (const copy of [m, ...(m.twins || [])]) {
          for (const l of copy.locations || []) if (l.role === "inbox") l.parked = way === "park";
        }
      }
      rerender();
    }
    if (App.shell) App.shell.reloadList();
  }

  // --- "Remind me" --------------------------------------------------------
  // One call for the conversation, like archive: the server files every message
  // of the thread and every folder each of them sits in, so a reply that landed
  // after this pane was drawn goes with it rather than holding the row in the
  // list. The pane and the row move on before the server answers, for the same
  // reason they do on an archive — see finishRemove.
  function remindThread(when) {
    const msgs = currentThread ? currentThread.messages : [];
    if (!msgs.length) return;
    // The newest message: any one of the thread would do (the server resolves
    // the conversation from it), and this is the one the toolbar acts on. One
    // message from each other account it is being read in goes along, so the
    // copy at your second address is put off until the same moment.
    const target = msgs[msgs.length - 1];
    finishRemove(msgs.slice(), App.api.remind(target.id, when, alsoMembers(target)));
  }

  // Taking a reminder back, from the strip over a parked conversation.
  // Deliberately not optimistic, unlike everything above it: whether the row
  // should leave the list depends on which list is on screen — it goes from the
  // Reminders view either way, and stays put in Archive when the mail is left
  // filed — and that is a judgement the server's own answer settles for free.
  async function unremindThread(restore) {
    const msgs = currentThread ? currentThread.messages : [];
    if (!msgs.length) return;
    const target = msgs[msgs.length - 1];
    const threadId = currentThread.thread_id;
    const accounts = threadAccounts();
    try {
      await App.api.unremind(target.id, restore, alsoMembers(target));
    } catch (e) {
      return alert(e.message || "Could not change the reminder");
    }
    // Re-read rather than patched in place: bringing a conversation back moves
    // it and marks it unread, and the strip is not the only thing on screen
    // that changes.
    if (threadId) await openThread(threadId, accounts, target.id);
    else { currentThread.reminder = null; rerender(); }
    if (App.shell) App.shell.reloadList();
  }

  // --- "Send & Archive" ---------------------------------------------------
  // The composer asks for a ticket when it opens and hands it back when the
  // mail goes out, which can be a long time later: the reader moves on while a
  // reply is being written — another conversation gets selected, a draft sits
  // minimized for an hour — and archiving whatever the pane happens to show at
  // send time files the wrong mail.
  function archiveTicket() {
    const msgs = currentThread ? currentThread.messages : [];
    if (!msgs.length) return null;
    // Only for a conversation that is still in an inbox. Archiving is the verb
    // for clearing one, so behind a reply to mail that was already filed —
    // something dug back out of Archive, a thread answered from Sent, anything
    // sitting in a folder you put it in — the button offers a move that either
    // does nothing or undoes that filing. No ticket is how it stops being
    // offered: compose hides the button, and Ctrl+Enter goes back to meaning
    // plain Send. See app.compose's updateSendButtons. The copy of a mail in
    // another account counts, since it can still be sitting in that inbox.
    if (!msgs.some((m) => [m, ...(m.twins || [])]
      .some((c) => (c.locations || []).some((l) => l.role === "inbox"))))
      return null;
    // `account_ids` is every account the conversation is being read in, which
    // is what archiving it has to reach. `account_id` stays for a draft saved
    // before tickets carried the list (see ticketAccounts).
    const accounts = threadAccounts();
    return {
      thread_id: currentThread.thread_id || null,
      account_id: accounts.length ? accounts[0] : msgs[0].account_id,
      account_ids: accounts.length ? accounts.slice() : [msgs[0].account_id],
      ids: msgs.map((m) => m.id),
      // Only for a conversation that was never threaded, which has no id to act
      // on: each message leaves the folder it was in when the ticket was taken,
      // and its copies in other accounts go with it.
      targets: currentThread.thread_id ? [] :
        moveTargets().map((t) => ({ id: t.m.id, source: t.source, also: twinIds(t.m) })),
    };
  }

  // A ticket from a draft written before tickets named every account carries
  // only the one it was taken in.
  function ticketAccounts(ticket) {
    return Array.isArray(ticket.account_ids) && ticket.account_ids.length
      ? ticket.account_ids : [ticket.account_id];
  }

  // The same thread, read in at least every account the ticket names. Read in
  // fewer (the pair from All Inboxes, answered and then opened again inside
  // one account's inbox), archiving what is on screen would leave the other
  // half filed where it was, so that case goes the out-of-sight way, which
  // archives exactly what the ticket names.
  function stillShowing(ticket) {
    const msgs = currentThread ? currentThread.messages : [];
    if (!msgs.length) return false;
    if (ticket.thread_id) {
      const accounts = threadAccounts();
      return currentThread.thread_id === ticket.thread_id
        && ticketAccounts(ticket).every((id) => accounts.includes(id));
    }
    return msgs.some((m) => ticket.ids.includes(m.id));
  }

  // Still the conversation on screen: the ordinary path, which empties the pane
  // and opens the next mail down. Otherwise it is filed out of sight, and only
  // its row leaves the list.
  function archiveTicketed(ticket) {
    if (!ticket) return;
    if (stillShowing(ticket)) return removeThread("archive");
    const accounts = ticketAccounts(ticket);
    const call = ticket.thread_id
      ? App.api.archiveThread(ticket.thread_id, accounts)
      : (async () => {
        for (const t of ticket.targets) await App.api.archiveMsg(t.id, t.source, t.also);
      })();
    App.list.drop((r) => (ticket.thread_id
      ? r.thread_id === ticket.thread_id
        && App.members(r).some((m) => accounts.includes(m.account_id))
      : ticket.ids.includes(r.id)));
    call.then(() => App.shell && App.shell.reloadList())
      .catch((e) => {
        alert(e.message || "Archive failed");
        if (App.shell) App.shell.reloadList();
      });
  }

  // `one` is the scope the verb was asked for at, and only the filing verbs read
  // it: true from a message's own toolbar, meaning that message, and falsy from
  // the bar at the top and from the keyboard, meaning the conversation. The
  // rest have never had two readings — Reply has always answered the message
  // whose button was pressed, Flag has always flagged it.
  async function handleAction(act, m, anchor, one) {
    try {
      if (act === "new") return App.compose.openNew();
      if (!m) return;
      if (act === "more") return anchor && openOverflowMenu(m, anchor, one);
      if (act === "move") return anchor && openMoveMenu(m, anchor, one);
      if (act === "remind") return anchor && App.reminders.open(m, anchor);
      if (act === "park") return parkThread();
      if (act === "task") return App.tasks.open(m);
      if (act === "ai") return App.ai.openThread(m);
      if (act === "reply") return App.compose.openReply(m.id, "reply");
      if (act === "replyall") return App.compose.openReply(m.id, "replyall");
      if (act === "forward") return App.compose.openReply(m.id, "forward");
      // A new tab, like an attachment: the source of a real message runs to
      // hundreds of lines of headers and base64, which is a document to scroll
      // through rather than something to fit beside the thread.
      if (act === "source") {
        if (!m.has_source) return;
        return window.open(`/api/messages/${m.id}/source`, "_blank", "noopener");
      }
      if (act === "plain") {
        if (!m.body_html) return;   // nothing to switch away from
        if (plainFor.has(m.id)) plainFor.delete(m.id); else plainFor.add(m.id);
        return rerender();
      }
      // Both reach the copies of the same mail in other accounts (twinIds), so
      // the card and every inbox it sits in agree on what it says.
      if (act === "flag") {
        m.flagged = !m.flagged; rerender();
        await App.api.flagMsg(m.id, m.flagged, twinIds(m)); return;
      }
      if (act === "unread") { m.seen = false; await App.api.markSeen(m.id, false, twinIds(m)); return; }
      if (act === "trash" && inTrash()) return deleteForever(one ? m : null);
      if (act === "archive" || act === "trash") {
        return one ? removeMessage(act, m) : removeThread(act);
      }
    } catch (e) { alert(e.message || "Action failed"); }
  }

  // The pane, the list and the cursor all move on *before* the server is asked:
  // `call` is the request that makes it true, and against a remote server its
  // round trip is what made archive and delete feel stuck. The reload after it
  // settles reconciles the list with the truth — which, when the server said
  // no, is also what puts the rows back.
  //
  // `keptRow` is a removal that took part of a conversation rather than the
  // whole of it — one message off its own toolbar. The row it came from is
  // still a conversation with mail in it, so it stays in the list and only its
  // preview and count are briefly stale, which the reload below settles. The
  // last message going is not that case: nothing is left to open, so the row
  // goes with it.
  function finishRemove(removed, call, keptRow) {
    const threadId = currentThread.thread_id;
    // Read before clear() below takes the conversation, and its accounts, away.
    const accounts = threadAccounts().length ? threadAccounts() : [removed[0].account_id];
    const gone = new Set(removed.map((x) => x.id));
    // The copies folded into each card count as gone too. The server may
    // answer a later fetch with the other copy as the card's message, and that
    // must not bring back a card this pane just took off (see addMessages).
    const copies = new Set(gone);
    for (const x of removed) for (const id of twinIds(x)) copies.add(id);
    for (const id of copies) dropped.add(id);
    currentThread.messages = currentThread.messages.filter((x) => !gone.has(x.id));
    const emptied = !currentThread.messages.length;
    if (emptied) clear(); else rerender();
    // A list row is a conversation, so it goes by thread — its id is whichever
    // message the row was built from, not necessarily one the reader held. It
    // is the row's conversation when it has a member in any account this one
    // is being read in, since All Inboxes folds the same thread at two
    // addresses into one row (see App.members).
    if (emptied || !keptRow) {
      App.list.drop((r) => (threadId
        ? r.thread_id === threadId && App.members(r).some((m) => accounts.includes(m.account_id))
        : App.members(r).some((m) => copies.has(m.id))));
    }
    // Clearing the conversation you were reading would leave the pane blank and
    // the keyboard flow stranded. The list kept the cursor on the slot the row
    // vacated, so opening it lands on the next mail down — and on nothing at
    // all when the folder is empty, which is what draws the all-done state.
    if (emptied) App.list.openFocused();
    call.then(() => App.shell && App.shell.reloadList())
      .catch((e) => {
        alert(e.message || "Action failed");
        if (App.shell) App.shell.reloadList();
      });
  }

  // Every message in the thread is drawn in full — no "N earlier messages" to
  // unfold. (The chip of that name points up at them; it scrolls, it does not
  // unfold — see updateEarlier.) The head stays a toggle so a long quoted chain
  // can still be folded away one card at a time. The history a reply quotes is
  // a fold of its own, inside the body, and it starts shut (see foldHistory).
  // What the reader shows instead of a body for mail outside the content
  // window. The two states differ in one clause — whether the body was ever
  // here — because that is the only thing the reader can act on differently:
  // widening the window brings back mail that was never fetched, while mail
  // that was pruned is equally gone from here either way. Both end on the same
  // reassurance: nothing was deleted from the mail server.
  function contentNotice(m) {
    const months = m.content_window_months;
    const window = months
      ? `mail sent in the last ${months === 1 ? "month" : months + " months"}`
      : "recent mail only";
    const what = m.content_status === "pruned"
      ? "Its content was here and has been cleared as it aged out of that window."
      : "Its content was never downloaded.";
    return `<div class="omitted-card">
        <span class="omitted-glyph">${App.icon("clock", 20)}</span>
        <div>
          <p class="omitted-title">This message is outside the sync window</p>
          <p>meerail is set to keep ${App.esc(window)}. ${App.esc(what)}
             The headers stay, so it still lists, threads and turns up in a search
             for its subject or sender — and the full message is untouched on your
             mail server.</p>
        </div>
      </div>`;
  }

  // The folder chips for a card. A card that is the same mail in two accounts
  // says where each copy is filed, each copy's chips led by a dot in its
  // account's colour. Both copies are usually in an Inbox, and two chips
  // saying Inbox side by side would not say which inbox is which. A card with
  // no twins draws exactly the chips it always did.
  function folderChipsOf(m) {
    const copies = [m, ...(m.twins || [])];
    if (copies.length < 2) return App.folderChips(m.locations, m.account_id);
    const accounts = App.shell ? App.shell.accounts() : [];
    return copies.map((c) => {
      const chips = App.folderChips(c.locations, c.account_id);
      if (!chips) return "";
      const acc = accounts.find((a) => a.id === c.account_id);
      const name = App.accountName(c.account_id);
      return `<span class="fc-copy"><span class="fc-acct"` +
        `${name ? ` title="${App.esc(`In ${name}`)}"` : ""}` +
        ` style="background:${App.esc(acc ? acc.color : "")}"></span>${chips}</span>`;
    }).join("");
  }

  // The messages above `m` in the open thread, as App.quotes reads them.
  // Above means earlier in the thread's own order, so the oldest copy of any
  // stretch of history is the one left open, and each reply below it folds
  // its copy away.
  function historyOf(m) {
    const out = [];
    for (const x of currentThread ? currentThread.messages : []) {
      if (x.id === m.id) break;
      if (!prints.has(x.id)) prints.set(x.id, App.quotes.print(x));
      out.push(prints.get(x.id));
    }
    return out;
  }

  // Folds the quoted history in a body of `m` behind its ••• button, when the
  // messages above already say it (app.quotes.js decides). Opened stays
  // opened across redraws, the way a body switched to plain text stays plain.
  // `refit` measures a frame again, since the body has changed height. The
  // chip counting earlier messages works from heights too.
  //
  // Folding is a convenience, so mail it trips over is drawn whole rather than
  // not at all. Uncaught, an exception here would take the message down with
  // it, and on the plain-text path the rest of the thread too.
  function foldHistory(m, root, refit) {
    try {
      App.quotes.fold(root, historyOf(m), historyFor.has(m.id), (open) => {
        if (open) historyFor.add(m.id); else historyFor.delete(m.id);
        if (refit) refit();
        updateEarlier();
      });
    } catch (e) {
      console.error("quote fold failed", e);
    }
  }

  function renderMsg(m) {
    const shut = collapsed.has(m.id);
    const wrap = document.createElement("div");
    wrap.className = "thread-msg" + (shut ? " collapsed" : "");
    wrap.dataset.mid = m.id;
    const av = App.avatarColor(m.from_addr);
    const showImages = imagesFor.has(m.id);
    // Cc is part of who was addressed, so it sits next to To rather than behind
    // a details toggle — a recipient you cannot see is one you cannot decide to
    // keep on a reply. Folded, the pair is one line with whatever fits; opened,
    // every recipient wraps onto as many lines as it takes, spelled out with
    // the address, because on a wide mail "who else got this" is exactly the
    // question a display name on its own cannot answer.
    const names = (kind, full) => (m.recipients[kind] || [])
      .map((r) => App.esc(full && r.name && r.address ? `${r.name} <${r.address}>` : (r.name || r.address)))
      .join(", ");
    const people = (m.recipients.to || []).length + (m.recipients.cc || []).length;
    const detail = (full) => {
      const to = names("to", full);
      const cc = names("cc", full);
      return App.esc(m.from_addr) + (to ? " · to " + to : "") + (cc ? " · cc " + cc : "");
    };
    // A folded card shows the snippet on this line instead, so there is nothing
    // to open there.
    const expandable = !shut && people > 0;
    const openTo = expandable && allTo.has(m.id);
    const detailClass = "from-detail" + (shut ? "" : " selectable")
      + (expandable ? " expandable" : "") + (openTo ? " open" : "");
    const detailTitle = expandable
      ? ` title="${openTo ? "Hide the full recipient list" : "Show every recipient"}"` : "";
    // Collapsed, the body's first line is the whole preview. Mail outside the
    // content window has no first line, so it says why instead of nothing.
    const snippet = m.body_text ? m.body_text.slice(0, 140)
      : (m.content_status && m.content_status !== "full"
          ? "Outside the sync window — headers only" : "");
    // Which folder this message is in, left of the date — where Apple Mail puts
    // it, and dropped by CSS on a pane too narrow to carry both. Per message
    // rather than per conversation, because that is the question a thread
    // cannot answer for you: whether the mail you are looking at is the copy in
    // Trash, and whether the rest of the thread went with it. Drawn on folded
    // cards too, which is what makes it readable as a column down a long thread.
    const where = folderChipsOf(m);

    wrap.innerHTML = `
      <div class="msg-head" role="button" tabindex="0"
           aria-expanded="${shut ? "false" : "true"}"
           title="${shut ? "Expand" : "Collapse"} this message">
        <div class="from-row">
          <div class="avatar" style="background:${av}">${App.esc(App.initials(m.from_name, m.from_addr))}</div>
          <div class="from-meta">
            <div class="from-name${shut ? "" : " selectable"}">${App.esc(m.from_name || m.from_addr)}</div>
            <div class="${detailClass}"${detailTitle}>${
              shut ? App.esc(snippet) : detail(openTo)}</div>
          </div>
          <span class="msg-folders" data-nohit></span>
          <div class="msg-date-full${shut ? "" : " selectable"}">${App.esc(App.fmtDateFull(m.date))}</div>
          <span class="msg-chevron">${App.icon("chevron", 16)}</span>
        </div>
        ${shut ? "" : `<div class="thread-subject selectable">${App.esc(m.subject || "(no subject)")}</div>`}
      </div>`;
    const head = wrap.querySelector(".msg-head");
    // Participants are part of what search matched on, so the header is marked
    // too — including the collapsed snippet, which is often the only text a
    // folded message shows.
    if (App.highlight.mark(head, marksNow())) wrap.classList.add("has-hit");
    // Filled in after the marking, not before it: the folder's name is ours
    // rather than the sender's, so a search for "invoices" must not light up
    // every card filed under Invoices — nor have that card count as a message
    // the search matched. The chip carries data-nohit as well, for the find
    // box, which re-marks a head that is already holding its folders.
    head.querySelector(".msg-folders").innerHTML = where;
    const toggle = () => {
      if (collapsed.has(m.id)) collapsed.delete(m.id); else collapsed.add(m.id);
      rerender();
    };
    // Sender, recipients, date and subject are text people copy out of a thread,
    // so they select instead of folding the card. The avatar, the chevron and
    // the padding around them stay the fold target; a collapsed card has nothing
    // worth copying and toggles anywhere.
    head.addEventListener("click", (e) => {
      if (e.target.closest(".selectable")) return;
      // A drag that starts on the chevron and ends on it still leaves a
      // selection behind — releasing it should not also fold the message.
      if (!window.getSelection().isCollapsed) return;
      toggle();
    });
    head.addEventListener("keydown", (e) => {
      // Space is a global "scroll the pane" shortcut; on a focused head it
      // means the button, so it must not reach the shortcut table as well.
      if (e.key !== "Enter" && e.key !== " ") return;
      e.preventDefault(); e.stopPropagation(); toggle();
    });
    if (shut) return wrap;

    // The recipient line opens in place rather than through a rerender: a mail
    // halfway up a long thread must not drag the reading position with it when
    // you ask who else was on it.
    const detailEl = wrap.querySelector(".from-detail");
    if (expandable) {
      const swap = () => {
        const full = detailEl.classList.toggle("open");
        if (full) allTo.add(m.id); else allTo.delete(m.id);
        detailEl.innerHTML = detail(full);
        detailEl.title = full ? "Hide the full recipient list" : "Show every recipient";
        App.highlight.mark(detailEl, marksNow());
      };
      detailEl.setAttribute("role", "button");
      detailEl.setAttribute("tabindex", "0");
      detailEl.addEventListener("click", () => {
        // Selecting an address to copy it out is the other thing this line is
        // for, and a drag that ends here should not also fold it away.
        if (window.getSelection().isCollapsed) swap();
      });
      detailEl.addEventListener("keydown", (e) => {
        if (e.key !== "Enter" && e.key !== " ") return;
        // Enter on the head folds the message — this one is a nested button.
        e.preventDefault(); e.stopPropagation(); swap();
      });
    }

    wrap.insertAdjacentHTML("beforeend", msgToolbar(m));
    wrap.querySelector(".msg-toolbar").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-act]");
      // The one place that asks for a verb at message scope: this row belongs to
      // a single card, and Archive/Delete/Move pressed on it mean that card.
      if (btn) handleAction(btn.dataset.act, m, btn, true);
    });

    // Remote images only exist in the HTML part, so the banner goes with it.
    if (m.remote_blocked && !showImages && !plainFor.has(m.id)) {
      const banner = document.createElement("div");
      banner.className = "remote-banner";
      banner.innerHTML = `<span>${m.remote_blocked} remote image(s) blocked to protect your privacy.</span>
        <button data-load="${m.id}">Load Images</button>`;
      banner.querySelector("button").addEventListener("click", async () => {
        imagesFor.add(m.id);
        const full = await App.api.message(m.id, true);
        Object.assign(m, full);
        rerender();
      });
      wrap.appendChild(banner);
    }

    // A calendar event the mail carries can go into meercal from here. Up by
    // the header rather than down with the attachment chips, because the
    // invitation is usually the reason the mail was sent, and the chips sit
    // under a body that can run to pages. One strip per file. Only once a
    // meercal address is saved (see App.calendar), and only for files whose
    // bytes are still stored. A pruned attachment is a name with nothing
    // behind it to import.
    if (calendarOn()) {
      const events = (m.attachments || []).filter((a) => a.calendar && a.stored !== false);
      events.forEach((a) => {
        const strip = document.createElement("div");
        strip.className = "calendar-banner";
        // With one file, "this message" says it. With several, each strip has
        // to name its own, or they read as the same sentence twice.
        const text = events.length > 1
          ? `${App.esc(a.filename || "An attachment")} contains a calendar event.`
          : "This message contains a calendar event.";
        strip.innerHTML = `${App.icon("calendar", 15)}
          <span class="cb-text">${text}</span>
          <button type="button">Add to meercal</button>`;
        strip.querySelector("button").addEventListener("click", () => App.calendar.open(m, a));
        wrap.appendChild(strip);
      });
    }

    const body = document.createElement("div");
    if (m.content_status && m.content_status !== "full") {
      // Outside the content window: the headers are all there ever was here (or
      // all that is left). Said plainly, because "(no content)" on a message
      // that visibly has a subject and a sender reads as a bug.
      body.className = "msg-body-omitted";
      body.innerHTML = contentNotice(m);
    } else if (m.body_html && !plainFor.has(m.id)) {
      mountFrame(body, m.body_html, () => wrap.classList.add("has-hit"),
        (root, refit) => foldHistory(m, root, refit));
    } else if (m.body_plain) {
      // Text-only mail and the plain-text toggle both land here — for text mail
      // body_plain *is* the text part, so this is the path it always took.
      // Plain-text mail is rendered as markdown: headings, lists, emphasis and
      // `>` quote levels all read better, and text that uses none of it comes
      // out looking exactly as it did before. No iframe needed — the parser
      // escapes everything and builds the HTML itself, so there is nothing of
      // the sender's to sanitize.
      body.className = "msg-body-md";
      body.innerHTML = App.markdown.toHtml(m.body_plain);
      // The `>` lines come out of the parser as <blockquote>, so plain-text
      // history folds the same way a frame's does.
      foldHistory(m, body, null);
      if (App.highlight.mark(body, marksNow()) && shown(body)) wrap.classList.add("has-hit");
    } else {
      body.className = "msg-body-text";
      // Switched to plain on a message that is all pictures and layout: there
      // is HTML, it just has no words in it. Say which of the two it is, so the
      // empty pane does not read as the switch having broken.
      body.textContent = m.body_html ? "(no text content in this message)" : "(no content)";
    }
    wrap.appendChild(body);

    if (m.attachments && m.attachments.length) {
      const at = document.createElement("div");
      at.className = "attachments";
      at.innerHTML = m.attachments.map((a) => {
        // A pruned message keeps its attachment rows for the names and sizes,
        // but the bytes are gone — so the chip stops being a link rather than
        // offering a download that would 404.
        if (a.stored === false) {
          return `<div class="att-item"><span class="attachment-chip is-absent"
              title="${App.esc(a.filename)} — not stored (outside the sync window)">
            ${App.icon("paperclip", 15)}
            <span class="att-meta">
              <span class="att-name">${App.esc(a.filename)}</span>
              <span class="att-size">${App.fmtSize(a.size)} · not stored</span>
            </span>
          </span></div>`;
        }
        // Types the browser renders itself open in a tab; everything else keeps
        // downloading. `viewable` is the server's allowlist, not a guess here —
        // it decides what may be served with an inline disposition.
        const link = a.viewable
          ? `href="/api/attachments/${a.id}?inline=1" target="_blank" rel="noopener"`
          : `href="/api/attachments/${a.id}" download="${App.esc(a.filename)}"`;
        // Previews are precomputed by the agent, so a chip shows one only once
        // that pass has run — mail read seconds after arriving falls back to the
        // paperclip rather than waiting on a render.
        const face = a.has_thumb
          ? `<img class="att-thumb" src="/api/attachments/${a.id}/thumb" alt="" loading="lazy">`
          : App.icon("paperclip", 15);
        // Save and ask, stacked beside the chip rather than in it: the chip is
        // an <a>, and a button inside a link is invalid markup that navigates
        // when you press it. Save comes first because a viewable chip — a PDF,
        // a photo — opens in a tab instead of downloading, so without it the
        // only way to keep the file is a right-click most people never try.
        // The robot is offered only where there is something to read — text
        // Tika extracted, or a picture a model can look at — so it never
        // appears on a zip it could only fail on.
        return `<div class="att-item">
          <a class="attachment-chip${a.has_thumb ? " has-thumb" : ""}" ${link}
              title="${App.esc(a.filename)}">
            ${face}
            <span class="att-meta">
              <span class="att-name">${App.esc(a.filename)}</span>
              <span class="att-size">${App.fmtSize(a.size)}</span>
            </span>
          </a>
          <span class="att-actions">
            <a class="att-btn" href="/api/attachments/${a.id}"
              download="${App.esc(a.filename)}" title="Download"
              aria-label="Download ${App.esc(a.filename)}"
              >${App.icon("download", 15)}</a>
            ${explainable(a) ? `<button class="att-btn att-ai" data-att="${a.id}"
              title="What is this file?" aria-label="Explain ${App.esc(a.filename)}"
              >${App.icon("robot", 15)}</button>` : ""}
          </span>
        </div>`;
      }).join("");
      // One archive of the lot, once there is more than one to save. Only the
      // stored ones count towards that: a pruned row is a name with no bytes
      // behind it, and the zip leaves those out.
      const savable = m.attachments.filter((a) => a.stored !== false);
      if (savable.length > 1) {
        at.insertAdjacentHTML("beforeend", `
          <a class="att-all" href="/api/messages/${m.id}/attachments.zip" download
              title="Download all ${savable.length} attachments as a zip">
            ${App.icon("download", 15)}
            <span>Download all (${savable.length})</span>
          </a>`);
      }
      const items = at.querySelectorAll(".att-item");
      m.attachments.forEach((a, i) => {
        if (a.match_contexts && a.match_contexts.length) {
          items[i].querySelector(".attachment-chip").classList.add("has-hit");
        }
      });
      at.addEventListener("click", (e) => {
        const btn = e.target.closest(".att-ai");
        if (!btn) return;
        const a = m.attachments.find((x) => String(x.id) === btn.dataset.att);
        if (a) App.ai.openAttachment(a);
      });
      wrap.appendChild(at);

      // A search can match a message purely on text extracted from a PDF, with
      // the term nowhere in the mail itself. Without this the reader would open
      // on a conversation showing no reason to have matched at all, so the
      // hits get quoted out of the attachment under its chip.
      const quoted = m.attachments.filter((a) => a.match_contexts && a.match_contexts.length);
      if (quoted.length) {
        wrap.classList.add("has-hit");
        const hits = document.createElement("div");
        hits.className = "att-hits";
        hits.innerHTML = quoted.map((a) => `
          <div class="att-hit">
            <div class="att-hit-name">${App.icon("paperclip", 13)} ${App.esc(a.filename)}</div>
            ${a.match_contexts.map((c) => `<div class="att-hit-quote">…${App.esc(c.before)}<mark
              class="hit">${App.esc(c.match)}</mark>${App.esc(c.after)}…</div>`).join("")}
          </div>`).join("");
        wrap.appendChild(hits);
      }
    }
    return wrap;
  }

  // The placeholder in the reading pane does double duty: "pick something" while
  // there is mail to pick, and the reward for clearing the folder once there
  // isn't. Driven off the list rather than the reader so it is right whichever
  // way the folder emptied — archived, deleted, or simply never had anything.
  function renderEmpty() {
    const empty = document.getElementById("reader-empty");
    const done = App.list && App.list.count() === 0;
    empty.classList.toggle("all-done", !!done);
    empty.innerHTML = done
      ? `<img class="reader-empty-art" src="/static/img/meerkat-no-tasks.png" alt="">
         <p>Whooo all done for today.</p>`
      : `<div class="reader-empty-glyph"></div><p>No message selected</p>`;
  }

  // pinLast: land the pane on the newest message rather than keeping the
  // scroll position. Only an opening thread wants this — a toggle or a flag
  // redraws under you and must leave the view where you left it.
  function rerender(pinLast) {
    // Both menus are anchored to a toolbar button that is about to be replaced.
    closeMoveMenu();
    if (App.reminders) App.reminders.close();
    // Every body frame goes with them, and a discarded document sends no
    // mouseout — so the peek would otherwise stand there naming a link that is
    // no longer under the pointer, or on screen at all.
    if (App.linkpeek) App.linkpeek.hide();
    pin = pinLast ? {} : null;
    // A redraw remounts every body frame, so the pane is unmeasured again until
    // they land. The chip stays down in the meantime rather than counting
    // against half-height messages.
    renderId += 1;
    frames = 0;
    settled = false;
    // Ctrl+A's ranges point into the messages and frames about to be replaced.
    picked = null;
    renderBar();
    const host = document.getElementById("reader-content");
    const empty = document.getElementById("reader-empty");
    // updateEarlier() with nothing settled takes the chip down, which is what
    // an emptied pane needs — clear() comes through here with it still up.
    if (!currentThread) {
      host.hidden = true; empty.hidden = false; renderEmpty(); updateEarlier(); return;
    }
    empty.hidden = true; host.hidden = false;
    host.innerHTML = "";
    // Above the conversation, and only for one that is waiting on a reminder:
    // it left the folder it was filed from, so this strip is the only place the
    // promise can be read or taken back.
    const strip = App.reminders && App.reminders.strip(currentThread);
    if (strip) host.appendChild(strip);
    for (const m of currentThread.messages) host.appendChild(renderMsg(m));
    // Fitted here rather than in renderMsg: a row's width is only knowable once
    // it is in the document, and a collapsed message renders no toolbar at all,
    // so everything in the pane now is a row that is really on screen.
    fitToolbars();
    // Right away for text-only mail; the iframes redo it as they measure up.
    if (pinLast) landOn();
    // Text-only mail mounts no frame, so nothing else would ever call time on
    // the layout; with frames up, the last one to load does it.
    if (frames === 0) settle();
  }

  // `accountIds` is one account or several: the row in All Inboxes that stands
  // for the same thread at two of your addresses opens it in both (see
  // App.memberAccounts), and the server folds a mail that reached both into
  // one card.
  async function openThread(threadId, accountIds, focusId) {
    const request = ++openRequest;
    // Asked for with the search still in hand: the server has to find the hits
    // in extracted attachment text, which the client never sees.
    const search = App.search && App.search.isActive() ? App.search.query() : null;
    let data;
    // Counted rather than flagged, so overlapping opens cannot clear it early.
    // app.keys.js reads this to tell "no thread" from "the thread you just
    // asked for has not landed yet" — see isBusy().
    loading += 1;
    try {
      data = await App.api.thread(threadId, accountIds, false, search);
    } finally {
      loading -= 1;
    }
    if (request !== openRequest) return;
    // The accounts that really hold the thread, as the server counted them. An
    // answer without the list (a server older than this page) is one account's
    // thread, the account of its messages. See threadAccounts().
    if (!Array.isArray(data.account_ids) || !data.account_ids.length) {
      data.account_ids = data.messages && data.messages.length
        ? [data.messages[0].account_id] : [].concat(accountIds);
    }
    marks = search ? App.highlight.patterns(search.q, search.mode) : [];
    resetFind();
    currentThread = data;
    threadSearch = search;
    imagesFor = new Set();
    plainFor = new Set();
    allTo = new Set();
    historyFor = new Set();
    prints = new Map();
    dropped = new Set();
    // Whole conversation open, oldest to newest — folding is something you ask
    // for per message, not a state a thread arrives in.
    collapsed = new Set();
    // Picked up where this conversation was left, counting the visits before
    // this one — not started over.
    viewed = loadViewed();
    pressedMid = null;
    rerender(true);
    // clear() drops the ↑↓ marker along with the thread it belonged to, which
    // is right when the pane empties — but archiving from here empties it and
    // opens the next conversation in one go, and the keyboard never left. Ask
    // where it actually is rather than leaving the bar saying otherwise.
    if (App.keys && App.keys.pane() === "reader") setKeyFocus(true);
    // Opening a conversation marks its messages read (write-back via the agent).
    // A card folded from two copies is unread while either copy is, and the
    // copies go with it, so the other inbox stops calling it unread too.
    for (const m of data.messages) {
      if (!m.seen) { m.seen = true; App.api.markSeen(m.id, true, twinIds(m)).catch(() => {}); }
    }
  }

  // Mail that lands in the conversation on screen. The pane used to be drawn
  // once, when the thread opened, and nothing redrew it after that. So a reply
  // sent from here did not show up in its thread until the conversation was
  // clicked again. The copy the mail server files in Sent reaches the database
  // ten to fifty seconds after the send (measured on 2026-10-24), long after the
  // composer has closed. The list refreshed for it, and the thread it belongs to
  // sat beside the list without it. An incoming reply had the same gap.
  //
  // So the agent's "messages" event (app.shell.js) brings the open thread in
  // again, and only the cards that are new get put in place. The ones already
  // drawn are left alone. A full rerender remounts every body frame, and while
  // they reload at the wrong height the pane jumps under whoever is reading. That
  // is a fair price for a fold or a flag they pressed, not for mail they did not
  // ask for.
  //
  // Additions only. A message gone from the server's answer stays on screen
  // until the conversation is opened again. One this pane took off itself
  // (finishRemove) stays off. A message moved to Trash is still in the thread,
  // filed in Trash, and bringing it back would read as the delete not working.
  //
  // New mail is not marked read here, unlike on opening. It turns up below
  // where you are reading, and nothing says you have seen it yet. The copy of
  // your own reply is read already.
  //
  // `account` is the address the event names. Mail for another account cannot
  // belong to this conversation, so it costs no fetch. Without one (coming back
  // from power save, where every event in between went unheard) the thread is
  // checked anyway. A conversation read in two accounts (threadAccounts) is
  // looked at again for mail in either of them, and skipped only when every
  // one of them is known and none has that address.
  function mailArrived(account) {
    if (!currentThread) return;
    if (account && App.shell) {
      const known = App.shell.accounts();
      const accs = threadAccounts().map((id) => known.find((a) => a.id === id));
      if (accs.length && accs.every((a) => a && a.email !== account)) return;
    }
    // A batch per folder, and Proton files the copy in Sent and All Mail. One
    // look after they have all landed is enough.
    clearTimeout(arrivalTimer);
    arrivalTimer = setTimeout(fetchNew, 700);
  }

  async function fetchNew() {
    if (!currentThread) return;
    // A thread on its way may have been read before this mail was stored, so it
    // is asked again once that one is on screen. A look already on its way
    // likewise, since it may have left before the latest batch committed. One
    // look at a time keeps a full recheck, which stores a batch every second or
    // so, from stacking up fetches of a long thread.
    if (loading > 0 || fetchingNew) {
      clearTimeout(arrivalTimer);
      arrivalTimer = setTimeout(fetchNew, 700);
      return;
    }
    const thread = currentThread;
    const request = openRequest;
    fetchingNew = true;
    let data;
    try {
      data = await App.api.thread(thread.thread_id, threadAccounts(), false, threadSearch);
    } catch (_) {
      return;   // offline, or the conversation is gone. The next mail asks again.
    } finally {
      fetchingNew = false;
    }
    // Another conversation opened, or this one closed, while the answer was out.
    if (request !== openRequest || thread !== currentThread) return;
    addMessages(data.messages);
  }

  function addMessages(list) {
    const msgs = currentThread.messages;
    // The same mail in two accounts arrives as one entry with the other copy
    // in `twins`, and which copy leads the entry can change when the second
    // one lands. A card that already stands for one of its copies is told
    // about the rest, so read, flag and file reach the copy that just arrived.
    // Data only, with no redraw, for the reason given above.
    for (const m of list) {
      const copies = [{ id: m.id, account_id: m.account_id, locations: m.locations },
                      ...(m.twins || [])];
      const card = msgs.find((x) => copies.some((c) => c.id === x.id));
      if (card) card.twins = copies.filter((c) => c.id !== card.id);
    }
    // Every copy a card stands for, so an entry led by the other copy of a
    // card on screen is that card and not new mail. The same goes for copies
    // of a card this pane took off itself (finishRemove).
    const have = new Set();
    for (const x of msgs) for (const id of [x.id, ...twinIds(x)]) have.add(id);
    const known = (id) => have.has(id) || dropped.has(id);
    const fresh = list.filter((m) => !known(m.id) && !twinIds(m).some(known));
    if (!fresh.length) return;
    const host = document.getElementById("reader-content");
    // The server's order, oldest first, decides where each one goes. A message
    // that turns up late (older mail synced from a folder the agent reached
    // last) lands between the ones it was written between, not at the bottom.
    // That is above the view now and then, and then only scroll anchoring
    // (Chromium, Firefox) keeps what you are reading where it was.
    const rank = new Map(list.map((m, i) => [m.id, i]));
    // A frame mounted now would otherwise land the pane on the newest message
    // when it loads, which is right while the thread is still opening and wrong
    // once someone is reading it. See mountFrame().
    if (settled) pin = null;
    for (const m of fresh) {
      const at = msgs.findIndex((x) => rank.has(x.id) && rank.get(x.id) > rank.get(m.id));
      const next = at < 0 ? null
        : host.querySelector(`.thread-msg[data-mid="${msgs[at].id}"]`);
      // In its place before it is drawn, because the drawing asks which
      // messages are above it (historyOf) to decide what of its quote to fold.
      msgs.splice(at < 0 ? msgs.length : at, 0, m);
      const card = renderMsg(m);
      host.insertBefore(card, next);
      card.querySelectorAll(".msg-toolbar").forEach(fitToolbar);
    }
    // The same accounting rerender() does. The chip and the find box wait for
    // new frames to be measured, and text-only mail is measured already.
    if (frames === 0) settle(); else settled = false;
    // The bar means the newest message, and that may be one of these. It is
    // left alone while a menu hangs off one of its buttons, because redrawing
    // would pull the button out from under the menu. The next redraw catches up.
    if (!openMenu && !(App.reminders && App.reminders.isOpen())) renderBar();
  }

  // No thread means nothing to scroll, so the arrow marker goes with it.
  function clear() {
    openRequest += 1;
    currentThread = null;
    threadSearch = null;
    dropped = new Set();
    marks = [];
    resetFind();
    viewed = new Set();
    pressedMid = null;
    keyFocus = false;
    rerender();
  }

  // Keyboard entry point — the same target and the same toolbar button as a
  // click, so a shortcut can never act on a different message than the icon
  // sitting above it. The button doubles as the anchor for the move menu.
  function action(act) {
    const m = targetMsg();
    if (!m) return false;
    const anchor = document.querySelector(`#reader-bar [data-act="${act}"]`);
    handleAction(act, m, anchor);
    return true;
  }

  // .reading-pane is the scroller; #reader-content is just its contents.
  function pane() { return currentThread ? document.querySelector(".reading-pane") : null; }

  // `frac` is a share of the visible height: Space pages, the arrows nudge.
  function scrollBy(dir, frac = 0.9) {
    const p = pane();
    if (!p) return false;
    // Held arrows queue up smooth animations and then lag behind the key
    // repeat, so only the one-shot page scroll animates.
    p.scrollBy({ top: dir * (p.clientHeight * frac),
                 behavior: frac >= 0.5 ? "smooth" : "auto" });
    return true;
  }

  function scrollEnd(dir) {
    const p = pane();
    if (!p) return false;
    pressedMid = null;     // a jump says where you are now, see msgAt()
    p.scrollTo({ top: dir > 0 ? p.scrollHeight : 0, behavior: "smooth" });
    return true;
  }

  // Jumps to the header of the next or previous message in the conversation.
  // The arrows and Space move by the screenful, which in a long thread is a lot
  // of presses between one message and the next — this is the coarse gear.
  //
  // `held` is the key repeating: the same reasoning as scrollBy(), where a
  // queue of smooth animations falls behind a held key, so only single presses
  // animate.
  function scrollMsg(dir, held) {
    const p = pane();
    if (!p) return false;
    pressedMid = null;     // a jump says where you are now, see msgAt()
    // The line scrollToMsg() parks a header on: the top of the pane, clear of
    // the sticky bars. A message sitting within a few pixels of it is the one
    // being read rather than one to jump to, which is what keeps a press going
    // somewhere instead of re-landing on the message already at the top.
    const line = p.getBoundingClientRect().top + stuckTop() + 10;
    const msgs = rows();
    const to = dir > 0
      ? msgs.find((el) => el.getBoundingClientRect().top > line + 4)
      : msgs.filter((el) => el.getBoundingClientRect().top < line - 4).pop();
    // Past the last header there is still the tail of the last message to read,
    // and above the first one the top of the thread, so the key never dies in
    // the reader — it runs out of messages into the ends of the conversation.
    if (to) scrollToMsg(to, !held); else scrollEnd(dir);
    return true;
  }

  // h, from app.keys.js: the quoted history of the message you are on (msgAt),
  // out and back in. n and N are how a thread is read from the keyboard, and
  // the ••• button is otherwise the one thing in it that wants the mouse.
  // Answers false when that message has no history folded, so the key does
  // nothing rather than reaching for some other message.
  function toggleHistory() {
    const wrap = currentThread && msgAt();
    if (!wrap) return false;
    const md = wrap.querySelector(".msg-body-md");
    const frame = md ? null : wrap.querySelector(".msg-body-frame");
    let root = md;
    if (frame) {
      try { root = frame.contentDocument && frame.contentDocument.body; } catch (_) { /* mid-load */ }
    }
    const button = App.quotes.toggle(root);
    if (!button) return false;
    // Pressed on, the way a click would be. Opening the newest message's
    // history takes the pane off the end of the thread, and msgAt() would
    // otherwise answer the next h with the message above it.
    pressedMid = wrap.dataset.mid;
    // The button is where the history starts. Shut, the history you may have
    // been reading in is gone from under you, and open, it runs on below the
    // button, so either way the button is the place to look. Scrolled to only
    // when it is off screen, so a press on one in view moves nothing.
    const p = pane();
    const top = hitTop({ el: button, frame });
    const box = p.getBoundingClientRect();
    if (top < box.top + stuckTop() || top > box.bottom - 40) {
      p.scrollTo({ top: Math.max(0, p.scrollTop + top - box.top - stuckTop() - 60), behavior: "smooth" });
    }
    return true;
  }

  // Delegated once, so redrawing the bar never has to re-bind it.
  document.getElementById("reader-bar").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act]");
    if (btn) handleAction(btn.dataset.act, targetMsg(), btn);
  });

  // The find box is outside the half renderBar() redraws, so it is wired here
  // once for the life of the app rather than on every draw.
  (function wireFind() {
    const box = document.getElementById("reader-find");
    const input = findInput();
    box.querySelector(".find-icon").innerHTML = App.icon("search", 14);
    box.querySelector(".find-prev").innerHTML = App.icon("chevron", 14);
    box.querySelector(".find-next").innerHTML = App.icon("chevron", 14);
    box.querySelector(".find-clear").innerHTML = App.icon("close", 14);
    const close = document.getElementById("reader-find-close");
    close.innerHTML = App.icon("close", 18);
    // The phone's button. Same gesture both ways round, so a find opened by
    // mistake takes one tap to undo.
    const toggle = document.getElementById("reader-find-toggle");
    toggle.innerHTML = App.icon("search", 18);
    toggle.addEventListener("click", () => {
      if (findOpen) return closeFind();
      openFind();
      input.focus();
    });
    close.addEventListener("click", closeFind);

    // Marking a long thread walks every text node in it, iframes included, so
    // the pass waits out a run of typing rather than running per letter.
    let typing = null;
    const soon = () => {
      clearTimeout(typing);
      typing = setTimeout(() => setFind(input.value), 140);
    };
    input.addEventListener("input", soon);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        // Swallowed rather than passed on: the global Escape steps out of the
        // reading pane, which is not what the key means with a caret in a box
        // inside it. Back to the thread, unlit, in one press.
        e.preventDefault();
        e.stopPropagation();
        closeFind();
        return;
      }
      if (e.key !== "Enter") return;
      e.preventDefault();
      clearTimeout(typing);
      // Enter before the pause has run out means "now" — walking the matches
      // only starts once the ones you typed are actually on screen.
      if (input.value.trim() !== findQ) setFind(input.value);
      else stepHit(e.shiftKey ? -1 : 1);
    });
    box.querySelector(".find-prev").addEventListener("click", () => stepHit(-1));
    box.querySelector(".find-next").addEventListener("click", () => stepHit(1));
    box.querySelector(".find-clear").addEventListener("click", () => {
      clearFind();
      input.focus();
    });
  })();
  renderBar();

  // The chip goes to the oldest message you have not seen, so a conversation
  // you dipped into halfway is picked up where you left off rather than from
  // the very top.
  document.getElementById("earlier-pill").addEventListener("click", () => {
    scrollToMsg(rows().find((r) => !viewed.has(r.dataset.mid)), true);
  });
  document.querySelector(".reading-pane")
    .addEventListener("scroll", updateEarlier, { passive: true });

  // The page's half of what mountFrame() wires up in each body frame.
  document.addEventListener("copy", onCopy);
  document.addEventListener("selectionchange", onSelectionChange);
  document.getElementById("reader-content").addEventListener("pointerdown", onPress);

  // The pane rather than the toolbars, for two reasons. The divider between the
  // list and the reader is draggable, so the width that decides this changes
  // without any message being re-rendered — and watching the pane means one
  // observer for the life of the app, instead of one per message re-registered
  // on every redraw. It is also the box that cannot be resized by what the
  // callback does: folding a toolbar changes the row's contents, never the
  // pane's width, so there is no loop to fall into.
  if (window.ResizeObserver) {
    new ResizeObserver(() => { fitToolbars(); updateEarlier(); })
      .observe(document.querySelector(".reading-pane"));
  }

  // `redraw` is exported so App.tasks can put the Add Task buttons up (or take
  // them down) the moment the Meerato URL changes, rather than at the next
  // thread open — both the bar and the per-message toolbars carry one.
  // App.calendar uses it the same way for the meercal strips.
  return { openThread, clear, mailArrived, action, scrollBy, scrollEnd, scrollMsg, setKeyFocus,
    renderEmpty, toggleHistory,
    redraw: () => rerender(), isOpen: () => !!currentThread,
    // The composer previews a forwarded original in the same document the
    // reader draws it in, so it looks there the way it looked here.
    frameDoc,
    // Find in thread, for app.keys.js: Ctrl/Cmd+F puts the caret in the box,
    // and Escape takes a find back down before it starts stepping out of panes
    // — which it has to be asked about, since the query can still be lit, or
    // the box still standing open on a phone, with the caret long gone from it.
    focusFind, clearFind, closeFind, findActive: () => !!findQ || findOpen,
    // Ctrl/Cmd+A with the keyboard in this pane: the message, then the thread.
    selectAll,
    // How many messages the open conversation holds — App.ai says so before it
    // sends any of them to a provider.
    threadSize: () => (currentThread ? (currentThread.messages || []).length : 0),
    // "A thread is on its way." The keyboard moves into this pane on the same
    // keystroke that asks for the thread, which is a fetch ahead of isOpen().
    isBusy: () => loading > 0,
    // For the composer's "Send & Archive": which conversation it will file, and
    // the filing itself. Both return as soon as the UI has moved on — the
    // request runs behind them and reports its own failure, which is right for
    // the composer too, since the mail is already sent by then and a failed
    // archive must not read as a failed send.
    // Reminders: App.reminders owns the menu and the strip, and calls these to
    // act on the conversation the pane is holding.
    // currentReminder is what makes the bell menu know it is being opened on a
    // conversation that is already waiting on one, so it can offer to clear it
    // rather than only to set another.
    remindThread, unremindThread,
    currentReminder: () => (currentThread ? currentThread.reminder || null : null),
    archiveTicket, archiveTicketed };
})();
