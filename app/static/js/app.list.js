/* meerail message list: date-descending rows for the selected mailbox/scope. */

App.list = (function () {
  let activeId = null;

  function row(r, showAccount) {
    const el = document.createElement("div");
    el.className = "msg-row" + (r.seen ? "" : " unread") + (r.id === activeId ? " active" : "");
    el.dataset.id = r.id;
    el.dataset.thread = r.thread_id || "";

    const stripe = showAccount
      ? `<span class="acct-stripe" style="background:${App.esc(r.account_color)}"></span>` : "";
    const dot = r.seen ? "" : `<span class="unread-dot"></span>`;
    const flag = r.flagged ? `<span class="flag-dot">${App.icon("flag", 13, true)}</span>` : "";
    const attach = r.has_attachments ? `<span class="attach-glyph">${App.icon("paperclip", 12)}</span>` : "";
    const badge = r.thread_count > 1 ? `<span class="thread-badge">${r.thread_count}</span>` : "";

    el.innerHTML = `
      ${stripe}
      <div class="msg-gutter">${dot}</div>
      <div class="msg-main">
        <div class="msg-line1">
          <span class="msg-sender">${App.esc(r.from_name || r.from_addr || "Unknown")}</span>
          <span class="msg-date">${App.esc(App.fmtDate(r.date))}</span>
        </div>
        <div class="msg-subject">${App.esc(r.subject)}</div>
        <div class="msg-snippet">${App.esc(r.snippet || "")}</div>
        <div class="msg-meta">${badge}${flag}${attach}</div>
      </div>`;

    el.addEventListener("click", () => {
      activeId = r.id;
      document.querySelectorAll(".msg-row.active").forEach((n) => n.classList.remove("active"));
      el.classList.add("active");
      if (r.thread_id) App.reader.openThread(r.thread_id, r.account_id, r.id);
    });
    return el;
  }

  function render(rows, showAccount) {
    const host = document.getElementById("message-list");
    host.innerHTML = "";
    if (!rows.length) {
      host.innerHTML = `<div class="list-empty">No messages</div>`;
      return;
    }
    const frag = document.createDocumentFragment();
    for (const r of rows) frag.appendChild(row(r, showAccount));
    host.appendChild(frag);
  }

  function reset() { activeId = null; }

  return { render, reset };
})();
