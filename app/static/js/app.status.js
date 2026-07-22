/* meerail agent status: the toolbar health indicator, the warning strip, and
   the "Agent status" modal.

   The agent is a separate process that never talks to the server — it writes
   mail straight into Postgres. So there is nothing to ask "are you alive?";
   liveness is inferred by the server from what the agent has written, and
   /api/sync/status hands us the verdict.

   That is also why this polls. Every other live update in the app arrives over
   SSE, but a dead agent's defining symptom is that it sends nothing at all —
   silence is exactly what we need to notice, and no event will ever announce
   it. */

App.status = (function () {
  const $ = (s) => document.querySelector(s);

  // Slow enough to be invisible on a healthy system, fast enough that a crash
  // surfaces within a minute or so. Tightened while the modal is open, where
  // the user is actively watching the numbers, and while a pass is running,
  // where the sidebar bar is moving and 30s steps would read as frozen.
  //
  // Progress deliberately has no event of its own: core/ingest.set_progress
  // writes once per ingested batch, so a NOTIFY there would be a large share of
  // the channel's traffic for something only this panel reads. Polling faster
  // for the minute a pass lasts is the cheaper half of that trade.
  const POLL_IDLE = 30000;
  const POLL_OPEN = 8000;
  const POLL_ACTIVE = 8000;

  // The agent takes a moment to pick up a refresh request, and its first
  // progress write lands a moment after that. Without a floor the spinner would
  // stop on the very next poll and the click would look ignored.
  const NUDGE_MS = 6000;

  let latest = null;      // last /api/sync/status payload
  let timer = null;
  let spinUntil = 0;      // optimistic spinner floor, epoch ms

  const STATES = {
    ok:          { pill: "ok",    label: "syncing" },
    backfilling: { pill: "busy",  label: "backfilling" },
    failing:     { pill: "error", label: "failing" },
    offline:     { pill: "error", label: "offline" },
    never:       { pill: "warn",  label: "never seen" },
  };

  const meta = (state) => STATES[state] || STATES.never;

  function isBad(acc) { return acc.state === "failing" || acc.state === "offline"; }

  const num = (n) => (n || 0).toLocaleString();

  // Compact duration, for spans that run from seconds (an incremental pass) to
  // hours (a first backfill). relTime says "5 minutes ago"; this says "5m".
  function dur(seconds) {
    if (!(seconds >= 0)) return "—";
    if (seconds < 60) return `${Math.round(seconds)}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
    return `${Math.floor(seconds / 3600)}h ${Math.round((seconds % 3600) / 60)}m`;
  }

  // --- Sync progress ---
  // The agent writes its position once per ingested batch (agent/sync.py,
  // PassProgress). The bar is per folder because that is the only denominator
  // the agent knows without a second walk of the mailbox; the folder counter
  // beside it carries the rest of the story.
  function progressBlock(a) {
    const p = a.sync_progress;
    if (!p) return "";

    const from = p.started_at, to = p.updated_at;
    const elapsed = (new Date(to) - new Date(from)) / 1000;
    // Averaged over the pass, not instantaneous — the point is the ETA, and a
    // per-batch rate swings wildly as folders alternate between fresh mail and
    // already-held content.
    const rate = elapsed > 0 ? p.walked / elapsed : 0;

    if (!p.active) {
      // Between passes the counters still answer "did the last one do anything,
      // and how long did it take", which is the question after a slow backfill.
      return `<div class="ag-progress idle">
        <div class="ag-progress-head">
          <span>Last pass</span>
          <span class="ag-progress-when">${App.esc(App.relTime(p.finished_at || to))}</span>
        </div>
        <div class="ag-progress-sub">${num(p.walked)} scanned · ${num(p.stored)} new ·
          ${App.esc(dur(elapsed))}</div>
      </div>`;
    }

    const total = p.folder_total || 0;
    const done = Math.min(p.folder_done || 0, total);
    // A folder whose UID list has not come back yet has no total, and 0/0 would
    // render a full bar. Show it as indeterminate instead of lying.
    const known = total > 0;
    const pct = known ? Math.round((done / total) * 100) : 0;
    const left = known && rate > 0 ? dur((total - done) / rate) : null;

    return `<div class="ag-progress">
      <div class="ag-progress-head">
        <span>Folder ${p.folder_index} of ${p.folder_count}${
          p.folder ? ` · <b>${App.esc(p.folder)}</b>` : ""}</span>
        <span class="ag-progress-when">${known ? `${pct}%` : "scanning…"}</span>
      </div>
      <div class="ag-bar ${known ? "" : "indeterminate"}">
        <div class="ag-bar-fill"${known ? ` style="width:${pct}%"` : ""}></div>
      </div>
      <div class="ag-progress-sub">
        ${known ? `${num(done)} / ${num(total)} in this folder · ` : ""}
        ${num(p.walked)} scanned this pass · ${num(p.stored)} new
        ${rate > 0 ? ` · ${rate.toFixed(1)}/s` : ""}
        ${left ? ` · about ${App.esc(left)} left in this folder` : ""}
      </div>
    </div>`;
  }

  const accountList = () => (latest && latest.accounts) || [];

  // --- Attachment indexing ---
  // Deliberately not folded into the sync bars above. Extraction runs on the
  // agent's own thread (agent/sync.py run_indexer_forever) and is global rather
  // than per-account: mail can be entirely fetched with a long Tika queue still
  // draining behind it, and showing that as "syncing" reads as missing mail.
  const indexState = () => (latest && latest.indexing) || null;

  function indexing() {
    const ix = indexState();
    return !!(ix && ix.active);
  }

  function indexBlock() {
    const ix = indexState();
    if (!ix || !ix.active) return "";
    const total = ix.total || 0;
    // Settled, not just extracted: a file Tika refused is as finished as one it
    // read, and leaving failures out would park the bar short of full forever.
    const done = Math.min((ix.done || 0) + (ix.error || 0), total);
    const known = total > 0;
    const pct = known ? Math.round((done / total) * 100) : 0;
    return `<div class="ag-progress">
      <div class="ag-progress-head">
        <span>Indexing attachments</span>
        <span class="ag-progress-when">${known ? `${pct}%` : "counting…"}</span>
      </div>
      <div class="ag-bar ${known ? "" : "indeterminate"}">
        <div class="ag-bar-fill"${known ? ` style="width:${pct}%"` : ""}></div>
      </div>
      <div class="ag-progress-sub">
        ${num(done)} / ${num(total)} indexed · ${num(ix.pending)} queued${
          ix.error ? ` · ${num(ix.error)} unreadable` : ""}
      </div>
    </div>`;
  }

  // "A mail pass is running somewhere" — drives the toolbar spinner. `active`
  // is the agent's own flag, cleared in the finally block of the pass, so it
  // survives a crash mid-folder. Mail only: attachment indexing has its own
  // signal (see indexing() below) and must not spin this, or the spinner never
  // stops while a backlog drains.
  function syncing() {
    return accountList().some((a) => a.sync_progress && a.sync_progress.active);
  }

  // --- Poll ---
  async function refresh() {
    try {
      latest = await App.api.syncStatus();
      renderIndicator();
      renderStrip();
      if (isOpen()) renderModal();
    } catch (_) {
      // The server itself is unreachable. Leave the last known payload up
      // rather than blanking the panel: the SSE connection dropping is the
      // honest signal for that failure, and inventing an agent fault here
      // would point the user at the wrong process.
    }
    // Unconditional, including after a failure: a poll loop that stops on the
    // first bad response would never notice the server coming back.
    schedule();
  }

  function interval() {
    if (isOpen()) return POLL_OPEN;
    // Indexing counts too: its bar moves on the same poll, and an idle cadence
    // would leave it visibly frozen through a long backlog.
    return syncing() || indexing() ? POLL_ACTIVE : POLL_IDLE;
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(refresh, interval());
  }

  // Called on the refresh click. Starts the spinner before the agent has
  // written anything, and pulls the next poll in so the bar appears promptly
  // rather than up to POLL_IDLE later.
  function nudge() {
    spinUntil = Date.now() + NUDGE_MS;
    renderIndicator();
    setTimeout(refresh, 1200);
  }

  // --- Sidebar sync strip ---
  // The same renderer as the modal, mounted a second time rather than forked:
  // an in-flight sync is worth seeing without opening anything. Only while it
  // is in flight, though — progressBlock's idle "last pass" variant is a modal
  // footnote and would sit in the sidebar forever.
  //
  // Collapsible like the shortcut cheat sheet below it, and remembered the same
  // way: a backfill runs for hours, and someone who has seen the numbers once
  // should not have the sidebar shortened by them for the rest of the day.
  const STRIP_KEY = "meerail.sync.collapsed";

  function stripCollapsed() { return localStorage.getItem(STRIP_KEY) === "1"; }

  function applyStripCollapsed(state) {
    const strip = $("#sync-strip");
    if (!strip) return;
    strip.classList.toggle("collapsed", state);
    const btn = strip.querySelector(".sc-toggle");
    if (!btn) return;              // nothing rendered yet (no pass running)
    btn.setAttribute("aria-expanded", String(!state));
    btn.title = state ? "Show sync progress" : "Minimize";
    strip.querySelector(".sc-glyph").innerHTML = App.icon(state ? "chevron" : "minimize", 14);
    localStorage.setItem(STRIP_KEY, state ? "1" : "0");
  }

  function renderStrip() {
    const strip = $("#sync-strip");
    if (!strip) return;
    const live = accountList().filter((a) => a.sync_progress && a.sync_progress.active);
    const index = indexBlock();
    if (!live.length && !index) {
      strip.hidden = true;
      strip.innerHTML = "";
      return;
    }
    // Which account is syncing only matters when there is more than one; with a
    // single account the name is just a wider sidebar for no information.
    const multi = accountList().length > 1;
    strip.hidden = false;
    const body = live.map((a) =>
      (multi ? `<div class="sync-strip-who">${App.esc(a.label || a.email)}</div>` : "")
      + progressBlock(a)).join("");
    // Two independent jobs, so name the one that is actually running rather
    // than filing attachment indexing under "Syncing".
    const label = live.length && index ? "Syncing · Indexing"
      : live.length ? "Syncing" : "Indexing";
    strip.innerHTML = `
      <button class="sc-toggle" type="button" aria-expanded="true">
        <span>${label}</span>
        <span class="sc-glyph"></span>
      </button>
      <div class="sc-body">${body}${index}</div>`;
    // Re-bound on every poll because the markup above is replaced wholesale.
    strip.querySelector(".sc-toggle")
      .addEventListener("click", () => applyStripCollapsed(!stripCollapsed()));
    applyStripCollapsed(stripCollapsed());
  }

  // --- Toolbar indicator + warning strip ---
  function renderIndicator() {
    const btn = $("#btn-agent");
    const strip = $("#agent-warning");
    // The refresh button spins for as long as the agent is actually working,
    // rather than for a fixed six seconds that matched the real pass only by
    // coincidence. The nudge floor covers the gap before the first write.
    const refreshBtn = $("#btn-refresh");
    if (refreshBtn) {
      refreshBtn.classList.toggle("spinning", syncing() || Date.now() < spinUntil);
    }
    if (!btn || !latest) return;

    const accounts = latest.accounts || [];
    const bad = accounts.filter(isBad);
    btn.classList.toggle("warn", bad.length > 0);
    btn.innerHTML = App.icon(bad.length ? "warning" : "activity", 17);
    btn.title = bad.length
      ? `Agent problem — ${summarize(bad)}`
      : "Agent status";

    // No accounts at all is the first-run state, not a fault. The empty message
    // list already explains it, so a red strip on top would just be noise.
    if (!bad.length || !accounts.length) {
      strip.hidden = true;
      return;
    }
    strip.hidden = false;
    strip.innerHTML =
      `<span class="aw-icon">${App.icon("warning", 14)}</span>` +
      `<span class="aw-text">${App.esc(summarize(bad))}</span>`;
  }

  function summarize(bad) {
    const one = bad[0];
    const who = one.label || one.email;
    const verb = one.state === "offline" ? "agent is not running" : "sync is failing";
    if (bad.length === 1) return `${who}: ${verb}`;
    return `${bad.length} accounts: agent not syncing`;
  }

  // --- Modal ---
  function accountCard(a) {
    const m = meta(a.state);
    const rows = [
      ["Last sign of agent", App.relTime(a.last_agent_seen)],
      ["Last completed sync", App.relTime(a.last_sync_at)],
      ["Newest mail stored", App.relTime(a.last_message_at)],
      ["Downloaded, last hour", a.stored_last_hour.toLocaleString()],
      ["Downloaded, last 24h", a.stored_last_day.toLocaleString()],
      ["Downloaded, last 7 days", a.stored_last_week.toLocaleString()],
      ["Messages stored", a.stored_total.toLocaleString()],
      ["Folders tracked", String(a.mailbox_count)],
    ];
    const error = a.last_error
      ? `<div class="ag-error">
           <div class="ag-error-head">Last error · ${App.esc(App.relTime(a.last_error_at))}</div>
           <pre>${App.esc(a.last_error)}</pre>
         </div>`
      : "";
    // A pending request is the whole feedback story for this button: the recheck
    // itself can take many minutes, and the flag clearing is the only honest
    // "done". So the button becomes the status while one is outstanding.
    const recheck = a.recheck_requested
      ? `<div class="ag-recheck pending">
           Full recheck requested ${App.esc(App.relTime(a.recheck_requested_at))} — the
           agent re-walks every folder on its next pass. This can take a while.
         </div>`
      : `<div class="ag-recheck">
           <button class="ag-btn" data-recheck="${App.esc(a.email)}">Recheck all mail</button>
           <span class="ag-btn-hint">Re-reads every folder from the start to repair
             missing or damaged messages. Nothing is duplicated.</span>
         </div>`;
    return `<li class="ag-account">
      <div class="ag-head">
        <span class="ag-name">${App.esc(a.label || a.email)}</span>
        <span class="status-pill ${m.pill}">${m.label}</span>
      </div>
      <div class="ag-detail">${App.esc(a.state_detail)}</div>
      ${progressBlock(a)}
      <dl class="ag-stats">
        ${rows.map(([k, v]) =>
          `<div><dt>${App.esc(k)}</dt><dd>${App.esc(v)}</dd></div>`).join("")}
      </dl>
      ${error}
      ${recheck}
    </li>`;
  }

  async function requestRecheck(email, btn) {
    if (!confirm(
      `Recheck all mail for ${email}?\n\n` +
      `The agent will re-read every folder from the beginning instead of only ` +
      `fetching new mail. Messages you already have are left alone — this only ` +
      `fills in what is missing — but a large mailbox can take a long while.`
    )) return;
    btn.disabled = true;
    btn.textContent = "Requesting…";
    try {
      await App.api.requestRecheck(email);
    } catch (e) {
      btn.disabled = false;
      btn.textContent = "Recheck all mail";
      alert(`Could not request the recheck: ${e.message}`);
      return;
    }
    // Re-read rather than patching `latest` locally, so what the modal shows is
    // the flag the agent will actually act on.
    refresh();
  }

  function renderModal() {
    const body = $("#agent-body");
    if (!latest) { body.innerHTML = `<p class="muted small">Loading…</p>`; return; }
    const accounts = latest.accounts || [];
    if (!accounts.length) {
      body.innerHTML = `<p class="muted small">No accounts yet. Start a
        <code>meerail-agent</code> and it registers itself here.</p>`;
      return;
    }
    // Above the per-account cards: the queue is global, so it belongs to the
    // whole agent rather than to any one address.
    body.innerHTML =
      indexBlock() +
      `<ul class="ag-list">${accounts.map(accountCard).join("")}</ul>` +
      `<p class="muted small">The agent syncs on its own schedule and writes
       straight to the database; these figures are read back from what it has
       stored. If something looks stuck, check the agent's log — it prints every
       failure — or run <code>meerail-agent --test</code>.</p>`;
  }

  function isOpen() { return !$("#agent-modal").hidden; }

  function open() {
    $("#agent-modal").hidden = false;
    renderModal();
    refresh();     // freshen on open rather than showing stale numbers; also
                   // reschedules, picking up the faster open cadence
  }

  function close() {
    $("#agent-modal").hidden = true;
    schedule();    // back to the idle (or active-sync) cadence
  }

  function init() {
    $("#btn-agent").innerHTML = App.icon("activity", 17);
    $("#btn-agent").addEventListener("click", open);
    $("#btn-close-agent").innerHTML = App.icon("close", 18);
    $("#btn-close-agent").addEventListener("click", close);
    $("#agent-modal").addEventListener("click", (e) => {
      if (e.target.id === "agent-modal") close();
    });
    $("#agent-warning").addEventListener("click", open);
    // Delegated: the modal body is re-rendered on every poll, so per-button
    // listeners would not survive.
    $("#agent-body").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-recheck]");
      if (btn) requestRecheck(btn.dataset.recheck, btn);
    });
    refresh();   // schedules the first poll itself
  }

  // `refresh` is exported so the shell can recheck on SSE traffic: any event at
  // all proves the agent is alive, so recovery clears the warning promptly
  // instead of waiting out the poll interval. During a pass that traffic is
  // steady (a `messages` and `cursor` event per batch), so the strip tracks the
  // agent closely without any progress event existing.
  // `nudge` lets the refresh button hand the spinner over to real sync state.
  return { init, refresh, nudge, syncing, open, close, isOpen };
})();
