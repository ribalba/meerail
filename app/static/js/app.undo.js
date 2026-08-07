/* meerail recent actions: what you last did to your mail, and a way back.

   The panel exists because undo without one is a guess. A button that says
   "Undo" and nothing else is asking the user to remember what the last keypress
   did — which folder, how many messages, whether the thing they are thinking of
   is even the most recent thing that happened. So the list comes first and the
   button hangs off each row: you undo *that archive of four messages*, not
   "the last action".

   It lists operations, not queue rows. One press of Delete over a two hundred
   row selection is one line here, and undoing it puts all two hundred back.
   The server groups them; see core/undo.py.

   Only mail-moving actions appear — trash, archive, move. Marking something read
   is undone by marking it unread, a send has its own window in the Outbox, and
   emptying the Trash is gone from the mail server. That last one does appear,
   greyed, saying so: the one destructive thing in the app should not also be the
   one invisible thing. */

App.undo = (function () {
  const $ = (s) => document.querySelector(s);
  const STORE_KEY = "meerail.recent.collapsed";

  // Long enough to be worth reading back, short enough that the sidebar box
  // stays a box. The server's own default is the same number.
  const LIMIT = 5;

  let items = [];
  let busy = null;        // op_id currently being undone, so it can't be double-pressed

  // Undo attempts that came back with a reason, by op_id, and which of those the
  // user has opened the ⓘ on.
  //
  // Held here rather than on the queue row, because a failed *attempt* is not a
  // property of the action — the commonest one by far is "the agent is holding
  // this, press it again in a moment", which is untrue a second later. Writing
  // that to the database would make a transient refusal outlive the thing it was
  // about. It survives every re-render (the panel redraws on a debounce, and an
  // error that vanished on the next SSE event would be unreadable); it does not
  // survive a reload, which is correct — by then it is not news.
  const failures = new Map();
  const opened = new Set();

  const VERB = {
    trash: "Trashed", archive: "Archived", move: "Moved",
    delete: "Deleted", undo: "Put back", remind: "Snoozed",
  };

  function collapsed() { return localStorage.getItem(STORE_KEY) === "1"; }

  function applyCollapsed(state) {
    const box = $("#recent-box");
    if (!box) return;
    box.classList.toggle("collapsed", state);
    const btn = box.querySelector(".sc-toggle");
    btn.setAttribute("aria-expanded", String(!state));
    btn.title = state ? "Show recent actions" : "Minimize";
    box.querySelector(".sc-glyph").innerHTML = App.icon(state ? "chevron" : "minimize", 14);
    localStorage.setItem(STORE_KEY, state ? "1" : "0");
  }

  // What one operation did, in the terms the user pressed it in. "Archived 4"
  // rather than "move", and the destination named the way the sidebar names it.
  function line(item) {
    const verb = VERB[item.kind] || "Moved";
    const what = item.count > 1 ? `${item.count} messages` : (item.subject || "1 message");
    const where = item.kind === "move" && item.to ? ` to ${item.to}`
                : item.kind === "undo" && item.to ? ` in ${item.to}`
                : "";
    return `${verb} ${what}${where}`;
  }

  function render() {
    const box = $("#recent-box");
    if (!box) return;
    box.hidden = false;

    // Shown empty rather than hidden. Hiding it was the obvious economy — why
    // spend sidebar on a list of nothing — and it was wrong: a panel that only
    // exists once you have used it is indistinguishable from one that is
    // broken, and the first thing anybody does after an upgrade is look for it.
    // It is one line, it says what will appear here, and it collapses like
    // everything else in this column if you would rather not see it.
    //
    // Nothing filed before this version appears, ever: the record an undo needs
    // is written when the action is queued (core/undo.py), and mail moved by an
    // older build carries none. There is no way to say that in the sidebar
    // without it outliving its usefulness by months, so it is in the README.
    const rows = !items.length
      ? `<div class="rc-empty">Mail you trash, archive or move shows up here.</div>`
      : items.map((item) => {
          const when = item.at ? App.relTime(item.at) : "";
          // An attempt that failed outranks the server's own verdict on the row:
          // it is more recent, more specific, and it is the answer to the thing
          // the user just did.
          const failed = failures.get(item.op_id) || null;
          const why = failed || (item.undoable ? null : item.reason);
          const disabled = !item.undoable || busy === item.op_id;
          // Spelled out under the row rather than left in a `title`. A tooltip
          // is the one place a reason can be that gives no sign it is there —
          // the row simply looks broken, and hovering to find out is not
          // something anybody does on a sidebar they were not asking about.
          const note = why && opened.has(item.op_id)
            ? `<div class="rc-why">${App.esc(why)}</div>` : "";
          return `<div class="rc-row${failed ? " failed" : ""}">
            <div class="rc-what">
              <span class="rc-line">${App.esc(line(item))}</span>
              <span class="rc-when">${App.esc(when)}</span>
            </div>
            ${why ? `<button class="rc-info" type="button" data-info="${App.esc(item.op_id)}"
                     aria-label="Why?" title="Why?">${App.icon("info", 13)}</button>` : ""}
            ${item.pending
              ? `<span class="rc-settling" title="Still being recorded">
                   ${App.icon("refresh", 13)}</span>`
              : `<button class="rc-undo" type="button" data-op="${App.esc(item.op_id)}"
                        ${disabled ? "disabled" : ""}>
                   ${busy === item.op_id ? "…"
                     : failed && item.undoable ? "Retry" : "Undo"}
                 </button>`}
          </div>${note}`;
        }).join("");

    box.innerHTML = `
      <button class="sc-toggle" type="button" aria-expanded="true">
        <span>Recent actions</span>
        <span class="sc-glyph"></span>
      </button>
      <div class="sc-body">${rows}</div>`;
    box.querySelector(".sc-toggle").addEventListener("click",
      () => applyCollapsed(!collapsed()));
    box.querySelectorAll(".rc-undo").forEach((btn) => {
      btn.addEventListener("click", () => run(btn.dataset.op));
    });
    box.querySelectorAll(".rc-info").forEach((btn) => {
      btn.addEventListener("click", () => {
        const op = btn.dataset.info;
        opened.has(op) ? opened.delete(op) : opened.add(op);
        render();
      });
    });
    applyCollapsed(collapsed());
  }

  async function refresh() {
    try {
      const res = await App.api.recentActions(LIMIT);
      items = (res && res.items) || [];
    } catch (_) {
      // A panel that cannot load is not worth a banner — the mail is fine and
      // the last list stays on screen. Anything genuinely wrong with the server
      // is already being reported by App.conn.
      return;
    }
    render();
  }

  async function run(opId) {
    if (!opId || busy) return;
    busy = opId;
    failures.delete(opId);          // this attempt's answer replaces the last one
    render();                       // the pressed row goes to "…" straight away
    let ok = true;
    try {
      await App.api.undoAction(opId);
    } catch (e) {
      // Kept against the row rather than thrown at a dialog. Every one of these
      // is about one entry in a list that is on screen, and the commonest is
      // "the agent is holding this, try again in a moment" — which as a modal
      // is an interruption demanding to be dismissed, and as a red row with a
      // Retry beside it is just the state of that row.
      ok = false;
      failures.set(opId, e.message || "That could not be undone.");
      opened.add(opId);             // the first sight of it should say why
    } finally {
      busy = null;
    }
    // On success the entry is gone from the server's list, so this is what makes
    // it disappear. On failure it comes back with whatever the server now thinks
    // of it, and render() puts the red state on top.
    await refresh();
    if (ok && App.shell && App.shell.reloadList) App.shell.reloadList();
  }

  // The keyboard's version: take back the newest thing on the list. An
  // operation that has been undone is not on it, so pressing this repeatedly
  // walks backwards a step at a time, which is what an undo key is expected to
  // do.
  //
  // It re-reads the list first rather than trusting what is on screen. The panel
  // refreshes on the SSE debounce, half a second behind the keypress that
  // changed anything, so a fast `a` then `z` would otherwise find the archive
  // missing from `items` and undo the action *before* it — putting back the
  // wrong mail, silently, which is the one failure this key cannot have.
  async function undoLast() {
    await refresh();
    const next = items[0];
    if (!next) return;
    // Stops at an operation that cannot be undone instead of reaching past it.
    // Emptying the Trash is the case that matters: skipping over it would make
    // one press of undo restore an archive from ten minutes ago, having said
    // nothing about the thing the user was actually thinking of. The reason is
    // opened against the row, where a keypress that appeared to do nothing can
    // be traced to the line explaining itself.
    if (!next.undoable) {
      opened.add(next.op_id);
      render();
      return;
    }
    await run(next.op_id);
  }

  // Which endpoint produced which entry. Only the verb is guessed — everything
  // else on the optimistic row comes from the response or is left blank until
  // the server's own version of the row arrives a moment later.
  const KIND_FOR = [
    [/\/trash(\?|$)/, "trash"],
    [/\/archive(\?|$)/, "archive"],
    [/\/move\?/, "move"],
    [/\/remind$/, "remind"],
    [/\/bulk\/trash/, "trash"],
  ];

  /* Put the entry on the list now, before the server has been asked for it.

     The panel used to appear only when app.shell's SSE handler got round to
     refreshing everything, and that could take a very long time: the handler
     clears its own timer on every event, so during a sync — which is exactly
     when mail is being filed — each new event pushed the refresh further out,
     and the refresh then waited on a full mailbox reload before reaching this
     panel. Archiving something and watching nothing happen for half a minute
     made the whole box look broken.

     So the action's own response is what puts the row up, and the refresh that
     follows is this panel's alone rather than the shared one. The row is marked
     pending until that returns: it is a real operation and undoing it would
     work, but the count and the subject are not known yet, and a row that
     rewrote itself under the pointer a moment after appearing would be worse
     than one that says it is still settling. */
  function record(path, body) {
    const hit = KIND_FOR.find(([re]) => re.test(path));
    if (!hit) return;
    items = [{
      op_id: body.op_id,
      kind: hit[1],
      count: body.moved || 1,
      subject: "",
      at: new Date().toISOString(),
      undoable: true,
      reason: null,
      pending: true,
      // Trimmed here as well as by the server: this row goes up before the
      // refresh that would have bounded the list, and without the slice a run
      // of actions grows the panel one line at a time until that lands.
    }, ...items.filter((item) => item.op_id !== body.op_id)].slice(0, LIMIT);
    render();
    refresh();
  }

  function init() { refresh(); }

  return { init, refresh, record, undoLast };
})();
