/* meerail reader: renders a conversation thread in the reading pane. */

App.reader = (function () {
  let currentThread = null;
  let expanded = new Set();     // message ids shown expanded
  let imagesFor = new Set();    // message ids with remote images loaded

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
      </style></head><body>${html || ""}</body></html>`;
  }

  function mountFrame(container, html) {
    const frame = document.createElement("iframe");
    frame.className = "msg-body-frame";
    frame.setAttribute("sandbox", "allow-same-origin allow-popups allow-popups-to-escape-sandbox");
    frame.scrolling = "no";
    frame.srcdoc = frameDoc(html);
    frame.addEventListener("load", () => {
      try {
        const doc = frame.contentDocument;
        frame.style.height = (doc.documentElement.scrollHeight + 4) + "px";
      } catch (_) { frame.style.height = "400px"; }
    });
    container.appendChild(frame);
  }

  function toolbar(m) {
    return `<div class="reader-toolbar">
      <button class="tb-btn" data-act="reply" title="Reply">${App.icon("reply", 16)} Reply</button>
      <button class="tb-btn" data-act="replyall" title="Reply All">${App.icon("replyAll", 16)} Reply All</button>
      <button class="tb-btn" data-act="forward" title="Forward">${App.icon("forward", 16)} Forward</button>
      <span class="tb-spacer"></span>
      <button class="tb-btn ${m.flagged ? "on" : ""}" data-act="flag" title="Flag">${App.icon("flag", 16, m.flagged)}</button>
      <button class="tb-btn" data-act="archive" title="Archive">${App.icon("archive", 16)}</button>
      <button class="tb-btn" data-act="trash" title="Delete">${App.icon("trash", 16)}</button>
      <button class="tb-btn" data-act="unread" title="Mark as unread">${App.icon("markunread", 16)}</button>
    </div>`;
  }

  async function handleAction(act, m) {
    try {
      if (act === "reply") return App.compose.openReply(m.id, "reply");
      if (act === "replyall") return App.compose.openReply(m.id, "replyall");
      if (act === "forward") return App.compose.openReply(m.id, "forward");
      if (act === "flag") { m.flagged = !m.flagged; await App.api.flagMsg(m.id, m.flagged); return rerender(); }
      if (act === "unread") { m.seen = false; await App.api.markSeen(m.id, false); return; }
      const selectedMailbox = App.shell && App.shell.currentMailboxId();
      const selectedLocation = m.locations.find((loc) => loc.mailbox_id === selectedMailbox);
      const inboxLocation = m.locations.find((loc) => loc.role === "inbox");
      const sourceMailbox = (selectedLocation || inboxLocation || m.locations[0] || {}).mailbox_id;
      if ((act === "archive" || act === "trash") && !sourceMailbox) throw new Error("No source mailbox for this message");
      if (act === "archive") { await App.api.archiveMsg(m.id, sourceMailbox); return afterRemove(m); }
      if (act === "trash") { await App.api.trashMsg(m.id, sourceMailbox); return afterRemove(m); }
    } catch (e) { alert(e.message || "Action failed"); }
  }

  function afterRemove(m) {
    currentThread.messages = currentThread.messages.filter((x) => x.id !== m.id);
    if (!currentThread.messages.length) clear(); else rerender();
    if (App.shell) App.shell.reloadList();
  }

  function renderExpanded(m) {
    const wrap = document.createElement("div");
    wrap.className = "thread-msg";
    const av = App.avatarColor(m.from_addr);
    const showImages = imagesFor.has(m.id);
    const to = (m.recipients.to || []).map((r) => App.esc(r.name || r.address)).join(", ");

    wrap.innerHTML = `
      <div class="msg-head">
        <div class="from-row">
          <div class="avatar" style="background:${av}">${App.esc(App.initials(m.from_name, m.from_addr))}</div>
          <div class="from-meta">
            <div class="from-name">${App.esc(m.from_name || m.from_addr)}</div>
            <div class="from-detail">${App.esc(m.from_addr)}${to ? " · to " + to : ""}</div>
          </div>
          <div class="msg-date-full">${App.esc(App.fmtDateFull(m.date))}</div>
        </div>
      </div>
      ${toolbar(m)}`;
    wrap.querySelector(".reader-toolbar").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-act]");
      if (btn) handleAction(btn.dataset.act, m);
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
      mountFrame(body, m.body_html);
    } else {
      body.className = "msg-body-text";
      body.textContent = m.body_text || "(no content)";
    }
    wrap.appendChild(body);

    if (m.attachments && m.attachments.length) {
      const at = document.createElement("div");
      at.className = "attachments";
      at.innerHTML = m.attachments.map((a) =>
        `<a class="attachment-chip" href="/api/attachments/${a.id}" download="${App.esc(a.filename)}">
          ${App.icon("paperclip", 15)}
          <span class="att-name">${App.esc(a.filename)}</span>
          <span class="att-size">${App.fmtSize(a.size)}</span>
        </a>`).join("");
      wrap.appendChild(at);
    }
    return wrap;
  }

  function renderCollapsed(m) {
    const row = document.createElement("div");
    row.className = "collapsed-preview";
    const av = App.avatarColor(m.from_addr);
    row.innerHTML = `
      <div class="avatar" style="background:${av};width:28px;height:28px;font-size:.8rem">
        ${App.esc(App.initials(m.from_name, m.from_addr))}</div>
      <span class="from-name">${App.esc(m.from_name || m.from_addr)}</span>
      <span class="collapsed-snippet">${App.esc(m.body_text ? m.body_text.slice(0, 120) : "")}</span>
      <span class="msg-date">${App.esc(App.fmtDate(m.date))}</span>`;
    row.addEventListener("click", () => { expanded.add(m.id); rerender(); });
    return row;
  }

  function rerender() {
    const host = document.getElementById("reader-content");
    const empty = document.getElementById("reader-empty");
    if (!currentThread) { host.hidden = true; empty.hidden = false; return; }
    empty.hidden = true; host.hidden = false;
    host.innerHTML = "";
    for (const m of currentThread.messages) {
      host.appendChild(expanded.has(m.id) ? renderExpanded(m) : renderCollapsed(m));
    }
    host.scrollTop = 0;
  }

  async function openThread(threadId, accountId, focusId) {
    const data = await App.api.thread(threadId, accountId, false);
    currentThread = data;
    imagesFor = new Set();
    // Expand the focused message and the most recent one; collapse the rest.
    expanded = new Set();
    if (data.messages.length) expanded.add(data.messages[data.messages.length - 1].id);
    if (focusId) expanded.add(focusId);
    rerender();
    // Opening a conversation marks its messages read (write-back via the agent).
    for (const m of data.messages) {
      if (!m.seen) { m.seen = true; App.api.markSeen(m.id, true).catch(() => {}); }
    }
  }

  function clear() { currentThread = null; rerender(); }

  return { openThread, clear };
})();
