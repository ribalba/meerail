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
  let busy = false;          // a retry/cancel/discard is in flight — see actionBar()
  let ticker = null;         // retimes a delayed send's countdown — see counting()

  const $ = (s) => document.querySelector(s);
  const byId = (id) => rows.find((r) => r.id === id) || null;

  // --- Words ---------------------------------------------------------------
  // Said the same way in the row, the header and the sidebar, because they are
  // the same fact seen from three distances.

  function stuck(r) { return !r.held && (!!r.error || !r.queued); }

  // Held first, and before the error: a cancelled message is not failing, it is
  // waiting for its author, and saying "not going out — 3 failed attempts"
  // about mail they stopped themselves is answering a question nobody asked.
  function stateLabel(r) {
    // Ahead of everything, including a stale error from a previous attempt:
    // this one is happening right now, and it is the only state in the list
    // that is about to stop being true on its own.
    if (r.sending) return "Being sent right now";
    // Parked like a cancelled message and the opposite of one: nobody stopped
    // this, the server stopped answering. Saying "cancelled" would be a lie in
    // the one direction that matters — it may already have arrived.
    if (r.delivery_unknown) return "May already have been sent";
    if (r.held) return "Cancelled — not being sent";
    if (!r.queued) return "Not queued — an older agent gave up on it";
    if (r.error) return `Not going out — ${r.attempts} failed attempt${r.attempts === 1 ? "" : "s"}`;
    if (r.send_at) return cap(sendingText(r.send_at));
    return r.attempts ? `Waiting — ${r.attempts} attempt${r.attempts === 1 ? "" : "s"} so far`
                      : "Waiting to be sent";
  }

  // "sending in 40s", and "sending now" once the wait is over — never "sending
  // due now", which is untilText's phrasing for a retry that is overdue and
  // means something else.
  function sendingText(iso) {
    const t = untilText(iso);
    return t === "due now" ? "sending now" : `sending ${t}`;
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
    if (r.held || !r.queued) return "";
    // A message inside its send delay has not been tried and is not going to
    // be; "next attempt" would be the wrong noun for the first one.
    if (r.send_at) return sendingText(r.send_at);
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
            ${App.icon(stuck(r) ? "warning" : "sent", 11)}<span class="ob-when"
              >${App.esc(stateLabel(r))}</span></span>
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

  // --- The countdown -------------------------------------------------------
  // A delayed send is the one thing in this folder that changes on its own
  // between refreshes, and a window that says "sending in 40s" for two minutes
  // is worse than one that says nothing: the number is the whole reason to look
  // at the screen at all. So the labels are retimed in place every second —
  // only their text, never the rows, because rebuilding the list under a reader
  // who is scrolling it is the cure being worse than the disease.

  // Only while there is a number still going down. A delay that has run out
  // leaves the row saying "sending now" until the agent's pass takes it away,
  // and nothing about that sentence changes with the clock.
  function counting() {
    return rows.some((r) => !r.held && r.send_at && App.utcDate(r.send_at) > Date.now());
  }

  function tick() {
    for (const r of rows) {
      const el = document.querySelector(`.ob-row[data-id="${r.id}"] .ob-when`);
      if (el) el.textContent = stateLabel(r);
    }
    const open = openId === null ? null : byId(openId);
    const el = document.querySelector(".ob-detail .ob-when");
    if (el && open) el.textContent = stateLabel(open);
    if (!counting()) stopTicker();
  }

  function startTicker() {
    // A countdown nobody is looking at does not need retiming every second;
    // stateLabel is recomputed from send_at on the next render anyway, so the
    // labels are right again the moment the app comes back.
    if (App.power && App.power.isSuspended()) return;
    if (ticker === null && counting()) ticker = setInterval(tick, 1000);
  }

  function stopTicker() {
    if (ticker !== null) { clearInterval(ticker); ticker = null; }
  }

  // Registered at load rather than from an init(): this module has no init, and
  // the hooks are safe to hold whether or not the outbox is ever opened.
  if (App.power) {
    App.power.whenSuspended(stopTicker);
    App.power.whenResumed(startTicker);
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
    startTicker();
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
    // The same verb, named for the situation it is being pressed in: a message
    // that is failing gets tried again, a message that is waiting on purpose
    // gets sent. Offered on everything, because "go now" is a sentence you can
    // always say to a queue.
    const going = !!r.send_at || r.held;
    const send = going
      ? { label: "Send now", icon: "sent",
          title: "Send this message now instead of at the end of its delay" }
      : { label: "Try now", icon: "refresh",
          title: "Ask the agent to try this message now instead of at the end of its backoff" };

    // While an agent is inside the SMTP conversation for this message, none of
    // the three verbs is true any more: it is going, and the only honest thing
    // the screen can do is say so. The server refuses them in this state
    // regardless of what is on screen (app/routers/outbox.py) — this is so the
    // refusal is not the first anyone hears of it.
    const off = busy || r.sending ? " disabled" : "";
    const sendingTitle = r.sending ? " title=\"This message is being sent right now\"" : "";

    // Cancel is only offered while there is still something to cancel. A held
    // message has already been stopped, and saying so twice would make the
    // button look like it had not worked the first time.
    const cancel = r.held ? "" : `
      <button class="ob-btn" data-ob="cancel"${off}
        title="Stop this message going out — it stays here until you send it"
        >${App.icon("close", 15)} Cancel send</button>`;

    return `<div class="ob-actions"${sendingTitle}>
      <button class="ob-btn" data-ob="retry"${off}
        title="${App.esc(send.title)}"
        >${App.icon(send.icon, 15)} ${App.esc(send.label)}</button>
      ${cancel}
      <button class="ob-btn danger" data-ob="discard"${off}
        title="Take this message out of the queue — it will never be sent"
        >${App.icon("trash", 15)} Delete</button>
      <span class="ob-action-status" id="ob-action-status"
        >${r.sending ? "Being sent right now…" : ""}</span>
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
    const why = m.delivery_unknown
      // The one state where sending again is a real decision rather than an
      // obvious one, so the screen says what is and is not known and leaves it
      // to the reader.
      ? `<div class="ob-waiting">${App.icon("warning", 15)}
           <span>This message was handed to the mail server, and the connection failed
           before the server confirmed it. It may have been delivered and it may not,
           and there is no way to ask. It has <strong>not</strong> been retried, because
           sending again would deliver a second copy if the first one arrived —
           <strong>Send now</strong> does exactly that, if you would rather risk the
           duplicate than the silence. What the connection said:</span>
         </div>${m.error ? `<pre class="ob-held-error">${App.esc(m.error)}</pre>` : ""}`
      : m.held
      // A cancelled message can still carry the error from before it was
      // cancelled, and that error is often the reason it was: it stays on
      // screen, under a banner that no longer calls it a fault.
      ? `<div class="ob-waiting">${App.icon("close", 15)}
           <span>${App.esc(stateLabel(m))}. It is still here, with everything it was
           addressed to — nothing goes out until you press <strong>Send now</strong>.
           ${m.error ? "The last attempt before it was stopped said:" : ""}</span>
         </div>${m.error ? `<pre class="ob-held-error">${App.esc(m.error)}</pre>` : ""}`
      : m.send_at
      ? `<div class="ob-waiting">${App.icon("sent", 15)}
           <span><span class="ob-when">${App.esc(stateLabel(m))}</span>. Written messages
           wait here before they go, so
           there is time to change your mind — <strong>Send now</strong> skips the wait,
           <strong>Cancel send</strong> stops it.</span>
         </div>`
      : m.error
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

  // --- The three verbs -----------------------------------------------------

  const WORKING = { retry: "Asking the agent…", cancel: "Stopping it…", discard: "Deleting…" };

  async function act(what, id) {
    const status = $("#ob-action-status");
    // Only the irreversible one asks. Cancelling is undone by the button next
    // to it, and putting a dialog in front of a send someone is trying to catch
    // in the next twenty seconds would be the one place it actually costs them.
    if (what === "discard" && !confirm(
        "Delete this message without sending it?\n\nIt is not sent anywhere and cannot be got back.")) {
      return;
    }
    busy = true;
    if (status) status.textContent = WORKING[what] || "Working…";
    try {
      if (what === "retry") await App.api.outboxRetry(id);
      else if (what === "cancel") await App.api.outboxCancel(id);
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
    // Every verb changes the sidebar count as well as the list, and the retry
    // may have already sent the message by the time the next refresh lands.
    await App.shell.reloadList();
    App.status.refresh();
  }

  function clear() { openId = null; stopTicker(); }

  return { load, open, clear, count: () => rows.length,
           openId: () => openId };
})();
