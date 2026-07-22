/* meerail reader: renders a conversation thread in the reading pane. */

App.reader = (function () {
  let currentThread = null;
  let openRequest = 0;
  let collapsed = new Set();    // message ids folded shut; everything else is open
  let imagesFor = new Set();    // message ids with remote images loaded
  let keyFocus = false;         // are the arrow keys scrolling this pane?
  // The search that led here, captured when the thread opened. Held rather than
  // read live off the search box so a rerender mid-typing keeps marking the
  // term you actually opened the conversation on.
  let marks = [];

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

  // Where an opening thread lands: the first message carrying a search hit
  // when you got here from a search, the newest one otherwise. Re-derived on
  // every call rather than resolved once, because iframe bodies only report
  // their hits as they load — each load nudges the target forward.
  function landOn() {
    const host = document.getElementById("reader-content");
    const pane = document.querySelector(".reading-pane");
    if (!host || !pane) return;
    const el = (marks.length && host.querySelector(".thread-msg.has-hit")) || host.lastElementChild;
    if (!el) return;
    const bar = document.getElementById("reader-bar");
    const top = el.getBoundingClientRect().top - pane.getBoundingClientRect().top;
    pane.scrollTop = Math.max(0, pane.scrollTop + top - (bar ? bar.offsetHeight : 0) - 10);
  }

  function mountFrame(container, html, onHit) {
    const p = pin;
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
        frame.style.height = (doc.documentElement.scrollHeight + 4) + "px";
        // Once you click inside a message body the iframe owns the keyboard and
        // shortcuts would silently stop working. Forward them back out — this
        // reaches across only because the sandbox allows same-origin.
        if (App.keys) doc.addEventListener("keydown", App.keys.handle);
      } catch (_) { frame.style.height = "400px"; }
      if (p === pin) landOn();
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
    { act: "flag", icon: "flag", title: "Flag" },
    // Only drawn once a Meerato URL is configured — see App.tasks. An install
    // with no task tracker should not carry a button that can only fail.
    { sep: true, tasks: true },
    { act: "task", icon: "task", title: "Add Task", tasks: true },
  ];

  // The bar at the top always means "the newest message". This row means "this
  // one" — it is how you reply to something halfway up a long thread without
  // the reply silently going to the last mail instead.
  function msgToolbar(m) {
    return `<div class="msg-toolbar" data-msg="${m.id}">
      <button class="tb-btn" data-act="reply" title="Reply">${App.icon("reply", 16)} Reply</button>
      <button class="tb-btn" data-act="replyall" title="Reply All">${App.icon("replyAll", 16)} Reply All</button>
      <button class="tb-btn" data-act="forward" title="Forward">${App.icon("forward", 16)} Forward</button>
      ${tasksOn() ? `<button class="tb-btn" data-act="task" title="Add Task"
        >${App.icon("task", 16)} Add Task</button>` : ""}
      <span class="tb-spacer"></span>
      <button class="tb-btn ${m.flagged ? "on" : ""}" data-act="flag" title="Flag">${App.icon("flag", 16, m.flagged)}</button>
      <button class="tb-btn" data-act="move" title="Move to folder">${App.icon("move", 16)}</button>
      <button class="tb-btn" data-act="archive" title="Archive">${App.icon("archive", 16)}</button>
      <button class="tb-btn" data-act="trash" title="Delete">${App.icon("trash", 16)}</button>
      <button class="tb-btn" data-act="unread" title="Mark as unread">${App.icon("markunread", 16)}</button>
    </div>`;
  }

  function tasksOn() { return !!(App.tasks && App.tasks.enabled()); }

  function renderBar() {
    const bar = document.getElementById("reader-bar");
    const m = targetMsg();
    bar.innerHTML = BAR_BUTTONS.filter((b) => !b.tasks || tasksOn()).map((b) => {
      if (b.sep) return `<span class="tb-sep"></span>`;
      const flagged = b.act === "flag" && m && m.flagged;
      const off = b.act !== "new" && !m;
      return `<button class="tb-btn${flagged ? " on" : ""}" data-act="${b.act}"
        title="${b.title}" aria-label="${b.title}"${off ? " disabled" : ""}
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
      ? folders.map((mb) => `<button class="move-item" data-mailbox="${mb.id}">
          <span class="mm-icon">${App.icon(App.roleIcon(mb.role), 15)}</span>
          <span class="mm-name">${App.esc(mb.display_name)}</span></button>`).join("")
      : `<div class="move-empty">No other folders</div>`;
    document.body.appendChild(el);

    // Right-aligned under the button, nudged back on screen if the folder list
    // is long enough to run off the bottom.
    const r = anchor.getBoundingClientRect();
    el.style.top = Math.min(r.bottom + 4, window.innerHeight - el.offsetHeight - 8) + "px";
    el.style.left = Math.max(8, Math.min(r.left, window.innerWidth - el.offsetWidth - 8)) + "px";

    el.addEventListener("click", (e) => {
      const item = e.target.closest("[data-mailbox]");
      if (!item) return;
      const mailboxId = Number(item.dataset.mailbox);
      closeMoveMenu();
      moveThreadTo(mailboxId);
    });

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

  // Like archive and trash, a move takes the whole conversation with it — each
  // message leaves its own folder, so no reply is stranded behind.
  async function moveThreadTo(mailboxId) {
    try {
      const targets = moveTargets();
      if (!targets.length) throw new Error("No source mailbox for this message");
      for (const t of targets) await App.api.moveMsg(t.m.id, mailboxId, t.source);
      await afterRemove(targets.map((t) => t.m));
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
  async function removeThread(act) {
    const msgs = currentThread ? currentThread.messages : [];
    if (!msgs.length) return;
    const accountId = msgs[0].account_id;
    // Only a threaded conversation has an id to act on. A message that never
    // got threaded stands alone, so its single placement is the whole job.
    if (currentThread.thread_id) {
      if (act === "archive") await App.api.archiveThread(currentThread.thread_id, accountId);
      else await App.api.trashThread(currentThread.thread_id, accountId);
      await afterRemove(msgs.slice());
      return;
    }
    const targets = moveTargets();
    if (!targets.length) throw new Error("No source mailbox for this message");
    for (const t of targets) {
      if (act === "archive") await App.api.archiveMsg(t.m.id, t.source);
      else await App.api.trashMsg(t.m.id, t.source);
    }
    await afterRemove(targets.map((t) => t.m));
  }

  async function handleAction(act, m, anchor) {
    try {
      if (act === "new") return App.compose.openNew();
      if (!m) return;
      if (act === "move") return anchor && openMoveMenu(m, anchor);
      if (act === "task") return App.tasks.open(m);
      if (act === "reply") return App.compose.openReply(m.id, "reply");
      if (act === "replyall") return App.compose.openReply(m.id, "replyall");
      if (act === "forward") return App.compose.openReply(m.id, "forward");
      if (act === "flag") { m.flagged = !m.flagged; await App.api.flagMsg(m.id, m.flagged); return rerender(); }
      if (act === "unread") { m.seen = false; await App.api.markSeen(m.id, false); return; }
      if (act === "archive" || act === "trash") return removeThread(act);
    } catch (e) { alert(e.message || "Action failed"); }
  }

  async function afterRemove(removed) {
    const gone = new Set(removed.map((x) => x.id));
    currentThread.messages = currentThread.messages.filter((x) => !gone.has(x.id));
    const emptied = !currentThread.messages.length;
    if (emptied) clear(); else rerender();
    if (!App.shell) return;
    await App.shell.reloadList();
    // Clearing the conversation you were reading would leave the pane blank and
    // the keyboard flow stranded. The list keeps the cursor on the slot the row
    // vacated, so opening it lands on the next mail down — and on nothing at
    // all when the folder is empty, which is what draws the all-done state.
    if (emptied) App.list.openFocused();
  }

  // Every message in the thread is drawn in full — no "N earlier messages" to
  // unfold. The head stays a toggle so a long quoted chain can still be folded
  // away one card at a time.
  function renderMsg(m) {
    const shut = collapsed.has(m.id);
    const wrap = document.createElement("div");
    wrap.className = "thread-msg" + (shut ? " collapsed" : "");
    const av = App.avatarColor(m.from_addr);
    const showImages = imagesFor.has(m.id);
    const names = (kind) => (m.recipients[kind] || []).map((r) => App.esc(r.name || r.address)).join(", ");
    const to = names("to");
    // Cc is part of who was addressed, so it belongs next to To rather than
    // behind a details toggle — a recipient you cannot see is one you cannot
    // decide to keep on a reply.
    const cc = names("cc");
    const snippet = m.body_text ? m.body_text.slice(0, 140) : "";

    wrap.innerHTML = `
      <div class="msg-head" role="button" tabindex="0"
           aria-expanded="${shut ? "false" : "true"}"
           title="${shut ? "Expand" : "Collapse"} this message">
        <div class="from-row">
          <div class="avatar" style="background:${av}">${App.esc(App.initials(m.from_name, m.from_addr))}</div>
          <div class="from-meta">
            <div class="from-name${shut ? "" : " selectable"}">${App.esc(m.from_name || m.from_addr)}</div>
            <div class="from-detail${shut ? "" : " selectable"}">${shut ? App.esc(snippet)
              : App.esc(m.from_addr) + (to ? " · to " + to : "") + (cc ? " · cc " + cc : "")}</div>
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

    wrap.insertAdjacentHTML("beforeend", msgToolbar(m));
    wrap.querySelector(".msg-toolbar").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-act]");
      if (btn) handleAction(btn.dataset.act, m, btn);
    });

    if (m.remote_blocked && !showImages) {
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
    if (m.body_html) {
      mountFrame(body, m.body_html, () => wrap.classList.add("has-hit"));
    } else if (m.body_text) {
      // Plain-text mail is rendered as markdown: headings, lists, emphasis and
      // `>` quote levels all read better, and text that uses none of it comes
      // out looking exactly as it did before. No iframe needed — the parser
      // escapes everything and builds the HTML itself, so there is nothing of
      // the sender's to sanitize.
      body.className = "msg-body-md";
      body.innerHTML = App.markdown.toHtml(m.body_text);
      if (App.highlight.mark(body, marks)) wrap.classList.add("has-hit");
    } else {
      body.className = "msg-body-text";
      body.textContent = "(no content)";
    }
    wrap.appendChild(body);

    if (m.attachments && m.attachments.length) {
      const at = document.createElement("div");
      at.className = "attachments";
      at.innerHTML = m.attachments.map((a) => {
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
        return `<a class="attachment-chip${a.has_thumb ? " has-thumb" : ""}" ${link}
            title="${App.esc(a.filename)}">
          ${face}
          <span class="att-meta">
            <span class="att-name">${App.esc(a.filename)}</span>
            <span class="att-size">${App.fmtSize(a.size)}</span>
          </span>
        </a>`;
      }).join("");
      m.attachments.forEach((a, i) => {
        if (a.match_contexts && a.match_contexts.length) at.children[i].classList.add("has-hit");
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
    // The menu is anchored to a toolbar button that is about to be replaced.
    closeMoveMenu();
    pin = pinLast ? {} : null;
    renderBar();
    const host = document.getElementById("reader-content");
    const empty = document.getElementById("reader-empty");
    if (!currentThread) { host.hidden = true; empty.hidden = false; renderEmpty(); return; }
    empty.hidden = true; host.hidden = false;
    host.innerHTML = "";
    for (const m of currentThread.messages) host.appendChild(renderMsg(m));
    // Right away for text-only mail; the iframes redo it as they measure up.
    if (pinLast) landOn();
  }

  async function openThread(threadId, accountId, focusId) {
    const request = ++openRequest;
    // Asked for with the search still in hand: the server has to find the hits
    // in extracted attachment text, which the client never sees.
    const search = App.search && App.search.isActive() ? App.search.query() : null;
    const data = await App.api.thread(threadId, accountId, false, search);
    if (request !== openRequest) return;
    marks = search ? App.highlight.patterns(search.q, search.mode) : [];
    currentThread = data;
    imagesFor = new Set();
    // Whole conversation open, oldest to newest — folding is something you ask
    // for per message, not a state a thread arrives in.
    collapsed = new Set();
    rerender(true);
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

  // `redraw` is exported so App.tasks can put the Add Task buttons up (or take
  // them down) the moment the Meerato URL changes, rather than at the next
  // thread open — both the bar and the per-message toolbars carry one.
  return { openThread, clear, action, scrollBy, scrollEnd, setKeyFocus, renderEmpty,
    redraw: () => rerender(), isOpen: () => !!currentThread,
    // For the composer's "Send & Archive". Errors are the caller's to report:
    // the mail is already gone by then, so a failure here is not a failed send.
    archiveThread: () => (currentThread ? removeThread("archive") : Promise.resolve()) };
})();
