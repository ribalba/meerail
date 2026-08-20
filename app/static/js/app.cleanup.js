/* meerail cleanup: the "Cleanup" modal — mail you get over and over, grouped.

   One /api/cleanup/clusters call fills the whole panel, the same way the stats
   modal works and for the same reason: the server does the grouping, and this
   file holds no rule about what may be deleted. That matters more here than
   anywhere else in the UI — the protections (never Sent, never flagged, never
   answered, never this month's mail) are decided in app/routers/cleanup.py and
   re-checked at the moment the mail moves, so a stale panel cannot talk the
   server into anything the server would not do fresh.

   Which is also why a row's Delete sends the group's *name* — sender plus
   template, or sender plus fingerprint token — rather than the ids it drew. The
   server looks the group up again under the live filter.

   The two-step Delete is not friction for its own sake. Every other delete in
   meerail acts on something you are looking at; this one acts on a thousand
   messages you are not, so the second click is where the number gets read. */

App.cleanup = (function () {
  const $ = (s) => document.querySelector(s);

  // How the mail is grouped. The labels say what the group *is*, not how it was
  // computed — "Similar body" is a promise about the mail, "MinHash" is not.
  const MODES = [
    ["subject", "Same subject"],
    ["body", "Similar body"],
  ];

  // What "biggest" means. The two orders disagree sharply — the group that
  // costs the most disk is rarely the one that interrupts you most — so this is
  // a control rather than a decision made for the reader.
  const SORTS = [
    ["size", "Biggest"],
    ["count", "Most mail"],
  ];

  const STATE_KEY = "meerail.cleanup.state";

  let data = null;
  let loading = false;
  let error = "";
  // The group whose Delete has been pressed once and not yet confirmed, and the
  // one currently being filed. Keys, not indices: the list is re-sorted and
  // re-fetched under both.
  let confirming = null;
  let working = null;
  // key -> how many this session filed, so a row can say what it did instead of
  // vanishing and leaving the page a message shorter for no visible reason.
  const filed = new Map();

  let state = { account_id: null, mode: "subject", sort: "size" };

  const num = (n) => (n == null ? "—" : Number(n).toLocaleString());

  // --- Turning a group into a search ---

  // `:from` is matched as a regex server-side, and an address is full of
  // characters a regex reads as instructions — the dots are harmless, a `+` in
  // the local part is not.
  function rxEscape(v) {
    return String(v).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  /* The longest stretch of a subject template that is words rather than masks.

     "package updates on rebel (# pending)" is not a search — the # stands for
     whatever number that copy carried. What is searchable is the part around
     it, and the longest such part is the one most likely to be about this
     sender rather than about email in general. Punctuation is trimmed off both
     ends so the phrase reads as a phrase, and any quote inside it goes, because
     the result is about to be wrapped in quotes. */
  function literalRun(template) {
    return String(template || "")
      .split("#")
      .map((part) => part.replace(/"/g, " ")
                         .replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, "").trim())
      .reduce((best, part) => (part.length > best.length ? part : best), "");
  }

  /* The query that shows a group's mail in the ordinary message list.

     Body groups get `:similar`, which is the only way to ask for them: their
     subjects are all different, and what they have in common is a fingerprint
     no one could type. Subject groups get the phrase itself, because for those
     the title *is* the thing they share — and a phrase somebody can read, edit
     and re-run beats an opaque token whenever one will do.

     Both are scoped to the sender, which is how the group was defined. */
  function queryFor(group) {
    // `:no-trash` because this view is worked *while* deleting: without it the
    // mail you just trashed stays on screen — search reads the whole mailbox,
    // Trash included — and a delete that changes nothing visible reads as a
    // delete that failed.
    const from = `:no-trash :from=${rxEscape(group.from_addr)}`;
    // The key of a body group is its seed message — the one every member
    // resembles — so this is the same set the panel counted and the same set
    // Delete would take.
    if (data.mode === "body") return `${from} :similar=${group.key}`;
    const phrase = literalRun(group.label);
    return phrase.length >= 3 ? `${from} "${phrase}"` : from;
  }

  function show(group) {
    close();
    // All time, explicitly: the group was counted over the whole mailbox and
    // the search box otherwise looks at the last year only.
    App.search.showQuery(queryFor(group), "keyword", 0);
  }

  // --- Persistence ---
  function loadState() {
    try {
      const raw = JSON.parse(localStorage.getItem(STATE_KEY) || "{}");
      if (raw && typeof raw === "object") {
        if (MODES.some(([k]) => k === raw.mode)) state.mode = raw.mode;
        if (SORTS.some(([k]) => k === raw.sort)) state.sort = raw.sort;
        if (raw.account_id === null || Number.isInteger(raw.account_id)) {
          state.account_id = raw.account_id;
        }
      }
    } catch (_) { /* corrupt entry: fall back to the defaults above */ }
  }

  function saveState() {
    localStorage.setItem(STATE_KEY, JSON.stringify(state));
  }

  // --- Fetch ---
  async function refresh() {
    loading = true;
    error = "";
    confirming = null;
    render();
    try {
      data = await App.api.cleanupClusters({
        mode: state.mode,
        sort: state.sort,
        ...(state.account_id == null ? {} : { account_id: state.account_id }),
      });
    } catch (e) {
      error = e.message || "Could not look for groups";
      data = null;
    }
    loading = false;
    render();
  }

  // --- Filing ---

  /* Trash one whole group, looping until the server says it is done.

     Chunked at the server end, so this is the client half of the same loop the
     bulk-delete bar runs: each call is its own committed transaction with its
     own Undo. The `moved === 0` guard is the one that matters — without it a
     group the filter has started refusing (something in it got flagged between
     the draw and the click) would answer "not done, moved nothing" forever. */
  async function file(group) {
    working = group.key;
    confirming = null;
    error = "";
    render();
    let moved = 0;
    try {
      for (;;) {
        const res = await App.api.trashCluster({
          mode: data.mode,
          from_addr: group.from_addr,
          key: group.key,
          ...(state.account_id == null ? {} : { account_id: state.account_id }),
        });
        moved += res.moved || 0;
        if (res.done || !res.moved) break;
      }
      filed.set(group.key, moved);
      // The headline is what is left to do, so it has to come down as rows are
      // worked — refetching the whole panel after every group would re-sort the
      // list under the pointer, which is the one thing a list of buttons you are
      // working down must not do.
      if (data && data.totals) {
        data.totals.groups = Math.max(0, data.totals.groups - 1);
        data.totals.messages = Math.max(0, data.totals.messages - moved);
        data.totals.bytes = Math.max(0, data.totals.bytes - (group.bytes || 0));
      }
    } catch (e) {
      error = e.message || "Could not move that group to Trash";
    }
    working = null;
    render();
  }

  // --- Panels ---

  function controls() {
    const accounts = (data && data.accounts) || [];
    const opts = [`<option value="">All accounts</option>`]
      .concat(accounts.map((a) =>
        `<option value="${a.id}"${state.account_id === a.id ? " selected" : ""}>${
          App.esc(a.label || a.email)}</option>`));
    return `<div class="an-controls">
      <select id="cl-account" class="search-select" title="Account">${opts.join("")}</select>
      <div class="an-tabs" role="group" aria-label="How to group">
        ${MODES.map(([k, label]) =>
          `<button type="button" class="an-tab${state.mode === k ? " on" : ""}"
                   data-mode="${k}">${App.esc(label)}</button>`).join("")}
      </div>
      <div class="an-ranges" role="group" aria-label="Order">
        ${SORTS.map(([k, label]) =>
          `<button type="button" class="an-range${state.sort === k ? " on" : ""}"
                   data-sort="${k}">${App.esc(label)}</button>`).join("")}
      </div>
      ${loading ? `<div class="an-load" role="status" aria-label="Looking for groups">
        <i class="an-load-fill"></i></div>` : ""}
    </div>`;
  }

  function lede() {
    const byBody = state.mode === "body";
    return `<p class="cl-lede">${byBody
      ? `Mail from one sender whose <b>bodies are near-identical</b> even though the subjects
         are not — property alerts, job digests, anything generated from a template.`
      : `Mail from one sender under <b>one subject template</b>, with the numbers and dates
         masked out — backup reports, build failures, login notices.`}
      Every message in every folder is looked at — never your sent mail, and never anything
      flagged or replied to. <b>Click a group to see the mail in it</b>; deleting one moves it
      to Trash, and the Recent actions panel can put it back.</p>`;
  }

  function pendingNote(d) {
    // Only says anything while the fingerprint backfill still owes rows, which
    // on an install that has never been upgraded into this feature is never.
    if (state.mode !== "body" || !d.pending) return "";
    return `<p class="cl-warn">Still reading ${num(d.pending)} older message${
      d.pending === 1 ? "" : "s"} to fingerprint them — groups below are what has been read so
      far. Leave it running and check back.</p>`;
  }

  function totals(d) {
    const t = d.totals || {};
    if (!t.groups) return "";
    const shown = (d.clusters || []).length;
    return `<p class="cl-totals"><b>${num(t.groups)}</b> group${t.groups === 1 ? "" : "s"}
      · <b>${num(t.messages)}</b> message${t.messages === 1 ? "" : "s"}
      ${t.bytes ? `· <b>${App.esc(App.fmtSize(t.bytes))}</b>` : ""}
      ${shown < t.groups ? `<span class="cl-dim">— the ${num(shown)} biggest below</span>` : ""}</p>`;
  }

  function span(group) {
    const a = App.utcDate(group.first), b = App.utcDate(group.last);
    if (!a || !b) return "—";
    const ya = a.getFullYear(), yb = b.getFullYear();
    return ya === yb ? String(ya) : `${ya}–${yb}`;
  }

  /* The second line of a row: what is in the group beyond its size.

     Attachments are called out because they are the one thing that turns a
     group from junk into something worth keeping — "Neue Rechnung verfügbar"
     is a hundred identical notifications and also a hundred invoices. The panel
     will not decide that; it makes sure the number is on screen before the
     button is. */
  function meta(group) {
    const bits = [];
    if (group.attachments) {
      bits.push(`<span class="cl-flagged">${num(group.attachments)} with attachments</span>`);
    }
    if (group.unread) bits.push(`${num(group.unread)} unread`);
    if (group.subjects > 1) {
      bits.push(`${num(group.subjects)} different subjects`);
    }
    return bits.length ? `<div class="cl-meta">${bits.join(" · ")}</div>` : "";
  }

  function action(group) {
    const done = filed.get(group.key);
    if (done != null) {
      return `<span class="cl-done">${num(done)} moved to Trash</span>`;
    }
    if (working === group.key) {
      return `<span class="cl-working">Moving…</span>`;
    }
    if (confirming === group.key) {
      return `<span class="cl-confirm">
        <button type="button" class="cl-btn danger" data-act="do" data-key="${App.esc(group.key)}"
        >Trash ${num(group.count)}</button>
        <button type="button" class="cl-btn" data-act="cancel">Cancel</button></span>`;
    }
    return `<button type="button" class="cl-btn" data-act="ask"
            data-key="${App.esc(group.key)}" ${working ? "disabled" : ""}>Delete</button>`;
  }

  function table(d) {
    const rows = d.clusters || [];
    if (!rows.length) {
      return `<p class="an-empty">Nothing repeats itself enough to be worth grouping here.
        Try the other grouping, or a single account.</p>`;
    }
    return `<table class="cl-table">
      <thead><tr>
        <th>From</th><th>What it says</th>
        <th class="cl-n${state.sort === "count" ? " cl-sorted" : ""}">Mails${
          state.sort === "count" ? " &#9662;" : ""}</th>
        <th class="cl-n${state.sort === "size" ? " cl-sorted" : ""}">Size${
          state.sort === "size" ? " &#9662;" : ""}</th>
        <th class="cl-n">Years</th><th></th>
      </tr></thead>
      <tbody>${rows.map((g) => `<tr data-key="${App.esc(g.key)}"${
        filed.has(g.key) ? ' class="cl-filed"' : ""}>
        <td class="cl-who">
          ${g.from_name ? `<div class="cl-name">${App.esc(g.from_name)}</div>` : ""}
          <div class="cl-addr">${App.esc(g.from_addr)}</div>
        </td>
        <td class="cl-what"><div class="cl-label">${App.esc(g.label)}</div>${meta(g)}</td>
        <td class="cl-n">${num(g.count)}</td>
        <td class="cl-n">${App.esc(App.fmtSize(g.bytes) || "—")}</td>
        <td class="cl-n">${App.esc(span(g))}</td>
        <td class="cl-act">${action(g)}</td>
      </tr>`).join("")}</tbody>
    </table>`;
  }

  function render() {
    const body = $("#cleanup-body");
    if (!body) return;
    const parts = [controls(), lede()];
    if (error) parts.push(`<p class="an-error">${App.esc(error)}</p>`);
    if (data) {
      parts.push(pendingNote(data), totals(data));
      parts.push(`<div class="an-panels${loading ? " stale" : ""}">${table(data)}</div>`);
    } else if (!error) {
      parts.push(`<p class="an-empty">Looking through the mailbox…</p>`);
    }
    body.innerHTML = parts.join("");
  }

  // --- Events ---
  // Delegated from the modal body, which survives every render — the panel is
  // rebuilt wholesale on each state change and rewired listeners would be one
  // more thing to remember on each new control.
  function onClick(e) {
    const tab = e.target.closest("[data-mode]");
    if (tab) {
      state.mode = tab.dataset.mode;
      saveState();
      return refresh();
    }
    const order = e.target.closest("[data-sort]");
    if (order) {
      state.sort = order.dataset.sort;
      saveState();
      return refresh();
    }
    const btn = e.target.closest("button[data-act]");
    if (!btn) {
      // Anywhere on the row but the buttons: show me what is in this group.
      // The action cell is excluded rather than the buttons themselves so the
      // gap around them is not a live target for the row underneath.
      const row = e.target.closest("tr[data-key]");
      if (row && !e.target.closest(".cl-act")) {
        const g = ((data && data.clusters) || []).find((c) => c.key === row.dataset.key);
        if (g) show(g);
      }
      return;
    }
    if (btn.dataset.act === "cancel") {
      confirming = null;
      return render();
    }
    const group = ((data && data.clusters) || []).find((g) => g.key === btn.dataset.key);
    if (!group) return;
    if (btn.dataset.act === "ask") {
      confirming = group.key;
      return render();
    }
    if (btn.dataset.act === "do") file(group);
  }

  function onChange(e) {
    if (e.target.id !== "cl-account") return;
    state.account_id = e.target.value === "" ? null : Number(e.target.value);
    saveState();
    refresh();
  }

  // --- Modal ---
  function isOpen() { return !$("#cleanup-modal").hidden; }

  function open() {
    $("#cleanup-modal").hidden = false;
    // What was filed last time has been filed; a fresh open is a fresh page.
    filed.clear();
    render();
    refresh();
  }

  function close() { $("#cleanup-modal").hidden = true; }

  function init() {
    loadState();
    $("#btn-cleanup").innerHTML = App.icon("cleanup", 17);
    $("#btn-cleanup").addEventListener("click", open);
    $("#btn-close-cleanup").innerHTML = App.icon("close", 18);
    $("#btn-close-cleanup").addEventListener("click", close);
    $("#cleanup-modal").addEventListener("click", (e) => {
      if (e.target.id === "cleanup-modal") close();
    });
    $("#cleanup-body").addEventListener("click", onClick);
    $("#cleanup-body").addEventListener("change", onChange);
  }

  return { init, open, close, isOpen };
})();
