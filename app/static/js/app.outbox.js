/* meerail Outbox: the mail this app has written and no mail server has taken.

   It is not an IMAP folder — there is no such folder on the server, and the
   messages in here have no UID and no place in a thread — but it is a folder to
   whoever wrote them, so it renders as one: a row in the sidebar, a list in the
   middle pane, the message in the reading pane. Everything else in the UI reads
   /api/messages; this reads /api/outbox instead, which is the only reason it
   owns its own rendering rather than going through App.list and App.reader.

   What it is for is the second half of the folder, not the first. A queued
   message is normally on its way out within a second or two, and nobody needs a
   screen for that. The case this exists for is the one where it isn't: a wrong
   SMTP port, a Bridge that is signed out, a laptop that has been shut since
   Friday. Until now that looked exactly like a message already delivered — mail
   piling up in the queue with, in the words of the report, "no way to know that
   they were there and why they are not being sent". So every row says how many
   attempts it has cost and what the last one said, and the message itself opens
   onto the full error.

   Nothing in here is a lost message. The agent retries forever (agent/actions.py
   says why), so the wording throughout is "not sent yet" and never "not sent" —
   the two buttons are the exceptions the user asks for by pressing them. */

App.outbox = (function () {
  let rows = [];             // last /api/outbox payload
  let openId = null;         // the message showing in the reading pane
  let request = 0;           // guards against an older fetch landing last
  let busy = false;          // a retry/discard is in flight — see actionBar()

  const $ = (s) => document.querySelector(s);
  const byId = (id) => rows.find((r) => r.id === id) || null;

  // --- Words ---------------------------------------------------------------
  // Said the same way in the row, the header and the sidebar, because they are
  // the same fact seen from three distances.

  function stuck(r) { return !!r.error || !r.queued; }

  function stateLabel(r) {
    if (!r.queued) return "Not queued — an older agent gave up on it";
    if (r.error) return `Not going out — ${r.attempts} failed attempt${r.attempts === 1 ? "" : "s"}`;
    return r.attempts ? `Waiting — ${r.attempts} attempt${r.attempts === 1 ? "" : "s"} so far`
                      : "Waiting to be sent";
  }

  // "in 4m". The negative case matters: an overdue action is due *now*, and
  // saying "in -2m" would read as a clock problem rather than as a queue the
  // agent has not got to yet.
  function untilText(iso) {
    const d = App.utcDate(iso);
    if (d === null) return "";
    const s = Math.round((d.getTime() - Date.now()) / 1000);
    if (s <= 0) return "due now";
    if (s < 60) return `in ${s}s`;
    if (s < 3600) return `in ${Math.round(s / 60)}m`;
    return `in ${Math.round(s / 3600)}h`;
  }

  function nextText(r) {
    if (!r.queued) return "";
    if (!r.attempts) return "on the agent's next pass";
    return r.next_attempt_at ? `next attempt ${untilText(r.next_attempt_at)}` : "due now";
  }

  // nextText is written to follow a dash mid-sentence ("Waiting — next attempt
  // in 8m"); this is for the one place it starts a sentence of its own.
  function cap(s) { return s ? s[0].toUpperCase() + s.slice(1) : s; }

  // --- List ----------------------------------------------------------------

  function listRow(r) {
    const el = document.createElement("div");
    el.className = "msg-row ob-row" + (r.id === openId ? " active" : "") + (stuck(r) ? " stuck" : "");
    el.dataset.id = r.id;
    const to = r.to.concat(r.cc, r.bcc);
    el.innerHTML = `
      <span class="acct-stripe" style="background:${App.esc(r.account_color)}"></span>
      <div class="msg-main">
        <div class="msg-line1">
          <span class="msg-sender">${App.esc(to.join(", ") || "(no recipients)")}</span>
          <span class="msg-date">${App.esc(App.fmtDate(r.created_at))}</span>
        </div>
        <div class="msg-subject">${App.esc(r.subject || "(no subject)")}</div>
        <div class="msg-snippet">${App.esc(r.snippet || "")}</div>
        <div class="ob-row-state">
          <span class="ob-chip${stuck(r) ? " stuck" : ""}">
            ${App.icon(stuck(r) ? "warning" : "sent", 11)}${App.esc(stateLabel(r))}</span>
          ${r.attachment_count ? `<span class="attach-glyph">${App.icon("paperclip", 12)}</span>` : ""}
        </div>`;
    el.addEventListener("click", () => {
      App.mobile.show("reader");
      open(r.id);
    });
    return el;
  }

  function renderList() {
    const host = document.getElementById("message-list");
    host.innerHTML = "";
    if (!rows.length) {
      // The reward state, and it has to be unambiguous: an empty outbox means
      // everything written here has actually left the machine.
      host.innerHTML = `<div class="list-empty">Nothing waiting.<br>
        Everything written here has been handed to a mail server.</div>`;
      return;
    }
    const frag = document.createDocumentFragment();
    for (const r of rows) frag.appendChild(listRow(r));
    host.appendChild(frag);
  }

  async function load() {
    const mine = ++request;
    let data;
    try {
      data = await App.api.outbox();
    } catch (e) {
      if (mine !== request) return;
      document.getElementById("message-list").innerHTML =
        `<div class="list-empty">Could not load the outbox: ${App.esc(e.message)}</div>`;
      return;
    }
    if (mine !== request) return;
    rows = data.rows || [];
    renderList();
    // A background refresh lands here too (mail sends, the agent tries again),
    // so the pane has to be brought along rather than left showing a message
    // that has since gone out.
    if (openId !== null) refreshDetail();
  }

  // --- Reading pane --------------------------------------------------------

  function field(label, value) {
    return value ? `<div class="ob-field"><span>${App.esc(label)}</span><div>${App.esc(value)}</div></div>` : "";
  }

  function actionBar(r) {
    // "Try now" only says anything a waiting message does not already do — the
    // agent is coming for it either way — so it is offered on everything but
    // reads as the answer to "I just fixed the port".
    return `<div class="ob-actions">
      <button class="ob-btn" data-ob="retry"${busy ? " disabled" : ""}
        title="Ask the agent to try this message now instead of at the end of its backoff"
        >${App.icon("refresh", 15)} Try now</button>
      <button class="ob-btn danger" data-ob="discard"${busy ? " disabled" : ""}
        title="Take this message out of the queue — it will never be sent"
        >${App.icon("trash", 15)} Delete</button>
      <span class="ob-action-status" id="ob-action-status"></span>
    </div>`;
  }

  function renderDetail(m) {
    const host = document.getElementById("reader-content");
    const empty = document.getElementById("reader-empty");
    empty.hidden = true;
    host.hidden = false;

    // The error is the reason this screen exists, so it goes above the message
    // rather than under it: whoever opened this row is not here to re-read
    // their own mail.
    const why = m.error
      ? `<div class="ob-error">
           <div class="ob-error-head">${App.icon("warning", 15)}
             <span>${App.esc(stateLabel(m))}</span></div>
           <pre>${App.esc(m.error)}</pre>
           <div class="ob-error-sub">Still queued and still being retried — nothing is lost.
             The agent backs off between attempts and sends as soon as the cause above is
             fixed. ${App.esc(cap(nextText(m)))}.</div>
         </div>`
      : !m.queued
        ? `<div class="ob-error">
             <div class="ob-error-head">${App.icon("warning", 15)}
               <span>${App.esc(stateLabel(m))}</span></div>
             <div class="ob-error-sub">This message was retired by a version of the agent that
               gave up after five attempts. The message itself is still here — press
               <strong>Try now</strong> to put it back in the queue.</div>
           </div>`
        : `<div class="ob-waiting">${App.icon("sent", 15)}
             <span>${App.esc(stateLabel(m))} — ${App.esc(nextText(m))}. Sending happens in the
             agent, which relays it over SMTP the next time it reaches a mail server.</span>
           </div>`;

    const size = m.size_bytes ? ` · ${Math.max(1, Math.round(m.size_bytes / 1024))} kB` : "";
    host.innerHTML = `<div class="ob-detail">
      ${why}
      ${actionBar(m)}
      <div class="ob-head">
        <div class="ob-subject">${App.esc(m.subject || "(no subject)")}</div>
        <div class="ob-meta">
          ${field("From", m.from_address || m.account_email)}
          ${field("To", m.to.join(", "))}
          ${field("Cc", m.cc.join(", "))}
          ${field("Bcc", m.bcc.join(", "))}
          ${field("Written", App.fmtDateFull(m.created_at))}
          ${field("Last attempt", m.last_attempt_at ? App.fmtDateFull(m.last_attempt_at) : "")}
          ${field("Attachments", m.attachment_count ? String(m.attachment_count) : "")}
        </div>
        <div class="ob-flags">${m.html ? "Goes out as HTML" : "Goes out as plain text"}${App.esc(size)}</div>
      </div>
      <div class="ob-body">${App.esc(m.body_text || "")}</div>
    </div>`;

    host.querySelectorAll("[data-ob]").forEach((btn) => {
      btn.addEventListener("click", () => act(btn.dataset.ob, m.id));
    });
  }

  // Gone from the list between one refresh and the next means one of two things,
  // and they deserve different words: it went out, or it was discarded from
  // another window.
  function renderGone() {
    const host = document.getElementById("reader-content");
    document.getElementById("reader-empty").hidden = true;
    host.hidden = false;
    host.innerHTML = `<div class="ob-detail"><div class="ob-waiting">
      ${App.icon("sent", 15)}<span>This message has left the outbox — it has been sent, or
      removed from the queue. It appears in Sent once the agent has read the server's copy
      of it back.</span></div></div>`;
  }

  async function open(id) {
    openId = id;
    document.querySelectorAll(".ob-row.active").forEach((n) => n.classList.remove("active"));
    const el = document.querySelector(`.ob-row[data-id="${id}"]`);
    if (el) el.classList.add("active");
    await refreshDetail();
  }

  async function refreshDetail() {
    if (openId === null) return;
    if (!byId(openId)) return renderGone();
    let m;
    try {
      m = await App.api.outboxMessage(openId);
    } catch (_) {
      return renderGone();
    }
    if (openId !== m.id) return;   // opened something else while this was in flight
    renderDetail(m);
  }

  // --- The two verbs -------------------------------------------------------

  async function act(what, id) {
    const status = $("#ob-action-status");
    if (what === "discard" && !confirm(
        "Delete this message without sending it?\n\nIt is not sent anywhere and cannot be got back.")) {
      return;
    }
    busy = true;
    if (status) status.textContent = what === "retry" ? "Asking the agent…" : "Deleting…";
    try {
      if (what === "retry") await App.api.outboxRetry(id);
      else await App.api.outboxDiscard(id);
    } catch (e) {
      busy = false;
      if (status) { status.textContent = e.message || "Failed"; status.classList.add("error"); }
      return;
    }
    busy = false;
    if (what === "discard") {
      openId = null;
      App.reader.clear();
      App.mobile.show("list");
    }
    // Both verbs change the sidebar count as well as the list, and the retry
    // may have already sent the message by the time the next refresh lands.
    await App.shell.reloadList();
    App.status.refresh();
  }

  function clear() { openId = null; }

  return { load, open, clear, count: () => rows.length,
           openId: () => openId };
})();
