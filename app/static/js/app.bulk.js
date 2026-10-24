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
    // "Empty Trash" rather than "Delete forever" for the escalated version,
    // because the operation behind it is the whole folder and the button has to
    // be the first thing that says so — not the confirmation dialog. A ticked
    // handful is the other thing, and it took a while to admit it: the button
    // said Empty Trash over "3 selected" and there was no way at all to remove
    // three messages from a Trash holding five hundred.
    const verb = !inTrash() ? "Delete" : folderMode ? "Empty Trash" : "Delete forever";

    // Move is offered whenever there is an account to move within, one that
    // every ticked row has mail in. See moveAccountIds. It sits before Delete
    // because it is the ordinary half of what a selection is for, and because
    // the destructive button should not be the one under the cursor by default.
    const canMove = moveAccountIds().length > 0;
    const move = canMove
      ? `<button class="bulk-btn" type="button" data-act="move" ${busy ? "disabled" : ""}>
           ${App.icon("folder", 14)} ${busy ? "Moving…" : "Move to…"}</button>`
      : "";

    // The second destructive button, and it only exists where the first one is
    // not the end of the story. Delete files into Trash; on an imported account
    // Trash is a folder in this database and nothing else, so "I do not want a
    // Trash, I want this gone" needs a button that means it. Not offered in
    // Trash itself, where Empty Trash is already that button and two of them
    // would be one too many. See app/routers/actions.py::bulk_purge.
    const purgeBtn = importedOnly() && !inTrash()
      ? `<button class="bulk-btn danger" type="button" data-act="purge" ${busy ? "disabled" : ""}>
           ${App.icon("trash", 14)} ${busy ? "Deleting…" : "Delete permanently"}</button>`
      : "";

    // Park in an inbox list, Back to Inbox in Parked, and nothing anywhere
    // else. A selection in Archive or a search can hold no inbox mail at all,
    // and a button that mostly answers with a refusal is not offered.
    const way = App.shell.parkWay();
    const parkBtn = way
      ? `<button class="bulk-btn" type="button" data-act="park" ${busy ? "disabled" : ""}>
           ${App.icon(way === "park" ? "parked" : "inbox", 14)}
           ${way === "park" ? "Park" : "Back to Inbox"}</button>`
      : "";

    el.innerHTML = `
      <span class="bulk-count">${label}</span>
      ${escalate}
      <span class="bulk-spacer"></span>
      ${parkBtn}
      ${move}
      <button class="bulk-btn danger" type="button" data-act="trash" ${busy ? "disabled" : ""}>
        ${App.icon("trash", 14)} ${busy ? "Deleting…" : verb}</button>
      ${purgeBtn}
      <button class="bulk-btn" type="button" data-act="clear" ${busy ? "disabled" : ""}>Clear</button>`;
  }

  // Is the folder on screen the one Delete would otherwise be moving mail into?
  function inTrash() { return App.shell.currentRole() === "trash"; }

  // Is everything this bar would act on mail that came off disk — an account
  // with no agent and no server (Account.local)? Asked per render rather than
  // read off the folder, for the same reason moveAccountIds() is: one Ctrl-A in
  // the unified inbox makes a selection spanning every account, and a permanent
  // delete offered on the strength of one of them being imported would be
  // aimed at all of them. Every account has to be one, or the button is simply
  // not there. Empty means false — nothing selected is not "all imported".
  //
  // Every account a row stands for counts, not only the one it was built from.
  // A purge reaches each of them (see selectionItems), so a row folded from an
  // imported copy and a copy on a mail server is not "all imported".
  function importedOnly() {
    // The escalated action runs off the selector, so the question is about the
    // one folder it names — see moveAccountIds, which resolves it the same way
    // and for the same reason.
    if (folderMode) return importedFolder();
    const accounts = App.shell.accounts();
    const imported = (id) => {
      const acc = accounts.find((a) => a.id === id);
      return !!(acc && acc.local);
    };
    const ids = [...new Set(App.list.selection()
      .flatMap((r) => App.members(r).map((m) => m.account_id)))];
    return ids.length > 0 && ids.every(imported);
  }

  // What the ticked rows mean to a bulk route: one item per account each row
  // stands for. A row in All Inboxes can be the same conversation in two
  // accounts (see App.members), and deleting, parking or purging it has to
  // reach both, or the row comes straight back on the half that was left. A
  // member with a thread is its whole conversation in that account; one
  // without is that message alone, as a row of its own always was.
  function memberItem(m) {
    return { account_id: m.account_id, thread_id: m.thread_id || null,
             message_id: m.thread_id ? null : m.id };
  }

  function selectionItems() {
    return App.list.selection().flatMap((r) => App.members(r).map(memberItem));
  }

  // Is the folder on screen an imported one? Empty Trash acts on the folder
  // whatever happens to be ticked in it, so that is the account its wording has
  // to be about — not the accounts the ticked rows belong to.
  function importedFolder() {
    const mailboxId = App.shell.currentMailboxId();
    if (!mailboxId) return false;
    const acc = App.shell.accounts()
      .find((a) => a.mailboxes.some((mb) => mb.id === mailboxId));
    return !!(acc && acc.local);
  }

  // --- Move ---

  // The accounts this selection can be moved within, in sidebar order, or an
  // empty list if there is none. A move is an operation inside an account —
  // there is no IMAP command that carries a message to another server — so a
  // selection spanning two of them has no single destination to offer, and
  // the button is simply not there. The unified inbox makes such a selection
  // with one Ctrl-A, which is why this is asked on every render rather than
  // assumed from the folder.
  //
  // A row that stands for the same conversation in two accounts (see
  // App.members) can go to a folder in either, so the answer is the accounts
  // every ticked row has mail in. Ticked rows that are all such pairs leave
  // two accounts to choose from, and the menu offers the folders of both. Only
  // the mail in the chosen folder's account moves (see moveSelected).
  function moveAccountIds() {
    if (folderMode) {
      // The escalated action runs off the selector, and only a selector naming
      // one folder names one account with it. "All flagged" spans every
      // account, and moving that somewhere is not a thing to offer.
      const mailboxId = App.shell.currentMailboxId();
      if (!mailboxId) return [];
      const acc = App.shell.accounts().find((a) =>
        a.mailboxes.some((mb) => mb.id === mailboxId));
      return acc ? [acc.id] : [];
    }
    const rows = App.list.selection();
    if (!rows.length) return [];
    let common = null;
    for (const r of rows) {
      const ids = new Set(App.members(r).map((m) => m.account_id));
      common = common === null ? ids : new Set([...common].filter((id) => ids.has(id)));
      if (!common.size) return [];
    }
    return App.shell.accounts().map((a) => a.id).filter((id) => common.has(id));
  }

  // Where this selection could go: that account's folders, minus the one it is
  // already in. Excluding the current folder is not tidiness — a move into the
  // folder the mail is already in is refused by the server, and offering it is
  // offering an error.
  //
  // In Parked the inbox is left out as well. Parked mail is already filed in
  // the inbox, so a move there has nothing to move, and Back to Inbox beside
  // this button is the verb that does what it would have meant.
  function moveTargets(accountId) {
    const here = App.shell.currentMailboxId();
    const parked = App.shell.parkWay() === "unpark";
    return App.shell.mailboxesFor(accountId)
      .filter((mb) => mb.id !== here && !(parked && mb.role === "inbox"));
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
    const accountIds = moveAccountIds();
    if (!accountIds.length) return;
    const folders = accountIds.flatMap((accountId) =>
      moveTargets(accountId).map((mb) => ({ mb, accountId })));

    const el = document.createElement("div");
    el.className = "move-menu";
    el.innerHTML = folders.length
      ? App.moveMenuItems(folders, accountIds.length > 1)
      : `<div class="move-empty">No other folders</div>`;
    el.addEventListener("click", (e) => {
      const item = e.target.closest("[data-mailbox]");
      if (!item) return;
      const mailboxId = Number(item.dataset.mailbox);
      const accountId = Number(item.dataset.account);
      const name = item.querySelector(".mm-name").textContent;
      closeMenu();
      move(mailboxId, name, accountId);
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

  // Only the members in the folder's own account go: the server refuses a
  // selection that is not all in the target's account, and there is nothing
  // it could do with the copy in the other one. That copy stays where it is,
  // and the row stays listed on it.
  async function moveSelected(mailboxId, accountId) {
    const items = App.list.selection()
      .flatMap((r) => App.members(r).filter((m) => m.account_id === accountId))
      .map(memberItem);
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

  async function move(mailboxId, name, accountId) {
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
      else await moveSelected(mailboxId, accountId);
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

  // --- Park ---

  // The ticked rows, in one request. Conversations, like every bulk verb, in
  // every account each row stands for (see selectionItems).
  async function parkSelected(parked) {
    const items = selectionItems();
    if (!items.length) return 0;
    const res = await App.api.bulkPark(items, parked);
    return res.moved || 0;
  }

  // Everything the list selector matches, a chunk per call, looped exactly
  // like moveFolder(). Clearing a backlog out of the inbox is what this is for.
  async function parkFolder(parked) {
    const selector = App.shell.listSelector();
    if (!selector) return 0;
    let done = false;
    let moved = 0;
    while (!done) {
      const res = await App.api.bulkParkAll(selector, parked);
      moved += res.moved || 0;
      done = res.done;
      if (res.moved === 0) break;   // nothing shifted, so stop rather than spin
    }
    return moved;
  }

  // The button, and the p key while rows are ticked. Answers false, at once
  // rather than through a promise, where the bar offers no Park, so the key is
  // left to the browser rather than spent.
  function park() {
    const way = App.shell.parkWay();
    if (busy || !way || !scope()) return false;
    runPark(way === "park");
    return true;
  }

  async function runPark(parked) {
    // Only the folder-wide version asks, on the same rule as Move. A ticked
    // handful is on screen, and the pages nobody has looked at are not.
    if (folderMode && !confirm(
      `${parked ? "Park" : "Put back"} all ${plural(scope(), "conversation", "conversations")} ` +
      `in ${App.shell.currentTitle()}${parked ? "" : " into the inbox"}?` +
      `\n\nThis includes messages not currently on screen.`)) return;

    busy = true;
    render();
    try {
      if (folderMode) await parkFolder(parked);
      else await parkSelected(parked);
      folderMode = false;
      App.list.clearSelection();
    } catch (e) {
      alert(`Could not ${parked ? "park" : "put back"}: ` + e.message);
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

  // What emptying the whole folder has to be confirmed against. Two sentences
  // for the same operation, because it is two different facts. On an account
  // with a server this is an expunge and the folder named is the one on the
  // server; on an imported account there is no server and the rows here are the
  // mail. Saying "on the mail server" to someone emptying an imported Trash is
  // a promise about a machine that does not exist.
  function confirmEmpty() {
    const what = importedFolder()
      ? `\n\nThis mail was imported, so meerail holds the only copy of it. The whole ` +
        `folder is deleted, including messages not currently on screen.`
      : `\n\nThis empties the whole folder on the mail server, including messages ` +
        `not currently on screen.`;
    // Named with its size, because "everything in this folder" gave no sign of
    // what that was: a Trash nobody has emptied since the account was made is
    // years of mail nobody has looked at, and the first time its owner learns
    // the number should not be from the log afterwards. The count is the list's
    // own — the same one the header shows — so the question is asked about
    // exactly what the folder says it holds.
    const n = App.shell.listTotal();
    const all = n > 1 ? `all ${n} conversations`
              : n === 1 ? "the conversation" : "everything";
    return confirm(`Permanently delete ${all} in ${App.shell.currentTitle()}?${what}` +
                   `\n\nIt cannot be undone.`);
  }

  // Empty Trash with nothing ticked at all — the button in the folder header.
  // The bulk bar's version only exists once rows are selected, which left the
  // one operation that empties a Trash reachable only by first selecting mail
  // in it and then pressing a button labelled as if it were about that
  // selection. A folder you are standing in is a thing you can empty.
  async function emptyOpenFolder() {
    if (busy || !inTrash()) return;
    if (!confirmEmpty()) return;
    busy = true;
    render();
    try {
      await emptyTrash();
      clear();
    } catch (e) {
      alert("Could not delete: " + e.message);
    } finally {
      busy = false;
      render();
      await App.shell.reloadList();
    }
  }

  // Trash, emptied from the folder itself. Chunked like trashFolder(), for the
  // same reason, and always confirmed however few rows are ticked: this is the
  // one action in meerail that mail does not come back from.
  async function emptyTrash() {
    const mailboxId = App.shell.currentMailboxId();
    if (!mailboxId) return;
    let done = false;
    let deleted = 0;
    let queued = 0;
    while (!done) {
      const res = await App.api.emptyTrash(mailboxId);
      deleted += res.deleted || 0;
      queued = res.queued || 0;
      done = res.done;
      if (res.deleted === 0) break;   // nothing shifted — stop rather than spin
    }
    // Mail the empty left behind. It is here because meerail has told the server
    // to move it to Trash and has not been told it happened, so as far as any
    // mail server knows it is still filed where it came from — and "empty the
    // Trash" is not permission to delete it there. bulk_empty_trash leaves it
    // alone and reports it, and this is where that gets said.
    //
    // Whatever was deleted alongside it, not only when nothing was: on a Trash
    // that emptied all but the last handful, the rows that stay put are the
    // whole of what the user sees, and they are exactly the ones the button
    // looks like it failed on.
    if (queued > 0) {
      alert(`${deleted ? `Deleted ${plural(deleted, "message", "messages")}. ` : ""}` +
        `${plural(queued, "message is", "messages are")} still on the way to ` +
        `Trash on the mail server — meerail has asked for the move and is ` +
        `waiting to be told it happened, and does not delete mail the server ` +
        `still has filed somewhere else. Those stay in this folder for now.` +
        `\n\nEmpty the Trash again once the sync has caught up.`);
    }
  }

  async function trashSelected() {
    const items = selectionItems();
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

  // --- Delete permanently ---

  // The imported-account counterpart of trashSelected/trashFolder. The ticked
  // rows go in one request — every placement of every message in them, so a
  // conversation that was imported into two folders does not leave half of
  // itself behind — and the folder-wide version chunks like every other
  // whole-folder operation here. A row that stands for several accounts sends
  // one item per account, like Delete (see selectionItems), and importedOnly
  // has already made sure every one of them is imported before the button
  // that leads here was drawn outside Trash.
  async function purgeSelected() {
    const items = selectionItems();
    if (!items.length) return 0;
    const res = await App.api.bulkPurge(items);
    return res.deleted || 0;
  }

  // Looped on `removed` rather than on `deleted`: those two differ whenever an
  // mbox was imported into two folders, where taking a placement out of one of
  // them destroys nothing, and stopping on "deleted === 0" would leave the rest
  // of the folder standing. See app/routers/actions.py::bulk_purge_all.
  async function purgeFolder() {
    const selector = App.shell.listSelector();
    if (!selector) return 0;
    let done = false;
    let deleted = 0;
    while (!done) {
      const res = await App.api.bulkPurgeAll(selector);
      deleted += res.deleted || 0;
      done = res.done;
      if (!res.removed) break;      // nothing shifted — stop rather than spin
    }
    return deleted;
  }

  // Always asks, however few rows are ticked, and asks in the words of what it
  // does. Every other confirmation in here can lean on the mail still being
  // somewhere; this one cannot, and the sentence that says so is the only
  // warning there is going to be.
  async function purge() {
    if (busy) return;
    const n = scope();
    if (!n) return;
    const where = folderMode
      ? `\n\nThis is everything in ${App.shell.currentTitle()}, including messages ` +
        `not currently on screen.`
      : "";
    // Two sentences for the same button, because it is two different facts.
    // Outside Trash this is only ever offered on imported mail, where the rows
    // are the mail; inside it, it is also how one conversation is taken out of
    // a Trash a server is holding, and there the destruction happens over
    // there. Promising someone their imported mbox is being deleted "from the
    // mail server" is a promise about a machine that does not exist.
    const what = importedOnly()
      ? `This mail was imported, so meerail holds the only copy of it. It is ` +
        `deleted here and now — not moved to Trash — and nothing can undo it.`
      : `This deletes it from the mail server for good, and nothing can undo it.`;
    if (!confirm(
      `Permanently delete ${plural(n, "conversation", "conversations")}?${where}` +
      `\n\n${what}`)) return;

    busy = true;
    render();
    try {
      if (folderMode) await purgeFolder();
      else await purgeSelected();
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

  async function trash() {
    if (busy) return;
    const n = scope();
    if (!n) return;
    // A ticked handful in Trash is the ticked handful, not the folder. Handed
    // straight to the permanent delete, which is the same operation the reader's
    // Delete button runs in there and asks the same question before it.
    if (inTrash() && !folderMode) return purge();
    // Emptying the Trash always asks, and asks in the words of what it does:
    // this is a deletion from the mail server that nothing can undo, so the
    // question can never be the same one as "file these away".
    if (inTrash()) {
      if (!confirmEmpty()) return;
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
      else if (btn.dataset.act === "purge") purge();
      else if (btn.dataset.act === "park") park();
      else if (btn.dataset.act === "move") openMenu(btn);
    });
    render();
  }

  return { init, sync, selectAllLoaded, clear, trash, purge, park, emptyOpenFolder,
           // Whether Shift+Delete has anything to do — the keyboard's version of
           // the button only existing on imported mail. See app.keys.js.
           canPurge: () => importedOnly() && !inTrash(),
           isActive: () => folderMode || App.list.selectedCount() > 0 };
})();
