/* meerail message list: date-descending rows for the selected mailbox/scope.

   Two cursors live here and they are deliberately distinct:
     activeId — the thread currently shown in the reading pane
     focusId  — the keyboard cursor, moved by j/k without opening anything
   Keeping them separate is what stops j/k from firing a thread fetch (and
   marking mail read) on every keystroke while you skim. The arrow keys go
   through moveAndOpen() instead and do open as they move. */

App.list = (function () {
  const AGE_KEY = "meerail.age-days";
  const AGE_DEFAULT = 30;

  let activeId = null;
  let focusId = null;
  let rows = [];             // [{ id, data, el }] in render order
  let tintOn = false;        // set per render by the caller — see render()
  let keyFocus = false;      // is the list the pane holding the keyboard?
  let selected = new Set();  // row ids ticked for a bulk action
  let anchorId = null;       // where the last plain tick landed, for shift-range
  let moreFn = null;         // caller's "fetch the next page" hook, or null at the end

  // --- Age tint ---
  // How many days of age take a row from untinted to fully red. 0 turns the
  // whole thing off.
  function ageDays() {
    const raw = parseInt(localStorage.getItem(AGE_KEY), 10);
    if (isNaN(raw) || raw < 0) return AGE_DEFAULT;
    return raw;
  }

  function setAgeDays(days) {
    localStorage.setItem(AGE_KEY, String(Math.max(0, days | 0)));
    // Repaint in place: the rows already on screen hold the data we need, so
    // there is no reason to go back to the server for a display preference.
    for (const r of rows) tint(r.el, r.data.date);
  }

  function tint(el, iso) {
    const days = ageDays();
    const d = iso ? new Date(iso) : null;
    if (!tintOn || !days || !d || isNaN(d)) return el.style.removeProperty("--age-t");
    const age = (Date.now() - d.getTime()) / 86400000;
    // Clamped both ways: future-dated mail (clock skew) stays white, and
    // anything at or past the horizon sits at full red rather than overshooting.
    el.style.setProperty("--age-t", Math.min(1, Math.max(0, age / days)).toFixed(3));
  }

  // --- Bulk selection ---
  // Selection is by row id and deliberately survives a re-render (see render()),
  // so mail arriving mid-review doesn't silently drop what you had ticked. Rows
  // that genuinely went away — the ones you just deleted — fall out of the set
  // because their ids are no longer in `rows`.
  function changed() {
    if (App.bulk) App.bulk.sync();
  }

  function paint(r) {
    const on = selected.has(r.id);
    r.el.classList.toggle("selected", on);
    const box = r.el.querySelector(".msg-check");
    if (box) box.checked = on;
  }

  function toggle(id, on) {
    if (on) selected.add(id); else selected.delete(id);
    const r = rows.find((x) => x.id === id);
    if (r) paint(r);
  }

  // Shift-tick fills in everything between the last plain tick and this one,
  // matching the anchor's new state rather than flipping each row individually.
  function range(id, on) {
    const a = rows.findIndex((x) => x.id === anchorId);
    const b = rows.findIndex((x) => x.id === id);
    if (a < 0 || b < 0) return toggle(id, on);
    for (let i = Math.min(a, b); i <= Math.max(a, b); i++) toggle(rows[i].id, on);
  }

  // Driven off `click` rather than `change`: a change event is not a MouseEvent
  // and carries no shiftKey, so range-ticking read as a plain tick. By the time
  // click fires the browser has already flipped the box, so .checked is the new
  // state either way.
  function onTick(r, e) {
    const on = e.target.checked;
    if (e.shiftKey && anchorId !== null) range(r.id, on);
    else { toggle(r.id, on); anchorId = r.id; }
    changed();
  }

  function selectAllLoaded() {
    for (const r of rows) { selected.add(r.id); paint(r); }
    anchorId = rows.length ? rows[rows.length - 1].id : null;
    changed();
    return rows.length;
  }

  function clearSelection() {
    selected.clear();
    anchorId = null;
    for (const r of rows) paint(r);
    changed();
  }

  // The row payloads, not the ids: a bulk trash needs thread_id/account_id off
  // each one to say what it is deleting.
  function selection() {
    return rows.filter((r) => selected.has(r.id)).map((r) => r.data);
  }

  function open(r, el) {
    activeId = r.id;
    document.querySelectorAll(".msg-row.active").forEach((n) => n.classList.remove("active"));
    el.classList.add("active");
    if (!r.thread_id) return;
    // Every caller of open() is someone asking to read this conversation —
    // a tap, or Enter/j-k on the cursor — so the narrow layout turns the page
    // here rather than at each call site.
    App.mobile.show("reader");
    App.reader.openThread(r.thread_id, r.account_id, r.id);
    markSeen(r.id);
  }

  // The reader marks the conversation read as it opens it; this is the row
  // saying so. In a folder the dot would clear on its own once the agent's
  // write-back arrived and the list reloaded, but search results are not
  // refreshed by those events (see app.shell.js), so without this the row a
  // `:unread` search turned up stays bold after being read.
  function markSeen(id) {
    const r = rows.find((x) => x.id === id);
    if (!r || r.data.seen) return;
    r.data.seen = true;
    r.el.classList.remove("unread");
    const dot = r.el.querySelector(".unread-dot");
    if (dot) dot.remove();
  }

  function row(r, showAccount) {
    const el = document.createElement("div");
    el.className = "msg-row" + (r.seen ? "" : " unread") + (r.id === activeId ? " active" : "")
      + (selected.has(r.id) ? " selected" : "");
    el.dataset.id = r.id;
    el.dataset.thread = r.thread_id || "";
    tint(el, r.date);

    const stripe = showAccount
      ? `<span class="acct-stripe" style="background:${App.esc(r.account_color)}"></span>` : "";
    const dot = r.seen ? "" : `<span class="unread-dot"></span>`;
    const check = `<input type="checkbox" class="msg-check" tabindex="-1"` +
      `${selected.has(r.id) ? " checked" : ""} aria-label="Select this conversation" />`;
    const flag = r.flagged ? `<span class="flag-dot">${App.icon("flag", 13, true)}</span>` : "";
    const attach = r.has_attachments ? `<span class="attach-glyph">${App.icon("paperclip", 12)}</span>` : "";
    // Only search puts folders on a row. A folder list never needs them — you
    // can see which folder you are standing in — but a search reads the whole
    // mailbox, and where it found a result is half of what it is answering:
    // Trash, Sent, or the folder a rule filed the mail into two years ago.
    // Deleting from a search view and watching the rows sit there is the case
    // that made this necessary; a row that now says "Trash" in words is the
    // answer to it. Add `:no-trash` to leave those out altogether.
    const where = App.folderChips(r.folders, r.account_id);
    const badge = r.thread_count > 1
      ? `<span class="thread-badge" title="${r.thread_count} messages in this thread">` +
        `${App.icon("thread", 11)}${r.thread_count}</span>` : "";
    // A conversation put off until later. Shown in every list, not only in the
    // Reminders one: the mail is sitting in Archive, and the difference between
    // "filed away" and "coming back on Monday" is exactly what this says.
    const due = r.remind_at ? App.utcDate(r.remind_at) : null;
    const remind = due
      ? `<span class="remind-chip" title="Coming back ${App.esc(App.fmtDateFull(r.remind_at))}"
          >${App.icon("bell", 11)}${App.esc(App.reminders.stamp(due))}</span>` : "";
    // No snippet because there is no body: this mail is outside the content
    // window the agent keeps. Blank there reads as a broken row.
    const absent = r.content_status && r.content_status !== "full";

    el.innerHTML = `
      ${stripe}
      <div class="msg-gutter">${check}${dot}</div>
      <div class="msg-main">
        <div class="msg-line1">
          <span class="msg-sender">${App.esc(r.from_name || r.from_addr || "Unknown")}</span>
          <span class="msg-date">${App.esc(App.fmtDate(r.date))}</span>
        </div>
        <div class="msg-subject">${App.esc(r.subject)}</div>
        <div class="msg-snippet${absent ? " is-absent" : ""}">${
          absent ? "Outside the sync window — headers only" : App.esc(r.snippet || "")}</div>
        <div class="msg-meta">${badge}${remind}${where}${flag}${attach}</div>
      </div>`;

    const box = el.querySelector(".msg-check");
    box.addEventListener("click", (e) => {
      e.stopPropagation();          // ticking is not opening
      onTick(r, e);
    });

    el.addEventListener("click", () => {
      // The tail of a swipe can still arrive here as a tap; opening the mail
      // that was just filed away is not what the gesture asked for.
      if (App.swipe && App.swipe.blocked()) return;
      setFocus(rows.findIndex((x) => x.id === r.id), false);
      // Clicking into the list takes the keyboard with it, so j/k carry on from
      // the row you pointed at instead of walking the sidebar.
      if (App.keys) App.keys.focus("list");
      open(r, el);
    });
    // Swipe-to-file, touch and narrow layout only — see app.swipe.js.
    if (App.swipe) App.swipe.attach(el, r);
    return el;
  }

  // --- Keyboard cursor ---
  function focusIndex() { return rows.findIndex((x) => x.id === focusId); }

  function setFocus(idx, scroll = true) {
    document.querySelectorAll(".msg-row.focused").forEach((n) => n.classList.remove("focused"));
    if (idx < 0 || idx >= rows.length) { focusId = null; return; }
    const r = rows[idx];
    focusId = r.id;
    r.el.classList.add("focused");
    if (scroll) r.el.scrollIntoView({ block: "nearest" });
  }

  function move(delta) {
    if (!rows.length) return;
    const cur = focusIndex();
    // No cursor yet: j lands on the first row, k on the last.
    if (cur < 0) return setFocus(delta > 0 ? 0 : rows.length - 1);
    setFocus(Math.min(rows.length - 1, Math.max(0, cur + delta)));
  }

  function openFocused() {
    const idx = focusIndex();
    if (idx < 0) return false;
    open(rows[idx].data, rows[idx].el);
    return true;
  }

  // Arrow keys preview as they go: move the cursor, then open what it landed on.
  function moveAndOpen(delta) {
    move(delta);
    openFocused();
  }

  // app.keys.js saying whether the list is the pane holding the keyboard. The
  // cursor ring goes quiet while the folders or the thread have it, so only one
  // of the three ever looks live.
  function setKeyFocus(state) {
    keyFocus = state;
    document.getElementById("message-list").classList.toggle("keys", state);
    // Taking the keyboard with nothing under the cursor leaves the pane looking
    // dead — start at the top. The rows may still be loading, in which case
    // render() does the same thing once they land.
    if (state && rows.length && focusIndex() < 0) setFocus(0, false);
  }

  // --- Paging ---
  // The footer is owned here rather than by the caller because it has to sit
  // *after* the rows, and render() owns the host's contents.
  function renderMore() {
    const host = document.getElementById("message-list");
    const old = host.querySelector(".list-more");
    if (old) old.remove();
    if (!moreFn || !rows.length) return;

    const btn = document.createElement("button");
    btn.className = "list-more";
    btn.textContent = "Load more";
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      btn.textContent = "Loading…";
      try {
        await moreFn();
      } finally {
        // A successful load replaces this button via setMore(); if the fetch
        // failed it is still here, and has to be clickable again.
        if (btn.isConnected) { btn.disabled = false; btn.textContent = "Load more"; }
      }
    });
    host.appendChild(btn);
  }

  // null hides the footer — that is how the caller says "no more pages".
  function setMore(fn) {
    moreFn = fn || null;
    renderMore();
  }

  // Rows for the next page, added without rebuilding the ones already on
  // screen: a re-render would drop the scroll position right after the click
  // that asked for more.
  function append(data, showAccount) {
    if (!data.length) return;
    const frag = document.createDocumentFragment();
    for (const r of data) {
      // The list is paged by offset, so a message arriving mid-session can
      // shift rows down and repeat one on the next page. Drop the duplicate.
      if (rows.some((x) => x.id === r.id)) continue;
      const el = row(r, showAccount);
      rows.push({ id: r.id, data: r, el });
      frag.appendChild(el);
    }
    document.getElementById("message-list").appendChild(frag);
    renderMore();   // keep the footer last
    changed();
  }

  // ageTint is the caller's call, not ours: age only means "still sitting in
  // your inbox unanswered", so it is asked for by the inbox views and left off
  // everywhere else (Sent, Archive, Flagged, search results), where an old
  // message is just an old message.
  function render(data, showAccount, ageTint = false) {
    tintOn = !!ageTint;
    // Remember where the cursor sat so a refresh (or an archive that drops the
    // focused row) leaves it on the same spot rather than snapping to the top.
    const prevIdx = focusIndex();
    const host = document.getElementById("message-list");
    host.innerHTML = "";
    rows = [];
    if (!data.length) {
      host.innerHTML = `<div class="list-empty">No messages</div>`;
      focusId = null;
      selected.clear();
      anchorId = null;
      changed();
      // The reading pane's placeholder reads off this count, so it has to be
      // told when the count changes under it.
      if (App.reader && !App.reader.isOpen()) App.reader.renderEmpty();
      return;
    }
    const frag = document.createDocumentFragment();
    for (const r of data) {
      const el = row(r, showAccount);
      rows.push({ id: r.id, data: r, el });
      frag.appendChild(el);
    }
    host.appendChild(frag);
    renderMore();

    // Forget ticks whose rows are gone — deleted, moved, or filtered out by a
    // refresh — so the count in the bar can never claim more than is on screen.
    const present = new Set(rows.map((x) => x.id));
    for (const id of [...selected]) if (!present.has(id)) selected.delete(id);
    if (anchorId !== null && !present.has(anchorId)) anchorId = null;
    changed();

    const stillThere = rows.findIndex((x) => x.id === focusId);
    if (stillThere >= 0) setFocus(stillThere, false);
    else if (prevIdx >= 0) setFocus(Math.min(prevIdx, rows.length - 1), false);
    // First rows of a folder the keyboard was handed to while it was still
    // loading — see setKeyFocus().
    else if (keyFocus) setFocus(0, false);
    else focusId = null;
    if (App.reader && !App.reader.isOpen()) App.reader.renderEmpty();
  }

  // An action just filed these rows away: take them out of the list now, ahead
  // of the round trip that makes it true — reloadList() reconciles afterwards,
  // and puts them back if the server said no. The cursor stays on the slot the
  // rows vacated (the same rule render() applies on a refresh), so
  // openFocused() lands on the next mail down. Returns how many rows went.
  function drop(pred) {
    const prevIdx = focusIndex();
    const going = rows.filter((r) => pred(r.data));
    if (!going.length) return 0;
    for (const r of going) { r.el.remove(); selected.delete(r.id); }
    rows = rows.filter((r) => !pred(r.data));
    if (going.some((r) => r.id === activeId)) activeId = null;
    if (anchorId !== null && !rows.some((r) => r.id === anchorId)) anchorId = null;
    changed();
    if (!rows.length) {
      document.getElementById("message-list").innerHTML = `<div class="list-empty">No messages</div>`;
      focusId = null;
      if (App.reader && !App.reader.isOpen()) App.reader.renderEmpty();
    } else if (focusId !== null && !rows.some((r) => r.id === focusId)) {
      setFocus(Math.min(Math.max(prevIdx, 0), rows.length - 1), false);
    }
    return going.length;
  }

  function reset() {
    activeId = null; focusId = null; rows = [];
    selected.clear(); anchorId = null;
    moreFn = null;
    changed();
  }

  return { render, append, setMore, reset, drop, move, moveAndOpen, openFocused, setFocus,
           setKeyFocus, ageDays, setAgeDays,
           selectAllLoaded, clearSelection, selection, markSeen,
           count: () => rows.length, hasFocus: () => focusIndex() >= 0,
           // Which row the reading pane is showing, so a swipe that files it
           // away can take the pane with it.
           activeId: () => activeId,
           selectedCount: () => selected.size };
})();
