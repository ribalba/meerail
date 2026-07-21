/* meerail core: shared App namespace — API client, icons, utilities. */

window.App = window.App || {};

// --- API client ---
App.api = {
  async ensureSession() {
    const status = await this.get("/api/auth/status");
    if (!status.required) return;
    const probe = await fetch("/api/mailboxes");
    if (probe.ok) return;
    const token = window.prompt("Enter the meerail server token:");
    if (!token) throw new Error("Authentication is required");
    await this.post("/api/auth/login", { token });
  },
  async request(method, path, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    if (res.status === 204) return null;
    const ct = res.headers.get("content-type") || "";
    return ct.includes("application/json") ? res.json() : res.text();
  },
  get(p) { return this.request("GET", p); },
  post(p, b) { return this.request("POST", p, b); },
  patch(p, b) { return this.request("PATCH", p, b); },
  del(p) { return this.request("DELETE", p); },

  mailboxes() { return this.get("/api/mailboxes"); },
  messages(params) {
    const qs = new URLSearchParams(params).toString();
    return this.get("/api/messages?" + qs);
  },
  message(id, images) { return this.get(`/api/messages/${id}?images=${images ? 1 : 0}`); },
  thread(id, accountId, images) { return this.get(`/api/threads/${encodeURIComponent(id)}?account_id=${accountId}&images=${images ? 1 : 0}`); },
  search(params) { return this.get("/api/search?" + new URLSearchParams(params).toString()); },
  accounts() { return this.get("/api/accounts"); },
  contacts(q) { return this.get("/api/contacts?q=" + encodeURIComponent(q)); },

  markSeen(id, seen) { return this.post(`/api/messages/${id}/mark?seen=${seen ? 1 : 0}`); },
  flagMsg(id, flagged) { return this.post(`/api/messages/${id}/flag?flagged=${flagged ? 1 : 0}`); },
  trashMsg(id, sourceMailboxId) { return this.post(`/api/messages/${id}/trash?source_mailbox_id=${sourceMailboxId}`); },
  archiveMsg(id, sourceMailboxId) { return this.post(`/api/messages/${id}/archive?source_mailbox_id=${sourceMailboxId}`); },
  replyContext(id, mode) { return this.get(`/api/compose/reply-context/${id}?mode=${mode}`); },
  sendMail(payload) { return this.post("/api/compose/send", payload); },
  async uploadAttachment(file) {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch("/api/compose/attachments", { method: "POST", body: fd });
    if (!res.ok) {
      let d = res.statusText; try { d = (await res.json()).detail || d; } catch (_) {}
      throw new Error(d);
    }
    return res.json();
  },
};

// --- Feather-style inline icons (24x24 stroke) ---
const ICON_PATHS = {
  inbox: '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
  sent: '<line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>',
  drafts: '<path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><polyline points="13 2 13 9 20 9"/>',
  archive: '<polyline points="21 8 21 21 3 21 3 8"/><rect x="1" y="3" width="22" height="5"/><line x1="10" y1="12" x2="14" y2="12"/>',
  trash: '<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
  junk: '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
  all: '<path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/>',
  folder: '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
  flag: '<path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" y1="22" x2="4" y2="15"/>',
  star: '<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>',
  paperclip: '<path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>',
  reply: '<polyline points="9 17 4 12 9 7"/><path d="M20 18v-2a4 4 0 0 0-4-4H4"/>',
  replyAll: '<polyline points="7 17 2 12 7 7"/><polyline points="12 17 7 12 12 7"/><path d="M22 18v-2a4 4 0 0 0-4-4H7"/>',
  forward: '<polyline points="15 17 20 12 15 7"/><path d="M4 18v-2a4 4 0 0 1 4-4h12"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
  search: '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
  refresh: '<polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>',
  close: '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
  edit: '<path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>',
  markunread: '<path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/>',
};

App.icon = function (name, size = 18, fill = false) {
  const p = ICON_PATHS[name] || ICON_PATHS.folder;
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="${fill ? 'currentColor' : 'none'}" ` +
    `stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${p}</svg>`;
};

App.roleIcon = function (role) {
  return { inbox: "inbox", sent: "sent", drafts: "drafts", archive: "archive",
    trash: "trash", junk: "junk", all: "all", flagged: "flag" }[role] || "folder";
};

// --- Utilities ---
App.esc = function (s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
};

App.fmtDate = function (iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const days = (now - d) / 86400000;
  if (days < 7 && days >= 0) return d.toLocaleDateString([], { weekday: "short" });
  if (d.getFullYear() === now.getFullYear()) return d.toLocaleDateString([], { month: "short", day: "numeric" });
  return d.toLocaleDateString([], { year: "2-digit", month: "numeric", day: "numeric" });
};

App.fmtDateFull = function (iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
};

App.initials = function (name, addr) {
  const s = (name || addr || "?").trim();
  const parts = s.split(/[\s@.]+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return (s[0] || "?").toUpperCase();
};

App.avatarColor = function (seed) {
  let h = 0;
  for (const c of String(seed || "")) h = (h * 31 + c.charCodeAt(0)) % 360;
  return `hsl(${h}, 62%, 48%)`;
};

App.fmtSize = function (n) {
  if (!n) return "";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(0) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
};
