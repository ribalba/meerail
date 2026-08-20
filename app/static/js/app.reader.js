/* meerail reader: renders a conversation thread in the reading pane. */

App.reader = (function () {
  let currentThread = null;
  let openRequest = 0;
  let loading = 0;              // threads fetched but not yet on screen — see isBusy()
  let collapsed = new Set();    // message ids folded shut; everything else is open
  let imagesFor = new Set();    // message ids with remote images loaded
  let plainFor = new Set();     // message ids switched to their plain-text part
  let allTo = new Set();        // message ids showing their full recipient list
  let keyFocus = false;         // are the arrow keys scrolling this pane?
  // The search that led here, captured when the thread opened. Held rather than
  // read live off the search box so a rerender mid-typing keeps marking the
  // term you actually opened the conversation on.
  let marks = [];
  // Which messages have actually been on screen, by id. A thread opens on its
  // newest message, so everything earlier is off the top of the pane with
  // nothing to say so — this is what the "earlier messages" chip counts. It has
  // to be viewport history rather than the seen flag, which openThread sets on
  // the whole conversation the moment it opens.
  let viewed = new Set();
  let renderId = 0;    // which draw a body frame belongs to — see mountFrame()
  let frames = 0;      // its frames mounted but not yet measured
  let settled = false; // ...so nothing in the pane is where it will end up yet

  // Every action — toolbar or keyboard — applies to the newest message in the
  // conversation. That is the one you are replying to, and it keeps the single
  // toolbar honest: no hidden "which message is selected" state to guess at.
  function targetMsg() {
    const msgs = currentThread ? currentThread.messages : [];
    return msgs.length ? msgs[msgs.length - 1] : null;
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
        blockquote{margin:0 0 0 .8em;padding-left:.8em;border-left:3px solid #d2d2d7;color:#555}
        ${App.highlight.FRAME_CSS}
      </style></head><body>${html || ""}</body></html>`;
  }

  // Set for the duration of a render that should land on the newest message.
  // Bodies live in iframes that only get their real height once loaded, so the
  // scroll has to be redone as each one settles — a fresh object per render, so
  // frames left over from an earlier one fail the identity check and stay put.
  let pin = null;

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
    scrollToMsg((marks.length && host.querySelector(".thread-msg.has-hit"))
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
    msgs.forEach((row, i) => {
      const r = row.getBoundingClientRect();
      if (r.bottom <= top || r.top >= box.bottom) return;
      viewed.add(row.dataset.mid);
      if (i < first) first = i;
    });
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
    // Height comes after the width, so it is measured against the reflowed
    // document rather than the squeezed one.
    const h = doc.documentElement.scrollHeight + 4;
    frame.style.height = h + "px";
    // A transform is drawn small but laid out full size, so without this the
    // message trails a gap the height of everything the scale took off.
    frame.style.marginBottom = scale < 1 ? -h * (1 - scale) + "px" : "";
  }

  function mountFrame(container, html, onHit) {
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
        // Marked before the height is measured, so a term that wraps a line
        // does not leave the frame short by one.
        if (App.highlight.mark(doc.body, marks) && onHit) onHit();
        fitFrame(frame, doc);
        // Once you click inside a message body the iframe owns the keyboard and
        // shortcuts would silently stop working. Forward them back out — this
        // reaches across only because the sandbox allows same-origin.
        if (App.keys) doc.addEventListener("keydown", App.keys.handle);
      } catch (_) { frame.style.height = "400px"; }
      if (p === pin) landOn();
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
      <button class="tb-btn" data-act="archive" title="Archive">${App.icon("archive", 16)}</button>
      <button class="tb-btn" data-act="trash" title="Delete">${App.icon("trash", 16)}</button>
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
      <button class="tb-btn" data-act="move" title="Move to folder">${App.icon("move", 16)}</button>
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
    const m = targetMsg();
    bar.innerHTML = BAR_BUTTONS.filter(
      (b) => (!b.tasks || tasksOn()) && (!b.ai || aiOn())
    ).map((b) => {
      if (b.sep) return `<span class="tb-sep"></span>`;
      const flagged = b.act === "flag" && m && m.flagged;
      const off = b.act !== "new" && !m;
      // Delete is a different verb in Trash — there is nowhere left to file the
      // conversation, so the button destroys it — and the tooltip is where that
      // has to be said before the click rather than after it.
      const title = b.act === "trash" && inTrash() ? "Delete forever" : b.title;
      return `<button class="tb-btn${flagged ? " on" : ""}" data-act="${b.act}"
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
    if (keyFocus) bar.insertAdjacentHTML("afterbegin",
      `<span class="tb-keys" title="Arrow keys scroll this thread — Esc goes back to the list"
        >↑↓<span class="tb-keys-label">scroll</span></span>`);
  }

  // Owned here, and re-rendered from the flag, so a redraw of the bar cannot
  // drop the marker on the floor.
  function setKeyFocus(state) {
    if (keyFocus === state) return;
    keyFocus = state;
    renderBar();
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
  function openOverflowMenu(m, anchor) {
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
      // off is the one that is not on screen.
      handleAction(act, m, anchor);
    });
    mountMenu(el, anchor);
  }

  function openMoveMenu(m, anchor) {
    closeMoveMenu();
    const source = sourceOf(m);
    // The folder it already sits in is not a destination; neither is a folder
    // it is already filed under, which IMAP would take but which reads as a
    // move that did nothing.
    const here = new Set(m.locations.map((loc) => loc.mailbox_id));
    const folders = (App.shell ? App.shell.mailboxesFor(m.account_id) : [])
      .filter((mb) => mb.id !== source && !here.has(mb.id));

    const el = document.createElement("div");
    el.className = "move-menu";
    el.innerHTML = folders.length
      // The path, not the leaf: a menu offering three folders called "2024" is
      // not a choice anybody can make.
      ? folders.map((mb) => `<button class="move-item" data-mailbox="${mb.id}">
          <span class="mm-icon">${App.icon(App.roleIcon(mb.role), 15)}</span>
          <span class="mm-name">${App.esc(mb.path || mb.display_name)}</span></button>`).join("")
      : `<div class="move-empty">No other folders</div>`;

    el.addEventListener("click", (e) => {
      const item = e.target.closest("[data-mailbox]");
      if (!item) return;
      const mailboxId = Number(item.dataset.mailbox);
      closeMoveMenu();
      moveThreadTo(mailboxId);
    });
    mountMenu(el, anchor);
  }

  // Like archive and trash, a move takes the whole conversation with it — each
  // message leaves its own folder, so no reply is stranded behind.
  function moveThreadTo(mailboxId) {
    try {
      const targets = moveTargets();
      if (!targets.length) throw new Error("No source mailbox for this message");
      finishRemove(targets.map((t) => t.m), (async () => {
        for (const t of targets) await App.api.moveMsg(t.m.id, mailboxId, t.source);
      })());
    } catch (e) { alert(e.message || "Move failed"); }
  }

  // Archive/trash the whole conversation. Split out of handleAction so the
  // composer can archive the thread it just replied to without going through a
  // toolbar button that may not be the one the user is looking at.
  //
  // One call for the conversation rather than one per message on screen: the
  // server resolves the thread fresh and empties every folder it is filed
  // under, so a message that arrived after this pane was drawn — or a second
  // placement under a label — can't hold the row in the list.
  function removeThread(act) {
    const msgs = currentThread ? currentThread.messages : [];
    if (!msgs.length) return;
    const accountId = msgs[0].account_id;
    // Only a threaded conversation has an id to act on. A message that never
    // got threaded stands alone, so its single placement is the whole job.
    if (currentThread.thread_id) {
      const threadId = currentThread.thread_id;
      return finishRemove(msgs.slice(),
        act === "archive" ? App.api.archiveThread(threadId, accountId)
                          : App.api.trashThread(threadId, accountId));
    }
    const targets = moveTargets();
    if (!targets.length) throw new Error("No source mailbox for this message");
    finishRemove(targets.map((t) => t.m), (async () => {
      for (const t of targets) {
        if (act === "archive") await App.api.archiveMsg(t.m.id, t.source);
        else await App.api.trashMsg(t.m.id, t.source);
      }
    })());
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
  function deleteForever() {
    const msgs = currentThread ? currentThread.messages : [];
    if (!msgs.length) return;
    const accountId = msgs[0].account_id;
    const acc = App.shell.accounts().find((a) => a.id === accountId);
    const what = acc && acc.local
      ? "This mail was imported, so meerail holds the only copy of it."
      : "This deletes it from the mail server.";
    if (!confirm(`Permanently delete this conversation?\n\n${what} It cannot be undone.`)) return;
    // One item for the conversation where there is one, so a reply that landed
    // after this pane was drawn goes with it — the same rule removeThread
    // follows, and for the same reason.
    const items = currentThread.thread_id
      ? [{ account_id: accountId, thread_id: currentThread.thread_id }]
      : msgs.map((m) => ({ account_id: m.account_id, message_id: m.id }));
    finishRemove(msgs.slice(), App.api.bulkPurge(items));
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
    // the conversation from it), and this is the one the toolbar acts on.
    const target = msgs[msgs.length - 1];
    finishRemove(msgs.slice(), App.api.remind(target.id, when));
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
    const accountId = msgs[0].account_id;
    try {
      await App.api.unremind(target.id, restore);
    } catch (e) {
      return alert(e.message || "Could not change the reminder");
    }
    // Re-read rather than patched in place: bringing a conversation back moves
    // it and marks it unread, and the strip is not the only thing on screen
    // that changes.
    if (threadId) await openThread(threadId, accountId, target.id);
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
    return {
      thread_id: currentThread.thread_id || null,
      account_id: msgs[0].account_id,
      ids: msgs.map((m) => m.id),
      // Only for a conversation that was never threaded, which has no id to act
      // on: each message leaves the folder it was in when the ticket was taken.
      targets: currentThread.thread_id ? [] :
        moveTargets().map((t) => ({ id: t.m.id, source: t.source })),
    };
  }

  function stillShowing(ticket) {
    const msgs = currentThread ? currentThread.messages : [];
    if (!msgs.length) return false;
    if (ticket.thread_id) {
      return currentThread.thread_id === ticket.thread_id
        && msgs[0].account_id === ticket.account_id;
    }
    return msgs.some((m) => ticket.ids.includes(m.id));
  }

  // Still the conversation on screen: the ordinary path, which empties the pane
  // and opens the next mail down. Otherwise it is filed out of sight, and only
  // its row leaves the list.
  function archiveTicketed(ticket) {
    if (!ticket) return;
    if (stillShowing(ticket)) return removeThread("archive");
    const call = ticket.thread_id
      ? App.api.archiveThread(ticket.thread_id, ticket.account_id)
      : (async () => {
        for (const t of ticket.targets) await App.api.archiveMsg(t.id, t.source);
      })();
    App.list.drop((r) => (ticket.thread_id
      ? r.thread_id === ticket.thread_id && r.account_id === ticket.account_id
      : ticket.ids.includes(r.id)));
    call.then(() => App.shell && App.shell.reloadList())
      .catch((e) => {
        alert(e.message || "Archive failed");
        if (App.shell) App.shell.reloadList();
      });
  }

  async function handleAction(act, m, anchor) {
    try {
      if (act === "new") return App.compose.openNew();
      if (!m) return;
      if (act === "more") return anchor && openOverflowMenu(m, anchor);
      if (act === "move") return anchor && openMoveMenu(m, anchor);
      if (act === "remind") return anchor && App.reminders.open(m, anchor);
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
      if (act === "flag") { m.flagged = !m.flagged; rerender(); await App.api.flagMsg(m.id, m.flagged); return; }
      if (act === "unread") { m.seen = false; await App.api.markSeen(m.id, false); return; }
      if (act === "trash" && inTrash()) return deleteForever();
      if (act === "archive" || act === "trash") return removeThread(act);
    } catch (e) { alert(e.message || "Action failed"); }
  }

  // The pane, the list and the cursor all move on *before* the server is asked:
  // `call` is the request that makes it true, and against a remote server its
  // round trip is what made archive and delete feel stuck. The reload after it
  // settles reconciles the list with the truth — which, when the server said
  // no, is also what puts the rows back.
  function finishRemove(removed, call) {
    const threadId = currentThread.thread_id;
    const accountId = removed[0].account_id;
    const gone = new Set(removed.map((x) => x.id));
    currentThread.messages = currentThread.messages.filter((x) => !gone.has(x.id));
    const emptied = !currentThread.messages.length;
    if (emptied) clear(); else rerender();
    // A list row is a conversation, so it goes by thread — its id is whichever
    // message the row was built from, not necessarily one the reader held.
    App.list.drop((r) => (threadId
      ? r.thread_id === threadId && r.account_id === accountId
      : gone.has(r.id)));
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
  // can still be folded away one card at a time.
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
          <div class="msg-date-full${shut ? "" : " selectable"}">${App.esc(App.fmtDateFull(m.date))}</div>
          <span class="msg-chevron">${App.icon("chevron", 16)}</span>
        </div>
        ${shut ? "" : `<div class="thread-subject selectable">${App.esc(m.subject || "(no subject)")}</div>`}
      </div>`;
    const head = wrap.querySelector(".msg-head");
    // Participants are part of what search matched on, so the header is marked
    // too — including the collapsed snippet, which is often the only text a
    // folded message shows.
    if (App.highlight.mark(head, marks)) wrap.classList.add("has-hit");
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
        App.highlight.mark(detailEl, marks);
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
      if (btn) handleAction(btn.dataset.act, m, btn);
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

    const body = document.createElement("div");
    if (m.content_status && m.content_status !== "full") {
      // Outside the content window: the headers are all there ever was here (or
      // all that is left). Said plainly, because "(no content)" on a message
      // that visibly has a subject and a sender reads as a bug.
      body.className = "msg-body-omitted";
      body.innerHTML = contentNotice(m);
    } else if (m.body_html && !plainFor.has(m.id)) {
      mountFrame(body, m.body_html, () => wrap.classList.add("has-hit"));
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
      if (App.highlight.mark(body, marks)) wrap.classList.add("has-hit");
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
    pin = pinLast ? {} : null;
    // A redraw remounts every body frame, so the pane is unmeasured again until
    // they land. The chip stays down in the meantime rather than counting
    // against half-height messages.
    renderId += 1;
    frames = 0;
    settled = false;
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

  async function openThread(threadId, accountId, focusId) {
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
      data = await App.api.thread(threadId, accountId, false, search);
    } finally {
      loading -= 1;
    }
    if (request !== openRequest) return;
    marks = search ? App.highlight.patterns(search.q, search.mode) : [];
    currentThread = data;
    imagesFor = new Set();
    plainFor = new Set();
    allTo = new Set();
    // Whole conversation open, oldest to newest — folding is something you ask
    // for per message, not a state a thread arrives in.
    collapsed = new Set();
    // A different conversation, so what has been on screen starts over.
    viewed = new Set();
    rerender(true);
    // clear() drops the ↑↓ marker along with the thread it belonged to, which
    // is right when the pane empties — but archiving from here empties it and
    // opens the next conversation in one go, and the keyboard never left. Ask
    // where it actually is rather than leaving the bar saying otherwise.
    if (App.keys && App.keys.pane() === "reader") setKeyFocus(true);
    // Opening a conversation marks its messages read (write-back via the agent).
    for (const m of data.messages) {
      if (!m.seen) { m.seen = true; App.api.markSeen(m.id, true).catch(() => {}); }
    }
  }

  // No thread means nothing to scroll, so the arrow marker goes with it.
  function clear() {
    openRequest += 1;
    currentThread = null;
    marks = [];
    viewed = new Set();
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
    p.scrollTo({ top: dir > 0 ? p.scrollHeight : 0, behavior: "smooth" });
    return true;
  }

  // Delegated once, so redrawing the bar never has to re-bind it.
  document.getElementById("reader-bar").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act]");
    if (btn) handleAction(btn.dataset.act, targetMsg(), btn);
  });
  renderBar();

  // The chip goes to the oldest message you have not seen, so a conversation
  // you dipped into halfway is picked up where you left off rather than from
  // the very top.
  document.getElementById("earlier-pill").addEventListener("click", () => {
    scrollToMsg(rows().find((r) => !viewed.has(r.dataset.mid)), true);
  });
  document.querySelector(".reading-pane")
    .addEventListener("scroll", updateEarlier, { passive: true });

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
  return { openThread, clear, action, scrollBy, scrollEnd, setKeyFocus, renderEmpty,
    redraw: () => rerender(), isOpen: () => !!currentThread,
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
