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

    el.innerHTML = `
      <span class="bulk-count">${label}</span>
      ${escalate}
      <span class="bulk-spacer"></span>
      <button class="bulk-btn danger" type="button" data-act="trash" ${busy ? "disabled" : ""}>
        ${App.icon("trash", 14)} ${busy ? "Deleting…" : "Delete"}</button>
      <button class="bulk-btn" type="button" data-act="clear" ${busy ? "disabled" : ""}>Clear</button>`;
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
    // Only the folder-wide version asks. A ticked handful is visible on screen
    // and undoable by hand; "everything in this folder, including the pages you
    // never looked at" is neither.
    if (folderMode && !confirm(
      `Delete all ${plural(n, "conversation", "conversations")} in ${App.shell.currentTitle()}?` +
      `\n\nThis includes messages not currently on screen.`)) return;

    busy = true;
    render();
    try {
      if (folderMode) await trashFolder();
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
    });
    render();
  }

  return { init, sync, selectAllLoaded, clear, trash,
           isActive: () => folderMode || App.list.selectedCount() > 0 };
})();
