/* meerail shell: boots the app, renders the sidebar, wires selection,
   the settings modal, and live updates over SSE. */

App.shell = (function () {
  let sidebar = null;        // last /api/mailboxes payload
  let selection = null;      // { key, title, showAccount, params }
  let refreshTimer = null;
  let stream = null;         // the open EventSource, so power save can close it
  let listRequest = 0;
  let listTotal = 0;         // conversations matching the selection, not just the page

  const PAGE = 100;          // conversations per fetch — see loadMore()
  const MAX_ROWS = 1000;     // the server's ceiling on ?limit, and so on ours

  const $ = (s) => document.querySelector(s);

  // --- Sidebar ---
  // `star` = { id, on } on real folders, which can be pinned to Favorites;
  // omitted on the smart rows, which are always there.
  // `extra` is an extra class for the row — only the Outbox uses it, to go red
  // when the mail in it has stopped going out.
  function mailboxRow(sel, iconName, name, count, activeKey, star, extra) {
    const active = (sel.key === activeKey ? " active" : "") + (extra ? " " + extra : "");
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

    // The Outbox is registered whether or not it has anything in it, so "g o"
    // and a reload while standing in it both still resolve; the row below is
    // what comes and goes. `outbox: true` is how loadList() knows this folder is
    // not served by /api/messages — see App.outbox.
    register({ key: "outbox", title: "Outbox", outbox: true, params: {} });

    // Mail put off until later. A query rather than a folder — the messages are
    // filed in Archive, and this selects them by the reminder waiting on them
    // (app/reminders.py) — so it is served by /api/messages like every other
    // list and needs no renderer of its own. Registered unconditionally, so
    // "g r" and a reload while standing in it both resolve.
    register({ key: "reminders", title: "Reminders", showAccount: true,
               params: { scope: "reminders" } });

    let favs = "";
    if (multi) {
      register({ key: "unified", title: "All Inboxes", showAccount: true, ageTint: true,
                 params: { scope: "unified_inbox" } });
      favs += mailboxRow(selections["unified"], "inbox", "All Inboxes",
        sidebar.smart.unified_inbox_unread, activeKey);
    }
    // Shown only when it has something to say, and first when it does. An empty
    // outbox is the normal state and a permanent row for it would be furniture;
    // a non-empty one is either "sending, give it a second" or the reason a
    // message never arrived, and both want to be the first thing in the tree.
    // Kept while it is the open folder too, so the ground does not move under
    // someone reading the last message as it goes out.
    const unsent = sidebar.smart.outbox_unsent || 0;
    if (unsent || activeKey === "outbox") {
      const failing = sidebar.smart.outbox_failing || 0;
      favs += mailboxRow(selections["outbox"], failing ? "warning" : "sent", "Outbox",
        unsent, activeKey, null, failing ? "stuck" : "");
    }
    // Same rule as the Outbox: shown while it has something in it, and while it
    // is the folder on screen, so the row does not vanish under someone who has
    // just brought back the last thing in it. It goes red when a reminder is
    // overdue — the moment has passed and the mail has not landed — which means
    // the same thing here as a stuck send does there: it is late, not lost.
    const waiting = sidebar.smart.reminders_pending || 0;
    if (waiting || activeKey === "reminders") {
      const late = sidebar.smart.reminders_overdue || 0;
      favs += mailboxRow(selections["reminders"], "bell", "Reminders",
        waiting, activeKey, null, late ? "stuck" : "");
    }
    // Pinned folders. Keys are distinct from the account-tree copy of the same
    // folder so both rows can carry their own active state.
    for (const acc of sidebar.accounts) {
      for (const mb of acc.mailboxes) {
        if (!mb.favorite) continue;
        const key = "fav-" + mb.id;
        const title = multi ? `${mb.display_name} — ${acc.label || acc.email}` : mb.display_name;
        // `role` rides along because one bulk action is not the same action in
        // every folder: Delete files mail in Trash, and in Trash itself there is
        // nowhere left to file it — see app.bulk.js.
        register({ key, title, showAccount: false, ageTint: mb.role === "inbox",
                   role: mb.role, params: { mailbox_id: mb.id } });
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
                   role: mb.role, params: { mailbox_id: mb.id } });
        html += mailboxRow(selections[key], App.roleIcon(mb.role), mb.display_name, mb.unread,
          activeKey, { id: mb.id, on: mb.favorite });
      }
    }
    tree.innerHTML = html;
    tree.querySelectorAll(".mailbox-row").forEach((el) => {
      el.addEventListener("click", () => {
        select(selections[el.dataset.key]);
        // Only on the click, not inside select(): the narrow layout must follow
        // a tap on a folder, but not selectDefault() at boot (which would open
        // onto the list rather than the folders page) nor a re-select after
        // unpinning, which is bookkeeping and not a navigation.
        App.mobile.show("list");
      });
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
    paintFolderFocus();   // the rows were just rebuilt — put the cursor back on
  }

  // --- Folder keyboard cursor ---
  // The sidebar is the first of the three keyboard panes (folders → list →
  // thread; see app.keys.js). The cursor is held as a key rather than an index
  // because renderSidebar() rebuilds every row on each refresh and the order
  // shifts as folders are pinned or created.
  let folderFocusKey = null;
  let folderKeys = false;     // does the sidebar own the arrows right now?

  function folderRows() {
    return Array.from(document.querySelectorAll("#mailbox-tree .mailbox-row"));
  }

  function paintFolderFocus(scroll = false) {
    const rows = folderRows();
    $("#mailbox-tree").classList.toggle("keys", folderKeys);
    for (const el of rows) el.classList.toggle("focused", el.dataset.key === folderFocusKey);
    if (!scroll) return;
    const el = rows.find((n) => n.dataset.key === folderFocusKey);
    if (el) el.scrollIntoView({ block: "nearest" });
  }

  // Falls back to the open folder when the cursor has no row — at boot, or
  // after the row it sat on was unpinned — so j/k carry on from where you are
  // rather than from the top of the tree.
  function folderIndex(rows) {
    const at = (key) => rows.findIndex((el) => el.dataset.key === key);
    const cur = at(folderFocusKey);
    return cur >= 0 ? cur : (selection ? at(selection.key) : -1);
  }

  function moveFolder(delta) {
    const rows = folderRows();
    if (!rows.length) return;
    const cur = folderIndex(rows);
    const next = cur < 0 ? (delta > 0 ? 0 : rows.length - 1)
                         : Math.min(rows.length - 1, Math.max(0, cur + delta));
    folderFocusKey = rows[next].dataset.key;
    paintFolderFocus(true);
  }

  // Answers false when there is no row to open — an empty sidebar before the
  // first account syncs — so the caller can leave the keyboard where it is
  // instead of handing it to a list that will never appear.
  function openFocusedFolder() {
    const rows = folderRows();
    const cur = folderIndex(rows);
    if (cur < 0) return false;
    folderFocusKey = rows[cur].dataset.key;
    paintFolderFocus();
    // Already standing in this folder: stepping back into the list must not
    // reload it, which would throw away the cursor's place and every page past
    // the first. Search results are the exception — they are not this folder,
    // so re-entering it does have to fetch it back.
    const showing = selection && selection.key === folderFocusKey
      && !(App.search && App.search.isActive());
    if (!showing) select(selections[folderFocusKey]);
    return true;
  }

  // Arrows preview as they go, the same bargain the list strikes: j/k only walk
  // the tree, ↑/↓ walk it and load each folder as they land.
  function moveFolderAndOpen(delta) {
    moveFolder(delta);
    openFocusedFolder();
  }

  function setFolderKeyFocus(state) {
    folderKeys = state;
    if (state && !folderFocusKey && selection) folderFocusKey = selection.key;
    paintFolderFocus(state);
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
    // Leaving the Outbox drops whatever it had open in the reading pane; the
    // pane is about to be handed to a real thread (or to nothing).
    if (App.outbox && selection && selection.outbox && sel.key !== "outbox") App.outbox.clear();
    selection = sel;
    $("#list-title").textContent = sel.title;
    document.querySelectorAll(".mailbox-row.active").forEach((n) => n.classList.remove("active"));
    const el = document.querySelector(`.mailbox-row[data-key="${sel.key}"]`);
    if (el) el.classList.add("active");
    // However the folder was picked — click, "g i", the cursor itself — the
    // keyboard cursor follows it, so Escaping back to the sidebar lands on the
    // folder you are actually in.
    folderFocusKey = sel.key;
    paintFolderFocus();
    App.list.reset();
    App.reader.clear();
    await loadList();
  }

  // A background refresh re-fetches everything that is on screen rather than
  // the first page: collapsing a list the reader has paged through, just
  // because mail arrived, loses their place.
  async function loadList(keepPaged = false) {
    if (!selection) return;
    // The Outbox is this app's own queue rather than an IMAP folder, so it is
    // read from /api/outbox and rendered by App.outbox — into the same two panes,
    // by the same events, but with none of /api/messages' vocabulary (no thread,
    // no UID, no seen flag) that would have to be faked to get it in here.
    if (selection.outbox) {
      listTotal = 0;
      return App.outbox.load();
    }
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
      // An action's queue row changes state as the agent works through it, and
      // "Undo" on a move that has just landed does something different from
      // "Undo" on one still waiting. The panel rides the same debounce.
      if (App.undo) App.undo.refresh();
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

  // The role of the folder on screen ("trash", "inbox", …), or "" for the smart
  // rows, which are queries rather than folders. Read by app.bulk.js, which has
  // one button whose meaning depends on it.
  function currentRole() { return (selection && selection.role) || ""; }

  // Called after an action changed what the list should show. loadList() bails
  // while a search is up (it would replace the results with the folder), so the
  // search has to refresh itself — otherwise a deleted conversation sits on
  // screen until the query is retyped, which reads as the delete not working.
  async function reloadList() {
    if (App.search && App.search.isActive()) return App.search.rerun();
    if (!selection) return;
    $("#list-title").textContent = selection.title;
    await loadList(true);   // an archive from row 200 shouldn't snap back to page one
  }

  function connectSSE() {
    if (stream) stream.close();
    const es = new EventSource("/api/stream");
    stream = es;
    // "outbox" fires when a message is queued to send and when the agent has
    // been round to try: the count in the sidebar is only honest if it moves
    // without waiting out a poll.
    ["accounts", "messages", "flags", "cursor", "present", "folders", "extract",
     "outbox"].forEach((t) => es.addEventListener(t, scheduleRefresh));
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
    // A stream we closed on purpose is not an outage. Without the guard, going
    // to the background would raise the red "connection lost" bar and set the
    // watchdog probing on a loop — the opposite of standing down.
    es.onerror = () => { if (stream === es) App.conn.fail(); };
  }

  /* Stand the live half of the shell down while the app is in the background.

     The stream is the expensive part: every event it delivers costs a
     scheduleRefresh, which is five API calls and a full sidebar and list
     re-render. Closing it also drops the server's 15-second keepalive.

     Coming back does a full reload rather than trusting what is on screen —
     while the stream was shut, every change the agent made went unheard, so the
     list is exactly as stale as the pause was long. */
  function initPowerSave() {
    if (!App.power) return;
    App.power.whenSuspended(() => {
      clearTimeout(refreshTimer);
      if (stream) { const es = stream; stream = null; es.close(); }
    });
    App.power.whenResumed(() => {
      connectSSE();
      scheduleRefresh();
    });
  }

  // --- Settings modal (accounts) ---
  // The three per-account fields this modal owns — unless meerail.toml pins
  // them, in which case the server says so in `config_fields` and they are
  // shown here as set-elsewhere rather than edited. Order matters: it is the
  // order they are listed in the note.
  const FIELDS = ["label", "color", "footer"];
  const FIELD_NAMES = { label: "name", color: "colour", footer: "footer" };

  function pinnedNote(pinned) {
    const words = FIELDS.filter((f) => pinned.has(f)).map((f) => FIELD_NAMES[f]);
    const list = words.length > 1
      ? `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}`
      : words[0];
    const verb = words.length > 1 ? "are" : "is";
    return `The ${list} ${verb} set for this account in <code>meerail.toml</code>, ` +
      `so ${words.length > 1 ? "they are" : "it is"} not editable here.`;
  }

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
      // Fields the agent's meerail.toml pins. The control is left out entirely
      // rather than disabled — an editor that cannot be edited invites the
      // question this note answers — but the value is still shown, since the
      // colour and name are how the row identifies its account.
      const pinned = new Set(a.config_fields || []);
      const editable = FIELDS.filter((f) => !pinned.has(f));
      const li = document.createElement("li");
      li.innerHTML = `
        <div class="sa-row">
          ${pinned.has("color")
            ? `<span class="sa-swatch" style="background:${App.esc(a.color)}"
                 title="Account colour — set in meerail.toml"></span>`
            : `<input type="color" class="sa-color" data-color="${a.id}" value="${App.esc(a.color)}"
                 title="Account colour" />`}
          <span class="sa-main">
            ${pinned.has("label")
              ? `<div class="sa-name-fixed">${App.esc(a.label || a.email.split("@")[0])}</div>`
              : `<input type="text" class="sa-name" data-name="${a.id}" value="${App.esc(a.label)}"
                   placeholder="${App.esc(a.email.split("@")[0])}" aria-label="Account name" />`}
            <div class="sa-sub">${App.esc(a.email)} · agent ${App.relTime(a.last_agent_seen)}</div>
          </span>
          <span class="status-pill ${online ? "ok" : ""}">${online ? "online" : (a.backfill_complete ? "synced" : "waiting")}</span>
        </div>
        ${pinned.has("footer") ? "" : `
        <div class="sa-footer">
          <label for="footer-${a.id}">Footer — prefilled into the composer, editable per message</label>
          <textarea id="footer-${a.id}" data-footer="${a.id}" rows="3"
            placeholder="Empty — the composer opens without a footer">${App.esc(a.footer || "")}</textarea>
        </div>`}
        ${pinned.size ? `<div class="sa-pinned">${pinnedNote(pinned)}</div>` : ""}
        ${editable.length ? `
        <div class="sa-footer-actions">
          <button type="button" data-save="${a.id}">Save</button>
          <span class="sa-footer-status" data-save-status="${a.id}"></span>
        </div>` : ""}`;
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
      const base = accountFields(id);
      btn.addEventListener("click", () => saveAccount(id, base));
    });
  }

  // What the rendered controls currently hold, for the fields that have one — a
  // field pinned in meerail.toml has no control, and is simply absent here and
  // so from every payload built out of this.
  function accountFields(accountId) {
    const nodes = {
      label: $(`[data-name="${accountId}"]`),
      color: $(`[data-color="${accountId}"]`),
      footer: $(`[data-footer="${accountId}"]`),
    };
    const out = {};
    for (const field of FIELDS) if (nodes[field]) out[field] = nodes[field].value;
    return out;
  }

  async function saveAccount(accountId, base) {
    const status = $(`[data-save-status="${accountId}"]`);
    const now = accountFields(accountId);
    if ("label" in now) now.label = now.label.trim();
    // Send only what moved. Sending `footer` at all flips footer_customized on
    // the server, so a colour-only save must not carry it along.
    const payload = {};
    for (const field of Object.keys(now)) {
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

  // --- Settings modal (send delay) ---
  // Server-side, unlike the two below it: the delay decides when the *agent*
  // may send, so it cannot live in one browser's localStorage — the message
  // would go out on time from a machine that never had the preference.
  async function loadSendDelay() {
    const input = $("#send-delay");
    if (document.activeElement === input) return;   // never clobber a number being typed
    try {
      const cfg = await App.api.outboxSettings();
      input.value = cfg.send_delay_seconds;
    } catch (_) {}
  }

  async function saveSendDelay() {
    const status = $("#send-delay-status");
    status.classList.remove("error");
    const seconds = parseInt($("#send-delay").value, 10);
    if (isNaN(seconds) || seconds < 0) {
      status.textContent = "Enter a number of seconds";
      status.classList.add("error");
      return;
    }
    status.textContent = "Saving…";
    try {
      await App.api.saveOutboxSettings(seconds);
    } catch (e) {
      status.textContent = e.message || "Could not save";
      status.classList.add("error");
      return;
    }
    status.textContent = seconds ? `Saved — messages wait ${seconds}s` : "Saved — sending straight away";
    setTimeout(() => { status.textContent = ""; }, 2500);
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
    loadSendDelay();
    $("#theme-mode").value = App.theme.mode();
    $("#age-days").value = App.list.ageDays();
    $("#compose-html-default").checked = App.compose.htmlDefault();
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
    if (kind === "outbox") return select(selections["outbox"]);
    if (kind === "reminders") return select(selections["reminders"]);
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
    $("#send-delay-save").addEventListener("click", saveSendDelay);
    $("#send-delay").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); saveSendDelay(); }
    });
    // Immediate, like the age tint below it: the whole window repaints as you
    // pick, which is the only honest preview a theme picker can give.
    $("#theme-mode").addEventListener("change", (e) => App.theme.set(e.target.value));
    $("#age-days").addEventListener("input", applyAgeDays);
    // Like the age tint, this is local and takes effect immediately — but only
    // on the next draft: an open composer's button is that message's answer,
    // and the setting is not entitled to overrule it mid-sentence.
    $("#compose-html-default").addEventListener("change", (e) =>
      App.compose.setHtmlDefault(e.target.checked));
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
    // Before the initialisers that register with it, and before anything starts
    // a timer: a window that is already in the background at load should never
    // get as far as its first poll.
    App.power.init();
    try {
      await App.api.ensureSession();
      wire();
      App.mobile.init();
      App.search.init();
      App.compose.init();
      App.keys.init();
      App.bulk.init();
      App.tasks.init();
      App.status.init();
      App.undo.init();
      App.stats.init();
      // Last of the initialisers and deliberately fire-and-forget: an update
      // notice is the least urgent thing on the page, and it must not be able
      // to delay or fail the boot of the parts that show mail.
      App.update.init();
      initPowerSave();
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
           closeFolder, folderOpen, listSelector, currentTitle, currentRole,
           listTotal: () => listTotal,
           moveFolder, moveFolderAndOpen, openFocusedFolder, setFolderKeyFocus };
})();

document.addEventListener("DOMContentLoaded", App.shell.boot);
