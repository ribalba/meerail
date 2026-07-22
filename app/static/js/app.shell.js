/* meerail shell: boots the app, renders the sidebar, wires selection,
   the settings modal, and live updates over SSE. */

App.shell = (function () {
  let sidebar = null;        // last /api/mailboxes payload
  let selection = null;      // { key, title, showAccount, params }
  let refreshTimer = null;
  let listRequest = 0;
  let listTotal = 0;         // conversations matching the selection, not just the page

  const PAGE = 100;          // conversations per fetch — see loadMore()
  const MAX_ROWS = 1000;     // the server's ceiling on ?limit, and so on ours

  const $ = (s) => document.querySelector(s);

  // --- Sidebar ---
  // `star` = { id, on } on real folders, which can be pinned to Favorites;
  // omitted on the smart rows, which are always there.
  function mailboxRow(sel, iconName, name, count, activeKey, star) {
    const active = sel.key === activeKey ? " active" : "";
    const badge = count ? `<span class="mailbox-count">${count}</span>` : "";
    let pin = "";
    if (star) {
      const label = star.on ? "Remove from Favorites" : "Add to Favorites";
      pin = `<button class="mb-star${star.on ? " on" : ""}" data-mailbox="${star.id}"
        data-on="${star.on ? 1 : 0}" title="${label}" aria-label="${label}"
        >${App.icon("star", 13, star.on)}</button>`;
    }
    return `<div class="mailbox-row${active}" data-key="${sel.key}">
      <span class="mb-icon">${App.icon(iconName, 16)}</span>
      <span class="mailbox-name">${App.esc(name)}</span>${pin}${badge}</div>`;
  }

  const selections = {};   // key -> selection object

  function register(sel) { selections[sel.key] = sel; return sel; }

  function renderSidebar() {
    const tree = $("#mailbox-tree");
    const activeKey = selection ? selection.key : null;
    let html = "";

    const multi = sidebar.smart.account_count > 1;

    // Flagged stays registered but unrendered: it is no longer a fixed Favorites
    // row, yet the "g f" chord still jumps to it.
    register({ key: "flagged", title: "Flagged", showAccount: true, params: { scope: "flagged" } });

    let favs = "";
    if (multi) {
      register({ key: "unified", title: "All Inboxes", showAccount: true, ageTint: true,
                 params: { scope: "unified_inbox" } });
      favs += mailboxRow(selections["unified"], "inbox", "All Inboxes",
        sidebar.smart.unified_inbox_unread, activeKey);
    }
    // Pinned folders. Keys are distinct from the account-tree copy of the same
    // folder so both rows can carry their own active state.
    for (const acc of sidebar.accounts) {
      for (const mb of acc.mailboxes) {
        if (!mb.favorite) continue;
        const key = "fav-" + mb.id;
        const title = multi ? `${mb.display_name} — ${acc.label || acc.email}` : mb.display_name;
        register({ key, title, showAccount: false, ageTint: mb.role === "inbox",
                   params: { mailbox_id: mb.id } });
        favs += mailboxRow(selections[key], App.roleIcon(mb.role), mb.display_name,
          mb.unread, activeKey, { id: mb.id, on: true });
      }
    }
    // With one account and nothing pinned there are no favorites at all — drop
    // the heading rather than leaving it stranded above the first account.
    if (favs) html += `<div class="tree-section">Favorites</div>` + favs;

    for (const acc of sidebar.accounts) {
      const accName = acc.label || acc.email;
      html += `<div class="account-head"><span class="account-dot" style="background:${App.esc(acc.color)}"></span>
        <span class="account-label">${App.esc(accName)}</span>
        <button class="acc-add" data-account="${acc.id}" data-label="${App.esc(accName)}"
          title="New folder" aria-label="New folder in ${App.esc(accName)}"
          >${App.icon("plus", 14)}</button></div>`;
      for (const mb of acc.mailboxes) {
        const key = "mb-" + mb.id;
        register({ key, title: mb.display_name, showAccount: false, ageTint: mb.role === "inbox",
                   params: { mailbox_id: mb.id } });
        html += mailboxRow(selections[key], App.roleIcon(mb.role), mb.display_name, mb.unread,
          activeKey, { id: mb.id, on: mb.favorite });
      }
    }
    tree.innerHTML = html;
    tree.querySelectorAll(".mailbox-row").forEach((el) => {
      el.addEventListener("click", () => select(selections[el.dataset.key]));
    });
    tree.querySelectorAll(".mb-star").forEach((el) => {
      el.addEventListener("click", (e) => {
        e.stopPropagation();   // the star sits inside a row that selects on click
        toggleFavorite(el.dataset.mailbox, el.dataset.on !== "1");
      });
    });
    tree.querySelectorAll(".acc-add").forEach((el) => {
      el.addEventListener("click", () => openFolder(el.dataset.account, el.dataset.label));
    });
    // A folder added or renamed since the last render has to reach the search
    // scope menu too, otherwise it offers a list of folders that no longer
    // matches the one beside it.
    if (App.search) App.search.syncScope();
  }

  async function toggleFavorite(mailboxId, favorite) {
    try {
      await App.api.favoriteMailbox(mailboxId, favorite);
    } catch (e) {
      return;  // nothing was rendered optimistically, so the sidebar is still truthful
    }
    // Unpinning the row you are reading takes it out of Favorites; hand the
    // selection to the account-tree copy so the highlight survives.
    if (!favorite && selection && selection.key === "fav-" + mailboxId) {
      selection = Object.assign({}, selection, { key: "mb-" + mailboxId });
    }
    sidebar = await App.api.mailboxes();
    renderSidebar();
  }

  // --- Selection + list ---
  async function select(sel) {
    if (!sel) return;
    if (App.search) App.search.clear(false);  // leaving search when a folder is picked
    selection = sel;
    $("#list-title").textContent = sel.title;
    document.querySelectorAll(".mailbox-row.active").forEach((n) => n.classList.remove("active"));
    const el = document.querySelector(`.mailbox-row[data-key="${sel.key}"]`);
    if (el) el.classList.add("active");
    App.list.reset();
    App.reader.clear();
    await loadList();
  }

  // A background refresh re-fetches everything that is on screen rather than
  // the first page: collapsing a list the reader has paged through, just
  // because mail arrived, loses their place.
  async function loadList(keepPaged = false) {
    if (!selection) return;
    const request = ++listRequest;
    const selected = selection;
    const want = keepPaged ? Math.min(MAX_ROWS, Math.max(PAGE, App.list.count())) : PAGE;
    try {
      const data = await App.api.messages(Object.assign({ limit: want }, selected.params));
      if (request !== listRequest || selection !== selected
          || (App.search && App.search.isActive())) return;
      listTotal = data.total || 0;
      App.list.render(data.rows, selected.showAccount, selected.ageTint);
      App.list.setMore(hasMore() ? loadMore : null);
    } catch (e) {
      if (request !== listRequest || selection !== selected) return;
      document.getElementById("message-list").innerHTML =
        `<div class="list-empty">Could not load: ${App.esc(e.message)}</div>`;
    }
  }

  function hasMore() {
    return App.list.count() < Math.min(listTotal, MAX_ROWS);
  }

  // Appends the next page. Errors deliberately propagate: the button that
  // called this re-enables itself so the click can simply be retried.
  async function loadMore() {
    if (!selection) return;
    const request = listRequest;
    const selected = selection;
    const data = await App.api.messages(
      Object.assign({ limit: PAGE, offset: App.list.count() }, selected.params));
    // A folder switch or a refresh landed while we were fetching — those rows
    // belong to a list that is no longer on screen.
    if (request !== listRequest || selection !== selected
        || (App.search && App.search.isActive())) return;
    listTotal = data.total || 0;
    App.list.append(data.rows, selected.showAccount);
    App.list.setMore(data.rows.length && hasMore() ? loadMore : null);
  }

  // --- Live updates ---
  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(async () => {
      sidebar = await App.api.mailboxes();
      renderSidebar();
      // Keep the compose From dropdown current (new accounts / send addresses).
      if (App.compose && App.compose.refreshAccounts) App.compose.refreshAccounts();
      // An agent may have just registered the first account(s).
      if (!selection) selectDefault();
      if (!$("#settings-modal").hidden) renderSettingsAccounts();
      // Activity of any kind is evidence about the agent — recheck its health.
      App.status.refresh();
      // Don't clobber a live search result set with the folder list.
      if (!App.search || !App.search.isActive()) await loadList(true);
    }, 500);
  }

  // The button asks the agent to go fetch, then reloads what we already have.
  // Those are two different things: the agent's pass lands asynchronously and
  // arrives on its own as SSE events, so the local reload is what gives the
  // click an immediate response.
  async function requestRefresh() {
    const btn = $("#btn-refresh");
    if (btn.disabled) return;
    btn.disabled = true;
    try {
      await App.api.requestSync();
    } catch (_) {
      // No agent listening, or the request failed — the reload below still
      // shows whatever has already been synced, so this isn't worth surfacing.
    }
    scheduleRefresh();
    // The spinner belongs to App.status now: it runs for as long as the agent
    // reports a live pass, so a long backfill keeps spinning instead of going
    // still after six seconds while the work continues. The button re-enables
    // on its own timer, which is only rate-limiting for the click.
    App.status.nudge();
    setTimeout(() => { btn.disabled = false; }, 6000);
  }

  // The folders a message could be moved into: IMAP moves are within one
  // account, so only that account's own mailboxes are offered. Read off the
  // sidebar payload rather than refetched — it is already kept current by
  // scheduleRefresh, and a move menu must open on the click, not after a round
  // trip.
  function mailboxesFor(accountId) {
    if (!sidebar) return [];
    const acc = sidebar.accounts.find((a) => a.id === accountId);
    return acc ? acc.mailboxes : [];
  }

  function currentMailboxId() {
    return selection && selection.params ? selection.params.mailbox_id || null : null;
  }

  // Every account with its folders, in sidebar order. The search scope menu
  // lists the same folders in the same order, so the two never disagree about
  // what exists or what a folder is called.
  function accounts() { return sidebar ? sidebar.accounts : []; }

  // What a folder-wide bulk action would act on. Null while a search is showing:
  // search results come from a different query than /api/messages, so there is
  // no selector that means "everything you can see" — see app.bulk.js.
  function listSelector() {
    if (App.search && App.search.isActive()) return null;
    return selection && selection.params ? selection.params : null;
  }

  function currentTitle() { return selection ? selection.title : ""; }

  async function reloadList() {
    if (!selection) return;
    $("#list-title").textContent = selection.title;
    await loadList(true);   // an archive from row 200 shouldn't snap back to page one
  }

  function connectSSE() {
    const es = new EventSource("/api/stream");
    ["accounts", "messages", "flags", "cursor", "present", "folders", "extract"].forEach((t) =>
      es.addEventListener(t, scheduleRefresh));
    // "agent" fires when the agent's health changes. It rides the same debounce
    // as the rest; the status refresh happens inside it. Note that this can only
    // ever deliver good news promptly — an agent that has died sends nothing at
    // all, which is why App.status polls as well as listening.
    es.addEventListener("agent", scheduleRefresh);
    // EventSource auto-reconnects, so an error here is not proof of anything on
    // its own — but a server that has gone away is usually noticed here first,
    // long before the user clicks something. Hand it to the watchdog, which
    // confirms with a probe before showing the bar.
    es.onopen = () => App.conn.ok();
    es.onerror = () => App.conn.fail();
  }

  // --- Settings modal (accounts) ---
  async function renderSettingsAccounts() {
    const list = $("#settings-account-list");
    // A redraw replaces every input in the list, so anything being typed loses
    // both its focus and its text. Mail arriving must not interrupt an edit —
    // skip the pass entirely while the user is in here. The next refresh after
    // they click away picks up whatever changed in the meantime.
    if (list.contains(document.activeElement)) return;
    let accounts = [];
    try { accounts = await App.api.accounts(); } catch (_) {}
    list.innerHTML = "";
    for (const a of accounts) {
      const age = App.ageSeconds(a.last_agent_seen);
      const online = age !== null && age < 120;
      const li = document.createElement("li");
      li.innerHTML = `
        <div class="sa-row">
          <input type="color" class="sa-color" data-color="${a.id}" value="${App.esc(a.color)}"
            title="Account colour" />
          <span class="sa-main">
            <input type="text" class="sa-name" data-name="${a.id}" value="${App.esc(a.label)}"
              placeholder="${App.esc(a.email.split("@")[0])}" aria-label="Account name" />
            <div class="sa-sub">${App.esc(a.email)} · agent ${App.relTime(a.last_agent_seen)}</div>
          </span>
          <span class="status-pill ${online ? "ok" : ""}">${online ? "online" : (a.backfill_complete ? "synced" : "waiting")}</span>
        </div>
        <div class="sa-footer">
          <label for="footer-${a.id}">Footer — prefilled into the composer, editable per message</label>
          <textarea id="footer-${a.id}" data-footer="${a.id}" rows="3"
            placeholder="Empty — the composer opens without a footer">${App.esc(a.footer || "")}</textarea>
          <div class="sa-footer-actions">
            <button type="button" data-save="${a.id}">Save</button>
            <span class="sa-footer-status" data-save-status="${a.id}"></span>
          </div>
        </div>`;
      list.appendChild(li);
    }
    // No remove button by design: the agent's config.toml is what decides which
    // accounts exist. Deleting here only dropped the synced copy, and the agent
    // put it straight back on its next pass — so the control never did what it
    // appeared to. Removing an account means removing it from the agent config.
    list.querySelectorAll("[data-save]").forEach((btn) => {
      // Baseline off the rendered inputs rather than the API payload: <input
      // type="color"> normalizes its value, so an untouched picker would other-
      // wise read as changed.
      const id = btn.dataset.save;
      const base = {
        label: $(`[data-name="${id}"]`).value,
        color: $(`[data-color="${id}"]`).value,
        footer: $(`[data-footer="${id}"]`).value,
      };
      btn.addEventListener("click", () => saveAccount(id, base));
    });
  }

  async function saveAccount(accountId, base) {
    const status = $(`[data-save-status="${accountId}"]`);
    const now = {
      label: $(`[data-name="${accountId}"]`).value.trim(),
      color: $(`[data-color="${accountId}"]`).value,
      footer: $(`[data-footer="${accountId}"]`).value,
    };
    // Send only what moved. Sending `footer` at all flips footer_customized on
    // the server, so a colour-only save must not carry it along.
    const payload = {};
    for (const field of ["label", "color", "footer"]) {
      if (now[field] !== base[field]) payload[field] = now[field];
    }
    if (!Object.keys(payload).length) { status.textContent = "No changes"; return; }

    status.textContent = "Saving…";
    status.classList.remove("error");
    try {
      await App.api.patch(`/api/accounts/${accountId}`, payload);
      status.textContent = "Saved";
      Object.assign(base, now);
      // The sidebar and compose's From list both render label and colour.
      if (App.compose && App.compose.refreshAccounts) App.compose.refreshAccounts();
      scheduleRefresh();
      setTimeout(() => { status.textContent = ""; }, 2500);
    } catch (e) {
      status.textContent = e.message || "Could not save";
      status.classList.add("error");
    }
  }

  // --- Settings modal (Meerato task URL) ---
  // Static markup wired once in wire(), unlike the account list: the SSE
  // refresh redraws that list out from under any listener bound to it, and a
  // half-typed URL must survive mail arriving.
  async function loadMeeratoUrl() {
    const input = $("#meerato-url");
    // Never clobber a URL being typed — the modal re-opens on every settings
    // click, but the field keeps whatever was left in it.
    if (document.activeElement === input) return;
    try {
      const cfg = await App.tasks.refreshConfig();
      input.value = cfg.url || "";
    } catch (_) {}
  }

  async function saveMeeratoUrl() {
    const status = $("#meerato-status");
    status.classList.remove("error");
    status.textContent = "Checking…";
    try {
      // The server probes the URL before storing it, so "Saved" here means the
      // token actually works — not merely that the string was written down.
      const cfg = await App.api.saveTaskConfig($("#meerato-url").value.trim());
      status.textContent = cfg.configured ? "Saved" : "Removed";
      await App.tasks.refreshConfig();
      setTimeout(() => { status.textContent = ""; }, 2500);
    } catch (e) {
      status.textContent = e.message || "Could not save";
      status.classList.add("error");
    }
  }

  // --- Settings modal (age tint) ---
  // Applied on input rather than behind a Save button: it is a purely local
  // display preference, and seeing the list recolour as you type is the whole
  // point of picking a number here.
  function applyAgeDays() {
    const v = parseInt($("#age-days").value, 10);
    if (isNaN(v) || v < 0) return;
    App.list.setAgeDays(v);
  }

  function openSettings() {
    $("#settings-modal").hidden = false;
    renderSettingsAccounts();
    loadMeeratoUrl();
    $("#age-days").value = App.list.ageDays();
  }
  function closeSettings() { $("#settings-modal").hidden = true; }
  function settingsOpen() { return !$("#settings-modal").hidden; }

  // --- New folder ---
  let folderAccountId = null;

  function openFolder(accountId, label) {
    folderAccountId = accountId;
    $("#folder-account-hint").textContent = `Created in ${label}.`;
    setFolderStatus("");
    $("#folder-create").disabled = false;
    $("#folder-name").value = "";
    $("#folder-modal").hidden = false;
    $("#folder-name").focus();
  }
  function closeFolder() { $("#folder-modal").hidden = true; }
  function folderOpen() { return !$("#folder-modal").hidden; }

  function setFolderStatus(text, isError) {
    const el = $("#folder-status-line");
    el.textContent = text;
    el.classList.toggle("error", !!isError);
  }

  async function submitFolder() {
    const name = $("#folder-name").value.trim();
    if (!name) return setFolderStatus("Enter a folder name", true);
    $("#folder-create").disabled = true;
    setFolderStatus("Creating…");
    try {
      await App.api.createMailbox(folderAccountId, name);
    } catch (e) {
      $("#folder-create").disabled = false;
      return setFolderStatus(e.message || "Could not create folder", true);
    }
    // The folder is made on the server by the agent, not here, so it cannot be
    // rendered yet — say so rather than closing onto an unchanged sidebar. The
    // "folders" event from the agent's next pass brings it in on its own.
    setFolderStatus("Queued — appears once the agent syncs.");
    setTimeout(() => { if (folderOpen()) closeFolder(); }, 2200);
  }

  // Jump targets for the "g …" chords. "unified" only exists with >1 account,
  // so it falls back to the first inbox rather than doing nothing.
  function goto(kind) {
    if (kind === "flagged") return select(selections["flagged"]);
    if (kind === "unified" && selections["unified"]) return select(selections["unified"]);
    if (!sidebar || !sidebar.accounts.length) return;
    const acc = sidebar.accounts[0];
    const inbox = acc.mailboxes.find((m) => m.role === "inbox") || acc.mailboxes[0];
    if (inbox) select(selections["mb-" + inbox.id]);
  }

  function wire() {
    $("#btn-settings").innerHTML = App.icon("settings", 18);
    $("#btn-refresh").innerHTML = App.icon("refresh", 17);  // optically matches settings at 18
    $("#btn-close-settings").innerHTML = App.icon("close", 18);
    $("#search-icon").innerHTML = App.icon("search", 15);
    $("#btn-settings").addEventListener("click", openSettings);
    $("#btn-close-settings").addEventListener("click", closeSettings);
    $("#settings-modal").addEventListener("click", (e) => {
      if (e.target.id === "settings-modal") closeSettings();
    });
    $("#btn-close-folder").innerHTML = App.icon("close", 18);
    $("#btn-close-folder").addEventListener("click", closeFolder);
    $("#folder-modal").addEventListener("click", (e) => {
      if (e.target.id === "folder-modal") closeFolder();
    });
    $("#folder-create").addEventListener("click", submitFolder);
    $("#folder-name").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); submitFolder(); }
    });
    $("#btn-refresh").addEventListener("click", requestRefresh);
    $("#meerato-save").addEventListener("click", saveMeeratoUrl);
    $("#age-days").addEventListener("input", applyAgeDays);
  }

  function selectDefault() {
    if (sidebar.smart.account_count > 1) select(selections["unified"]);
    else if (sidebar.accounts.length) {
      const inbox = sidebar.accounts[0].mailboxes.find((m) => m.role === "inbox")
        || sidebar.accounts[0].mailboxes[0];
      if (inbox) select(selections["mb-" + inbox.id]);
    } else {
      $("#list-title").textContent = "meerail";
      document.getElementById("message-list").innerHTML =
        `<div class="list-empty">No accounts yet.<br>Start a <code>meerail-agent</code> and its
        accounts appear here automatically.</div>`;
      openSettings();
    }
  }

  async function boot() {
    // Layout only, no server involved — do it before anything that can fail so
    // even a half-booted shell comes up at the width the user left it.
    App.split.init();
    // First, so that a server that is already down at page load says so
    // instead of leaving an empty shell with no explanation.
    App.conn.init();
    try {
      await App.api.ensureSession();
      wire();
      App.search.init();
      App.compose.init();
      App.keys.init();
      App.bulk.init();
      App.tasks.init();
      App.status.init();
      App.stats.init();
      connectSSE();
      sidebar = await App.api.mailboxes();
      renderSidebar();
      selectDefault();
    } catch (err) {
      // Half-booted is not a state worth patching up: the bar explains why, and
      // the page reloads itself the moment the server answers again.
      App.conn.whenRestored(() => location.reload());
      throw err;
    }
  }

  return { boot, currentMailboxId, mailboxesFor, accounts, reloadList, goto, closeSettings, settingsOpen,
           closeFolder, folderOpen, listSelector, currentTitle, listTotal: () => listTotal };
})();

document.addEventListener("DOMContentLoaded", App.shell.boot);
