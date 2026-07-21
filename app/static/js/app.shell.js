/* meerail shell: boots the app, renders the sidebar, wires selection,
   the settings modal, and live updates over SSE. */

App.shell = (function () {
  let sidebar = null;        // last /api/mailboxes payload
  let selection = null;      // { key, title, showAccount, params }
  let refreshTimer = null;

  const $ = (s) => document.querySelector(s);

  // --- Sidebar ---
  function mailboxRow(sel, iconName, name, count, activeKey) {
    const active = sel.key === activeKey ? " active" : "";
    const badge = count ? `<span class="mailbox-count">${count}</span>` : "";
    return `<div class="mailbox-row${active}" data-key="${sel.key}">
      <span class="mb-icon">${App.icon(iconName, 16)}</span>
      <span class="mailbox-name">${App.esc(name)}</span>${badge}</div>`;
  }

  const selections = {};   // key -> selection object

  function register(sel) { selections[sel.key] = sel; return sel; }

  function renderSidebar() {
    const tree = $("#mailbox-tree");
    const activeKey = selection ? selection.key : null;
    let html = "";

    const multi = sidebar.smart.account_count > 1;
    html += `<div class="tree-section">Favorites</div>`;
    if (multi) {
      register({ key: "unified", title: "All Inboxes", showAccount: true, params: { scope: "unified_inbox" } });
      html += mailboxRow(selections["unified"], "inbox", "All Inboxes",
        sidebar.smart.unified_inbox_unread, activeKey);
    }
    register({ key: "flagged", title: "Flagged", showAccount: true, params: { scope: "flagged" } });
    html += mailboxRow(selections["flagged"], "flag", "Flagged", sidebar.smart.flagged_total, activeKey);

    for (const acc of sidebar.accounts) {
      html += `<div class="account-head"><span class="account-dot" style="background:${App.esc(acc.color)}"></span>
        <span class="account-label">${App.esc(acc.label || acc.email)}</span></div>`;
      for (const mb of acc.mailboxes) {
        const key = "mb-" + mb.id;
        register({ key, title: mb.display_name, showAccount: false, params: { mailbox_id: mb.id } });
        html += mailboxRow(selections[key], App.roleIcon(mb.role), mb.display_name, mb.unread, activeKey);
      }
    }
    tree.innerHTML = html;
    tree.querySelectorAll(".mailbox-row").forEach((el) => {
      el.addEventListener("click", () => select(selections[el.dataset.key]));
    });
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

  async function loadList() {
    if (!selection) return;
    try {
      const data = await App.api.messages(Object.assign({ limit: 80 }, selection.params));
      App.list.render(data.rows, selection.showAccount);
    } catch (e) {
      document.getElementById("message-list").innerHTML =
        `<div class="list-empty">Could not load: ${App.esc(e.message)}</div>`;
    }
  }

  // --- Live updates ---
  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(async () => {
      sidebar = await App.api.mailboxes();
      renderSidebar();
      // Don't clobber a live search result set with the folder list.
      if (!App.search || !App.search.isActive()) await loadList();
    }, 500);
  }

  function currentMailboxId() {
    return selection && selection.params ? selection.params.mailbox_id || null : null;
  }

  async function reloadList() {
    if (!selection) return;
    $("#list-title").textContent = selection.title;
    await loadList();
  }

  function connectSSE() {
    const es = new EventSource("/api/stream");
    ["messages", "flags", "cursor", "present", "folders", "extract"].forEach((t) =>
      es.addEventListener(t, scheduleRefresh));
    es.onerror = () => { /* EventSource auto-reconnects */ };
  }

  // --- Settings modal (accounts) ---
  function relTime(iso) {
    if (!iso) return "never";
    const t = new Date(iso + (iso.endsWith("Z") ? "" : "Z"));
    const s = Math.round((Date.now() - t) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    return t.toLocaleDateString();
  }

  async function renderSettingsAccounts() {
    const list = $("#settings-account-list");
    let accounts = [];
    try { accounts = await App.api.accounts(); } catch (_) {}
    list.innerHTML = "";
    for (const a of accounts) {
      const online = a.last_agent_seen &&
        (Date.now() - new Date(a.last_agent_seen + "Z").getTime()) < 120000;
      const li = document.createElement("li");
      li.innerHTML = `
        <span class="account-dot" style="background:${a.color}"></span>
        <span class="sa-main">
          <div class="sa-email">${App.esc(a.label || a.email)}</div>
          <div class="sa-sub">${App.esc(a.email)} · agent ${relTime(a.last_agent_seen)}</div>
        </span>
        <span class="status-pill ${online ? "ok" : ""}">${online ? "online" : (a.backfill_complete ? "synced" : "waiting")}</span>
        <button class="link-btn" data-del="${a.id}">Remove</button>`;
      list.appendChild(li);
    }
    list.querySelectorAll("[data-del]").forEach((btn) =>
      btn.addEventListener("click", async () => {
        if (!confirm("Remove this account? Its synced mail is deleted from the server.")) return;
        await App.api.del(`/api/accounts/${btn.dataset.del}`);
        await renderSettingsAccounts();
        scheduleRefresh();
      }));
  }

  function openSettings() { $("#settings-modal").hidden = false; renderSettingsAccounts(); }
  function closeSettings() { $("#settings-modal").hidden = true; }

  function wire() {
    $("#btn-settings").innerHTML = App.icon("settings", 18);
    $("#btn-refresh").innerHTML = App.icon("refresh", 16);
    $("#btn-compose").innerHTML = App.icon("edit", 17);
    $("#btn-compose").addEventListener("click", () => App.compose.openNew());
    $("#btn-close-settings").innerHTML = App.icon("close", 18);
    $("#search-icon").innerHTML = App.icon("search", 15);
    $("#btn-settings").addEventListener("click", openSettings);
    $("#btn-close-settings").addEventListener("click", closeSettings);
    $("#settings-modal").addEventListener("click", (e) => {
      if (e.target.id === "settings-modal") closeSettings();
    });
    $("#btn-refresh").addEventListener("click", scheduleRefresh);

    $("#add-account").addEventListener("submit", async (e) => {
      e.preventDefault();
      const err = $("#add-error"); err.hidden = true;
      try {
        await App.api.post("/api/accounts", {
          email: $("#acc-email").value.trim(), label: $("#acc-label").value.trim(),
        });
        $("#add-account").reset();
        await renderSettingsAccounts();
        scheduleRefresh();
      } catch (ex) { err.textContent = ex.message; err.hidden = false; }
    });
  }

  async function boot() {
    await App.api.ensureSession();
    wire();
    App.search.init();
    App.compose.init();
    connectSSE();
    sidebar = await App.api.mailboxes();
    renderSidebar();
    // Default selection.
    if (sidebar.smart.account_count > 1) select(selections["unified"]);
    else if (sidebar.accounts.length) {
      const inbox = sidebar.accounts[0].mailboxes.find((m) => m.role === "inbox")
        || sidebar.accounts[0].mailboxes[0];
      if (inbox) select(selections["mb-" + inbox.id]);
    } else {
      $("#list-title").textContent = "meerail";
      document.getElementById("message-list").innerHTML =
        `<div class="list-empty">No accounts yet.<br>Open settings (top-left) to add one.</div>`;
      openSettings();
    }
  }

  return { boot, currentMailboxId, reloadList };
})();

document.addEventListener("DOMContentLoaded", App.shell.boot);
