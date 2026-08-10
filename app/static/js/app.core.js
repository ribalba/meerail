/* meerail core: shared App namespace — API client, icons, utilities. */

window.App = window.App || {};

// Inert stand-in for the connection watchdog, replaced by app.conn.js when it
// loads. The API client below reports every request outcome here, so a missing
// or late-loading app.conn.js must cost us the offline bar — never the request.
App.conn = App.conn || {
  init() {}, fail() {}, ok() {}, whenRestored() {}, isDown: () => false,
};

// --- API client ---
App.api = {
  async ensureSession() {
    const status = await this.get("/api/auth/status");
    if (!status.required) return;
    const probe = await fetch("/api/mailboxes");
    if (probe.ok) return;
    await this.promptLogin();
  },
  // Full-screen password gate. Returns a promise that resolves once a login
  // succeeds; concurrent callers (several requests hitting 401 at once, as
  // happens when the 30-day session expires mid-use) share one overlay and one
  // promise instead of stacking prompts.
  promptLogin() {
    if (this._loginPromise) return this._loginPromise;
    const overlay = document.getElementById("login-overlay");
    const form = document.getElementById("login-form");
    const input = document.getElementById("login-password");
    const error = document.getElementById("login-error");
    const submit = document.getElementById("login-submit");
    this._loginPromise = new Promise((resolve) => {
      overlay.hidden = false;
      input.value = "";
      error.hidden = true;
      setTimeout(() => input.focus(), 0);
      form.onsubmit = async (e) => {
        e.preventDefault();
        if (!input.value) return;
        submit.disabled = true;
        try {
          await this.post("/api/auth/login", { password: input.value });
        } catch (err) {
          error.textContent = err.message || "Login failed";
          error.hidden = false;
          input.select();
          return;
        } finally {
          submit.disabled = false;
        }
        overlay.hidden = true;
        form.onsubmit = null;
        this._loginPromise = null;
        resolve();
      };
    });
    return this._loginPromise;
  },
  async request(method, path, body) {
    const opts = { method, headers: {} };
    if (body instanceof FormData) {
      // No Content-Type of ours: a multipart body needs the browser to write
      // the header, boundary and all. Everything else about the request — the
      // watchdog, the 401 replay — is the same, which is the point of it coming
      // through here at all.
      opts.body = body;
    } else if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let res;
    // A rejected fetch means the request never landed — the server is gone, DNS
    // failed, or the network is down. HTTP errors below are the opposite: proof
    // that something answered. Both are reported to the connection watchdog,
    // which decides whether to raise the offline bar.
    try {
      res = await fetch(path, opts);
    } catch (err) {
      App.conn.fail();
      throw err;
    }
    App.conn.ok();
    // A 401 outside the auth endpoints means the session cookie expired (or
    // never existed): raise the password gate, then replay the request. The
    // auth endpoints are excluded so a wrong password surfaces as an error in
    // the login form rather than re-opening it forever.
    if (res.status === 401 && !path.startsWith("/api/auth/")) {
      await this.promptLogin();
      return this.request(method, path, body);
    }
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    if (res.status === 204) return null;
    const ct = res.headers.get("content-type") || "";
    // Not `body` — that is this method's own parameter, and shadowing it here
    // threw at parse time, which took the whole file (and so the whole app) with it.
    const answer = ct.includes("application/json") ? await res.json() : await res.text();
    // Anything that files mail hands back the id of the operation it created.
    // Caught here, once, rather than at the five call sites that trash and
    // archive things — the reader, the swipe, the bulk bar, the list and the
    // keyboard all go through this method, and every one of them would
    // otherwise need the same two lines and would be the place somebody forgot
    // them. See app.undo.js::record for what it does with it.
    if (answer && answer.op_id && App.undo) App.undo.record(path, answer);
    return answer;
  },
  get(p) { return this.request("GET", p); },
  post(p, b) { return this.request("POST", p, b); },
  patch(p, b) { return this.request("PATCH", p, b); },
  put(p, b) { return this.request("PUT", p, b); },
  del(p) { return this.request("DELETE", p); },

  mailboxes() { return this.get("/api/mailboxes"); },
  favoriteMailbox(id, favorite) {
    return this.patch(`/api/mailboxes/${id}/favorite?favorite=${favorite ? 1 : 0}`);
  },
  createMailbox(accountId, name) {
    return this.post("/api/mailboxes", { account_id: accountId, name });
  },
  messages(params) {
    const qs = new URLSearchParams(params).toString();
    return this.get("/api/messages?" + qs);
  },
  message(id, images) { return this.get(`/api/messages/${id}?images=${images ? 1 : 0}`); },
  // `search` (optional) carries the query the thread was opened from, so the
  // server can point out which attachments the term was found in.
  thread(id, accountId, images, search) {
    const p = new URLSearchParams({ account_id: accountId, images: images ? 1 : 0 });
    if (search && search.q) { p.set("q", search.q); p.set("mode", search.mode); }
    return this.get(`/api/threads/${encodeURIComponent(id)}?${p}`);
  },
  search(params) { return this.get("/api/search?" + new URLSearchParams(params).toString()); },
  accounts() { return this.get("/api/accounts"); },
  contacts(q) { return this.get("/api/contacts?q=" + encodeURIComponent(q)); },
  relatedContacts(addresses) {
    const p = new URLSearchParams();
    addresses.forEach((a) => p.append("address", a));
    return this.get("/api/contacts/related?" + p.toString());
  },

  // The stats modal draws every panel from this one payload. tz_offset is sent
  // because hour-of-day and weekday buckets are computed in the database, and
  // the server stores naive UTC — without it every "when do I get mail" panel
  // would be shifted by the reader's own offset.
  analytics(params) {
    return this.get("/api/analytics/overview?" + new URLSearchParams(params).toString());
  },

  // The Recent actions panel: what the last few keypresses did to the mail, and
  // taking one back. Operations, not queue rows — one bulk delete is one entry.
  recentActions(limit) { return this.get(`/api/actions/recent?limit=${limit}`); },
  undoAction(opId) { return this.post(`/api/actions/${encodeURIComponent(opId)}/undo`); },

  requestSync() { return this.post("/api/sync/refresh"); },
  requestRecheck(email) { return this.post("/api/sync/recheck?email=" + encodeURIComponent(email)); },
  syncStatus() { return this.get("/api/sync/status"); },
  dismissDropped() { return this.post("/api/sync/actions/dismiss"); },
  createArchiveFolder(accountId) {
    return this.post(`/api/sync/actions/create-archive?account_id=${accountId}`);
  },

  markSeen(id, seen) { return this.post(`/api/messages/${id}/mark?seen=${seen ? 1 : 0}`); },
  flagMsg(id, flagged) { return this.post(`/api/messages/${id}/flag?flagged=${flagged ? 1 : 0}`); },
  trashMsg(id, sourceMailboxId) { return this.post(`/api/messages/${id}/trash?source_mailbox_id=${sourceMailboxId}`); },
  archiveMsg(id, sourceMailboxId) { return this.post(`/api/messages/${id}/archive?source_mailbox_id=${sourceMailboxId}`); },
  moveMsg(id, mailboxId, sourceMailboxId) {
    return this.post(`/api/messages/${id}/move?mailbox_id=${mailboxId}&source_mailbox_id=${sourceMailboxId}`);
  },
  // Whole-conversation versions: the server works out which messages and which
  // folders, so nothing ingested since the reader opened is left behind.
  archiveThread(threadId, accountId) {
    return this.post(`/api/messages/threads/${encodeURIComponent(threadId)}/archive?account_id=${accountId}`);
  },
  trashThread(threadId, accountId) {
    return this.post(`/api/messages/threads/${encodeURIComponent(threadId)}/trash?account_id=${accountId}`);
  },
  // Bulk: `items` is the explicit selection, `trashAll` takes the list selector
  // itself and deletes a chunk of everything matching it — see app.bulk.js.
  bulkTrash(items) { return this.request("POST", "/api/messages/bulk/trash", { items }); },
  bulkTrashAll(selector) { return this.request("POST", "/api/messages/bulk/trash-all", selector); },
  // The same two tiers for filing rather than deleting: the ticked rows, or
  // everything the selector matches, a chunk per call. Both take one folder,
  // and the server refuses a selection that is not all in its account.
  bulkMove(items, targetMailboxId) {
    return this.request("POST", "/api/messages/bulk/move",
                        { items, target_mailbox_id: targetMailboxId });
  },
  bulkMoveAll(selector, targetMailboxId) {
    return this.request("POST", "/api/messages/bulk/move-all",
                        { ...selector, target_mailbox_id: targetMailboxId });
  },
  // The one call that destroys mail, and the only one that has to say so: it
  // deletes the Trash folder's contents from the server, a chunk per call.
  // `confirm` is the server's requirement, not a formality — see
  // app/routers/actions.py::bulk_empty_trash.
  emptyTrash(mailboxId) {
    return this.request("POST", "/api/messages/bulk/empty-trash",
                        { mailbox_id: mailboxId, confirm: true });
  },
  // "Remind me": the conversation is filed away now and comes back at `due`.
  // The instant is computed here rather than asked for by name because "next
  // Monday, nine o'clock" is a question about this browser's calendar and
  // timezone — the server has neither, and would get it wrong twice a year at
  // the daylight-saving boundary. toISOString() sends it as absolute UTC.
  remind(id, due) {
    return this.post(`/api/messages/${id}/remind`, { due_at: due.toISOString() });
  },
  // restore=1 brings the mail back now, restore=0 leaves it filed and forgets
  // the reminder. Two different intentions, so never inferred from one button.
  unremind(id, restore) {
    return this.del(`/api/messages/${id}/remind?restore=${restore ? 1 : 0}`);
  },
  reminders() { return this.get("/api/reminders"); },

  // The two AI features (app/routers/ai.py). Every one of these is proxied by
  // the server: the provider key is never sent to the page, so there is no
  // endpoint here that returns one and none of these calls carries one except
  // the two that are saving or probing a key the user just typed.
  aiConfig() { return this.get("/api/ai/config"); },
  saveAiConfig(cfg) { return this.put("/api/ai/config", cfg); },
  clearAiConfig() { return this.del("/api/ai/config"); },
  // POST, not GET: the key being tried is in the body, and a key in a query
  // string is a key in an access log.
  aiModels(probe) { return this.post("/api/ai/models", probe); },
  aiSearch(description) { return this.post("/api/ai/search", { description }); },
  aiThread(payload) { return this.post("/api/ai/thread", payload); },
  // "When should this come back?" — the reader's own clock goes with it, because
  // the answer is a moment on *this* calendar and the server has none. Same
  // reason App.api.remind sends an absolute instant rather than a preset name.
  aiRemindSuggest(messageId, now, timezone) {
    return this.post("/api/ai/remind-suggest", { message_id: messageId, now, timezone });
  },
  aiAttachment(payload) { return this.post("/api/ai/attachment", payload); },

  taskConfig() { return this.get("/api/tasks/config"); },
  saveTaskConfig(url) { return this.request("PUT", "/api/tasks/config", { url }); },
  taskOptions() { return this.get("/api/tasks/options"); },
  createTask(payload) { return this.post("/api/tasks", payload); },

  replyContext(id, mode) { return this.get(`/api/compose/reply-context/${id}?mode=${mode}`); },
  // Which of the user's own addresses they normally write to these people from.
  // Resolves to null when the mailbox has nothing to say about them.
  senderFor(addresses) {
    const p = new URLSearchParams();
    addresses.forEach((a) => p.append("address", a));
    return this.get("/api/compose/sender-for?" + p.toString());
  },
  sendMail(payload) { return this.post("/api/compose/send", payload); },

  // The outbox: what has been written here and not yet handed to a mail server.
  // A queue of this app's own, not an IMAP folder, so it has endpoints of its
  // own rather than riding on /api/messages.
  outbox() { return this.get("/api/outbox"); },
  outboxMessage(id) { return this.get(`/api/outbox/${id}`); },
  outboxRetry(id) { return this.post(`/api/outbox/${id}/retry`); },
  outboxCancel(id) { return this.post(`/api/outbox/${id}/cancel`); },
  outboxDiscard(id) { return this.del(`/api/outbox/${id}`); },
  outboxSettings() { return this.get("/api/outbox/settings"); },
  saveOutboxSettings(seconds) {
    return this.put("/api/outbox/settings", { send_delay_seconds: seconds });
  },
  deleteAttachment(id) { return this.del(`/api/compose/attachments/${encodeURIComponent(id)}`); },
  // Multipart, so it cannot go through request() — that one JSON-encodes the
  // body and sets a Content-Type, and a FormData upload needs the browser to
  // set its own boundary. Everything else about it is the same call, and the
  // part that matters is the 401: an upload is the slowest thing in the UI and
  // therefore the likeliest to outlive a session, and it used to be the one
  // request that answered that with a raw "401" in an alert instead of the
  // password prompt every other request raises.
  uploadAttachment(file) {
    const fd = new FormData();
    fd.append("file", file);
    return this.request("POST", "/api/compose/attachments", fd);
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
  move: '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/><polyline points="12 11 15 14 12 17"/><line x1="8" y1="14" x2="15" y2="14"/>',
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
  plus: '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
  edit: '<path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>',
  markunread: '<path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/>',
  chevron: '<polyline points="6 9 12 15 18 9"/>',
  // A sheet with lines of text — the "show me the plain-text part" toggle.
  plaintext: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/>',
  // Angle brackets — "show me the raw message", the way every editor spells it.
  code: '<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>',
  info: '<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="11"/><line x1="12" y1="8" x2="12.01" y2="8"/>',
  minimize: '<line x1="5" y1="18" x2="19" y2="18"/>',
  activity: '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
  task: '<polyline points="9 11 12 14 21 5"/><path d="M20 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h10"/>',
  warning: '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
  // Two stacked speech bubbles — a conversation with more than one message.
  thread: '<path d="M21 9a2 2 0 0 0-2-2H9a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2h1v4l4-4h5a2 2 0 0 0 2-2z"/><path d="M17 4a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2h2"/>',
  // Rising bars — the stats modal.
  stats: '<line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/>',
  // A clock — mail whose body is outside the content window.
  clock: '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
  // A bell — "remind me", and the folder of mail waiting on one.
  bell: '<path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>',
  // Tray with an arrow into it — save the attachment you are looking at.
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
  // Arrow leaving a box — hand the attachment to the browser's own viewer.
  external: '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>',
  // Asleep rather than broken — the background power-save screen.
  moon: '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>',
  // A little robot: the two places a language model is asked something. The eyes
  // are zero-length lines, which the round line-cap draws as dots — a circle
  // that small disappears at 15px.
  robot: '<rect x="4" y="9" width="16" height="11" rx="2"/><path d="M12 9V5.5"/><circle cx="12" cy="4" r="1.5"/><line x1="9" y1="14" x2="9" y2="14.01"/><line x1="15" y1="14" x2="15" y2="14.01"/><line x1="2" y1="13" x2="2" y2="16"/><line x1="22" y1="13" x2="22" y2="16"/>',
  // A tick in a speech bubble — "use this answer".
  check: '<polyline points="20 6 9 17 4 12"/>',
  // Three dots — the actions that did not fit on a toolbar. Drawn filled
  // (App.icon's third argument): as outlines at this size they read as rings.
  more: '<circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/>',
  // Two sheets — copy the answer out.
  copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
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

// The API serves naive UTC timestamps, which JS would otherwise read as local
// time. Everything that parses one from the API goes through here.
App.utcDate = function (iso) {
  if (!iso) return null;
  const d = new Date(iso + (iso.endsWith("Z") ? "" : "Z"));
  return isNaN(d) ? null : d;
};

App.ageSeconds = function (iso) {
  const d = App.utcDate(iso);
  return d === null ? null : (Date.now() - d.getTime()) / 1000;
};

App.relTime = function (iso) {
  const d = App.utcDate(iso);
  if (d === null) return "never";
  const s = Math.round((Date.now() - d.getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return d.toLocaleDateString();
};

App.fmtDate = function (iso) {
  const d = App.utcDate(iso);
  if (d === null) return "";
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const days = (now - d) / 86400000;
  if (days < 7 && days >= 0) return d.toLocaleDateString([], { weekday: "short" });
  if (d.getFullYear() === now.getFullYear()) return d.toLocaleDateString([], { month: "short", day: "numeric" });
  return d.toLocaleDateString([], { year: "2-digit", month: "numeric", day: "numeric" });
};

App.fmtDateFull = function (iso) {
  const d = App.utcDate(iso);
  return d === null ? "" : d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
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

// Written for one attachment, but the statistics modal now feeds it a whole
// window's worth of mail, hence the GB step: without it a 4 GB year reads
// "4096.0 MB", which is a number nobody parses at a glance.
App.fmtSize = function (n) {
  if (!n) return "";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(0) + " KB";
  if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
  return (n / 1073741824).toFixed(2) + " GB";
};
