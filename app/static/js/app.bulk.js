/* meerail bulk actions: the bar above the list when rows are ticked.

   Two tiers, because the list only ever holds a page of a folder. Ctrl-A ticks
   the rows that are loaded and the bar says so; if the folder has more than
   that, it offers to escalate to the whole thing. The distinction matters —
   "select all" quietly meaning "select the first 80" is how people delete the
   wrong mail — so the count in the bar is always the count that would go. */

App.bulk = (function () {
  // Set by the escalation link: the action now means the selector, not the
  // ticked rows. Cleared whenever the selection empties (folder switch, a
  // finished delete), so it can never outlive the list it was agreed against.
  let folderMode = false;
  let busy = false;

  const $ = (s) => document.querySelector(s);
  const bar = () => $("#bulk-bar");

  function plural(n, one, many) { return `${n} ${n === 1 ? one : many}`; }

  // How many the buttons would act on right now — the number the bar shows.
  function scope() {
    const loaded = App.list.selectedCount();
    return folderMode ? App.shell.listTotal() : loaded;
  }

  // The escalation is only honest when every loaded row is ticked and the
  // server said there are more. Search has no selector to escalate to
  // (listSelector() returns null there), so it stays on tier one.
  function canEscalate() {
    const loaded = App.list.selectedCount();
    return !folderMode && loaded > 0 && loaded === App.list.count()
      && App.shell.listSelector() !== null && App.shell.listTotal() > loaded;
  }

  function render() {
    const el = bar();
    if (!el) return;
    // The bar is rebuilt from scratch, so the button any open menu is anchored
    // to is about to stop existing — leaving the menu floating over a list it
    // no longer belongs to, still offering to move a selection that may have
    // just been cleared.
    closeMenu();
    const n = scope();
    // Reveals every row's tick box for as long as selecting is going on — the
    // boxes are hover-only otherwise. See .list-selecting in mail.css.
    const list = $("#message-list");
    if (list) list.classList.toggle("list-selecting", n > 0);
    if (!n) { el.hidden = true; el.innerHTML = ""; return; }
    el.hidden = false;

    // Kept short: the list pane is narrow, and the folder name is already in
    // the header above. The escalated state names it anyway, because that is
    // the one where "how much is selected" stops being visible on screen.
    const label = folderMode
      ? `All ${n} in ${App.esc(App.shell.currentTitle())} selected`
      : `${n} selected`;
    const escalate = canEscalate()
      ? `<button class="bulk-link" type="button" data-act="all">` +
        `Select all ${App.shell.listTotal()}</button>`
      : "";

    // In Trash the button is a different action and says so. Everywhere else
    // Delete files mail in Trash, where it can be got back from; in Trash there
    // is nowhere further to file it, and the only thing left to mean is destroy
    // it on the server. That is worth a different word, not a quiet change of
    // meaning behind the same one — which is exactly what the old shared
    // endpoint did, including to flagged mail nobody had gone to Trash to
    // delete. See app/routers/actions.py::bulk_empty_trash.
    //
    // "Empty Trash" rather than "Delete forever", because the operation behind
    // it is the whole folder whatever happens to be ticked, and the button has
    // to be the first thing that says so — not the confirmation dialog.
    const verb = inTrash() ? "Empty Trash" : "Delete";

    // Move is offered whenever there is one account to move within — see
    // moveAccountId. It sits before Delete because it is the ordinary half of
    // what a selection is for, and because the destructive button should not be
    // the one under the cursor by default.
    const canMove = moveAccountId() !== null;
    const move = canMove
      ? `<button class="bulk-btn" type="button" data-act="move" ${busy ? "disabled" : ""}>
           ${App.icon("folder", 14)} ${busy ? "Moving…" : "Move to…"}</button>`
      : "";

    el.innerHTML = `
      <span class="bulk-count">${label}</span>
      ${escalate}
      <span class="bulk-spacer"></span>
      ${move}
      <button class="bulk-btn danger" type="button" data-act="trash" ${busy ? "disabled" : ""}>
        ${App.icon("trash", 14)} ${busy ? "Deleting…" : verb}</button>
      <button class="bulk-btn" type="button" data-act="clear" ${busy ? "disabled" : ""}>Clear</button>`;
  }

  // Is the folder on screen the one Delete would otherwise be moving mail into?
  function inTrash() { return App.shell.currentRole() === "trash"; }

  // --- Move ---

  // The one account this selection can be moved within, or null if there isn't
  // one. A move is an operation inside an account — there is no IMAP command
  // that carries a message to another server — so a selection spanning two of
  // them has no single destination to offer, and the button is simply not
  // there. The unified inbox makes such a selection with one Ctrl-A, which is
  // why this is asked on every render rather than assumed from the folder.
  function moveAccountId() {
    if (folderMode) {
      // The escalated action runs off the selector, and only a selector naming
      // one folder names one account with it. "All flagged" spans every
      // account, and moving that somewhere is not a thing to offer.
      const mailboxId = App.shell.currentMailboxId();
      if (!mailboxId) return null;
      const acc = App.shell.accounts().find((a) =>
        a.mailboxes.some((mb) => mb.id === mailboxId));
      return acc ? acc.id : null;
    }
    const ids = new Set(App.list.selection().map((r) => r.account_id));
    return ids.size === 1 ? [...ids][0] : null;
  }

  // Where this selection could go: that account's folders, minus the one it is
  // already in. Excluding the current folder is not tidiness — a move into the
  // folder the mail is already in is refused by the server, and offering it is
  // offering an error.
  function moveTargets(accountId) {
    const here = App.shell.currentMailboxId();
    return App.shell.mailboxesFor(accountId).filter((mb) => mb.id !== here);
  }

  let menu = null;

  function closeMenu() {
    if (!menu) return;
    document.removeEventListener("mousedown", menu.onOutside, true);
    document.removeEventListener("keydown", menu.onKey, true);
    document.removeEventListener("scroll", closeMenu, true);
    window.removeEventListener("resize", closeMenu);
    menu.el.remove();
    menu = null;
  }

  // Same popup as the reader's move menu, in the same classes, placed the same
  // way — this is the same choice being made from a different button, and it
  // should not look like a different control.
  function openMenu(anchor) {
    closeMenu();
    const accountId = moveAccountId();
    if (accountId === null) return;
    const folders = moveTargets(accountId);

    const el = document.createElement("div");
    el.className = "move-menu";
    el.innerHTML = folders.length
      ? folders.map((mb) => `<button class="move-item" data-mailbox="${mb.id}">
          <span class="mm-icon">${App.icon(App.roleIcon(mb.role), 15)}</span>
          <span class="mm-name">${App.esc(mb.path || mb.display_name)}</span></button>`).join("")
      : `<div class="move-empty">No other folders</div>`;
    el.addEventListener("click", (e) => {
      const item = e.target.closest("[data-mailbox]");
      if (!item) return;
      const mailboxId = Number(item.dataset.mailbox);
      const name = item.querySelector(".mm-name").textContent;
      closeMenu();
      move(mailboxId, name);
    });

    document.body.appendChild(el);
    const r = anchor.getBoundingClientRect();
    el.style.top = Math.min(r.bottom + 4, window.innerHeight - el.offsetHeight - 8) + "px";
    el.style.left = Math.max(8, Math.min(r.left, window.innerWidth - el.offsetWidth - 8)) + "px";

    menu = {
      el,
      onOutside: (e) => { if (!el.contains(e.target) && e.target !== anchor) closeMenu(); },
      onKey: (e) => { if (e.key === "Escape") { e.stopPropagation(); closeMenu(); } },
    };
    document.addEventListener("mousedown", menu.onOutside, true);
    document.addEventListener("keydown", menu.onKey, true);
    document.addEventListener("scroll", closeMenu, true);
    window.addEventListener("resize", closeMenu);
  }

  async function moveSelected(mailboxId) {
    const items = App.list.selection().map((r) => ({
      account_id: r.account_id, thread_id: r.thread_id || null,
      message_id: r.thread_id ? null : r.id,
    }));
    if (!items.length) return 0;
    const res = await App.api.bulkMove(items, mailboxId);
    return res.moved || 0;
  }

  // Chunked and looped exactly like trashFolder(), for the same reason: the
  // server files a chunk per call and says whether anything is left, so a
  // folder of forty thousand is a loop rather than one request that times out.
  async function moveFolder(mailboxId) {
    const selector = App.shell.listSelector();
    if (!selector) return 0;
    let done = false;
    let moved = 0;
    while (!done) {
      const res = await App.api.bulkMoveAll(selector, mailboxId);
      moved += res.moved || 0;
      done = res.done;
      if (res.moved === 0) break;   // nothing shifted — stop rather than spin
    }
    return moved;
  }

  async function move(mailboxId, name) {
    if (busy) return;
    const n = scope();
    if (!n) return;
    // Only the folder-wide version asks, on the same rule as Delete: a ticked
    // handful is visible on screen, and "everything in this folder, including
    // the pages you never looked at" is not.
    if (folderMode && !confirm(
      `Move all ${plural(n, "conversation", "conversations")} in ` +
      `${App.shell.currentTitle()} to ${name}?` +
      `\n\nThis includes messages not currently on screen.`)) return;

    busy = true;
    render();
    try {
      if (folderMode) await moveFolder(mailboxId);
      else await moveSelected(mailboxId);
      folderMode = false;
      App.list.clearSelection();
    } catch (e) {
      alert("Could not move: " + e.message);
    } finally {
      busy = false;
      render();
      await App.shell.reloadList();
    }
  }

  // Called by App.list whenever the ticked set changes or the list re-renders.
  function sync() {
    if (!App.list.selectedCount()) folderMode = false;
    render();
  }

  function selectAllLoaded() {
    if (!App.list.count()) return false;
    App.list.selectAllLoaded();     // sync() runs from inside the list
    return true;
  }

  function clear() {
    folderMode = false;
    App.list.clearSelection();
  }

  function escalate() {
    folderMode = true;
    render();
  }

  // Trash, emptied from the folder itself. Chunked like trashFolder(), for the
  // same reason, and always confirmed however few rows are ticked: this is the
  // one action in meerail that mail does not come back from.
  async function emptyTrash() {
    const mailboxId = App.shell.currentMailboxId();
    if (!mailboxId) return 0;
    let done = false;
    let deleted = 0;
    while (!done) {
      const res = await App.api.emptyTrash(mailboxId);
      deleted += res.deleted || 0;
      done = res.done;
      if (res.deleted === 0) break;   // nothing shifted — stop rather than spin
    }
    return deleted;
  }

  async function trashSelected() {
    const items = App.list.selection().map((r) => ({
      account_id: r.account_id, thread_id: r.thread_id || null,
      message_id: r.thread_id ? null : r.id,
    }));
    if (!items.length) return;
    await App.api.bulkTrash(items);
  }

  // The server deletes a chunk per call and reports whether anything is left,
  // so a big folder is a loop rather than one request that times out. The bar
  // is re-rendered each pass to keep the count moving.
  async function trashFolder() {
    const selector = App.shell.listSelector();
    if (!selector) return;
    let done = false;
    let moved = 0;
    while (!done) {
      const res = await App.api.bulkTrashAll(selector);
      moved += res.moved || 0;
      done = res.done;
      if (res.moved === 0) break;   // nothing shifted — stop rather than spin
    }
    return moved;
  }

  async function trash() {
    if (busy) return;
    const n = scope();
    if (!n) return;
    // Emptying the Trash always asks, and asks in the words of what it does:
    // this is a deletion from the mail server that nothing can undo, so the
    // question can never be the same one as "file these away".
    if (inTrash()) {
      if (!confirm(
        `Permanently delete everything in ${App.shell.currentTitle()}?` +
        `\n\nThis empties the whole folder on the mail server, including messages ` +
        `not currently on screen. It cannot be undone.`)) return;
    } else if (folderMode && !confirm(
      // Only the folder-wide version asks. A ticked handful is visible on screen
      // and undoable by hand; "everything in this folder, including the pages you
      // never looked at" is neither.
      `Delete all ${plural(n, "conversation", "conversations")} in ${App.shell.currentTitle()}?` +
      `\n\nThis includes messages not currently on screen.`)) return;

    busy = true;
    render();
    try {
      if (inTrash()) await emptyTrash();
      else if (folderMode) await trashFolder();
      else await trashSelected();
      folderMode = false;
      App.list.clearSelection();
    } catch (e) {
      alert("Could not delete: " + e.message);
    } finally {
      busy = false;
      render();
      await App.shell.reloadList();
    }
  }

  function init() {
    const el = bar();
    if (!el) return;
    el.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-act]");
      if (!btn) return;
      if (btn.dataset.act === "clear") clear();
      else if (btn.dataset.act === "all") escalate();
      else if (btn.dataset.act === "trash") trash();
      else if (btn.dataset.act === "move") openMenu(btn);
    });
    render();
  }

  return { init, sync, selectAllLoaded, clear, trash,
           isActive: () => folderMode || App.list.selectedCount() > 0 };
})();
