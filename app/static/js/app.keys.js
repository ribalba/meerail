/* meerail keyboard shortcuts.

   The SHORTCUTS table below is the single source of truth: it drives both the
   key handling and the cheat-sheet box in the sidebar, so the two cannot drift.

   The arrows and j/k deliberately differ: ↑/↓ open each row as you land on it
   (preview-as-you-go), while j/k only move the cursor and leave opening to
   Enter/o. Opening fetches the thread and marks it read, so j/k is the way to
   skim a mailbox without burning through your unread state.

   Enter/o also hands the arrows over to the reading pane, where they scroll the
   thread until Escape (or j/k) hands them back to the list. */

App.keys = (function () {
  const $ = (s) => document.querySelector(s);
  const STORE_KEY = "meerail.shortcuts.collapsed";

  let pendingG = null;       // timer for the "g then …" chord
  let inReader = false;      // do the arrows scroll the thread or walk the list?

  // The pane is only really "focused" while a thread is up. If the thread went
  // away under us (mailbox switch, archive) the flag is dropped here, so the
  // arrows go straight back to the list instead of costing an Escape first.
  function readerHasKeys() {
    if (inReader && !App.reader.isOpen()) setReader(false);
    return inReader;
  }

  function setReader(state) {
    inReader = state;
    App.reader.setKeyFocus(state);   // the ↑↓ marker in the thread's action bar
  }

  function openAndRead() { if (App.list.openFocused()) setReader(true); }

  function listMove(delta) { setReader(false); App.list.move(delta); }

  function arrow(delta) {
    if (readerHasKeys()) return App.reader.scrollBy(delta, 0.15);
    App.list.moveAndOpen(delta);
  }

  const SHORTCUTS = [
    {
      group: "Navigate",
      items: [
        { keys: ["j"], show: "j", label: "Next message",
          run: () => listMove(1) },
        { keys: ["k"], show: "k", label: "Previous message",
          run: () => listMove(-1) },
        { keys: ["ArrowDown"], show: "↓", label: "Next + open / scroll",
          run: () => arrow(1) },
        { keys: ["ArrowUp"], show: "↑", label: "Previous + open / scroll",
          run: () => arrow(-1) },
        { keys: ["Enter", "o"], show: "↵ / o", label: "Open + read thread",
          run: () => openAndRead() },
        { keys: ["PageDown"], show: "PgDn", label: "End of thread",
          run: () => App.reader.scrollEnd(1) },
        { keys: ["PageUp"], show: "PgUp", label: "Top of thread",
          run: () => App.reader.scrollEnd(-1) },
        { keys: [" "], show: "Space", label: "Scroll message",
          run: (e) => App.reader.scrollBy(e.shiftKey ? -1 : 1) },
        { chord: ["g", "i"], show: "g i", label: "Go to Inbox",
          run: () => App.shell.goto("inbox") },
        { chord: ["g", "a"], show: "g a", label: "Go to All Inboxes",
          run: () => App.shell.goto("unified") },
        { chord: ["g", "f"], show: "g f", label: "Go to Flagged",
          run: () => App.shell.goto("flagged") },
      ],
    },
    {
      group: "Message",
      items: [
        { keys: ["e"], show: "e", label: "Reply to sender",
          run: () => App.reader.action("reply") },
        { keys: ["r"], show: "r", label: "Reply all",
          run: () => App.reader.action("replyall") },
        { keys: ["f"], show: "f", label: "Forward",
          run: () => App.reader.action("forward") },
        { keys: ["a"], show: "a", label: "Archive",
          run: () => App.reader.action("archive") },
        { keys: ["v"], show: "v", label: "Move to folder…",
          run: () => App.reader.action("move") },
        // Backspace too: the Mac "delete" key reports Backspace, not Delete.
        // With rows ticked this deletes the selection rather than the open
        // thread — that is the whole point of having ticked them.
        { keys: ["#", "Delete", "Backspace"], show: "# / Del", label: "Move to trash",
          run: () => (App.bulk.isActive() ? App.bulk.trash() : App.reader.action("trash")) },
        { keys: ["s"], show: "s", label: "Toggle flag",
          run: () => App.reader.action("flag") },
        { keys: ["u"], show: "u", label: "Mark unread",
          run: () => App.reader.action("unread") },
      ],
    },
    {
      group: "General",
      items: [
        { keys: ["c"], show: "c", label: "Compose", run: () => App.compose.openNew() },
        { keys: ["/"], show: "/", label: "Search", run: () => App.search.focusInput() },
        { show: "⌘/Ctrl ↵", label: "Send message" },
        // Handled ahead of the table in handle() — modified keys never reach it.
        { show: "⌘/Ctrl A", label: "Select all in list" },
        { show: "Esc", label: "Close / clear" },
        { keys: ["?"], show: "?", label: "Toggle this box", run: () => toggleBox() },
      ],
    },
  ];

  // key -> item, built once from the table above
  const BY_KEY = {};
  const BY_CHORD = {};
  for (const g of SHORTCUTS) {
    for (const item of g.items) {
      for (const k of item.keys || []) BY_KEY[k] = item;
      if (item.chord) BY_CHORD[item.chord.join(" ")] = item;
    }
  }

  // The INPUT types that are plain controls rather than somewhere you type. A
  // checkbox is an INPUT too, and counting it as text entry meant that ticking
  // a row left the focus on the box and killed every single-key shortcut until
  // you clicked elsewhere. Listed as an exception rather than the other way
  // round so an input type nobody thought about still swallows shortcuts.
  const CONTROL_INPUT = new Set(["checkbox", "radio", "button", "submit", "reset"]);

  function isTyping(el) {
    if (!el) return false;
    const tag = el.tagName;
    if (tag === "INPUT") return !CONTROL_INPUT.has((el.type || "text").toLowerCase());
    return tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
  }

  function onEscape() {
    // Only the × discards a draft, so Escape minimizes instead of closing.
    if (App.compose.isOpen()) return App.compose.minimize();
    if (App.tasks.isOpen()) return App.tasks.close();
    if (App.shell.folderOpen()) return App.shell.closeFolder();
    if (App.shell.settingsOpen()) return App.shell.closeSettings();
    if (App.status.isOpen()) return App.status.close();
    if (App.stats.isOpen()) return App.stats.close();
    if (App.search.helpOpen()) return App.search.closeHelp();
    // A pending bulk selection is the most recent thing you set up, so it is
    // the first thing Escape should take back.
    if (App.bulk.isActive()) return App.bulk.clear();
    // Hand the arrows back to the list before Escape starts closing things:
    // leaving a thread you were reading is the smaller, more likely intent.
    if (readerHasKeys()) return setReader(false);
    const input = $("#search-input");
    if (App.search.isActive() || document.activeElement === input) {
      App.search.clear(true);
      input.blur();
      return;
    }
    if (isTyping(document.activeElement)) document.activeElement.blur();
  }

  function handle(e) {
    if (e.defaultPrevented) return;
    const mod = e.metaKey || e.ctrlKey;

    // These must work even while typing in the composer. Ctrl/Cmd+Enter takes
    // whichever button is the primary one — Send & Archive behind a thread,
    // plain Send otherwise — so the shortcut and the highlighted button always
    // do the same thing. Alt+Enter is the escape hatch: send and nothing else.
    if (mod && e.key === "Enter") {
      if (App.compose.isOpen()) { e.preventDefault(); App.compose.sendDefault(); }
      return;
    }
    if (e.altKey && !mod && e.key === "Enter") {
      if (App.compose.isOpen()) { e.preventDefault(); App.compose.sendNow(); }
      return;
    }
    if (e.key === "Escape") { e.preventDefault(); onEscape(); return; }

    // Ctrl/Cmd+A over the list ticks every conversation on it. Only out here:
    // in the composer or the search box the browser's select-all-text is what
    // the key is for, and taking it would be maddening.
    if (mod && !e.altKey && (e.key === "a" || e.key === "A")) {
      if (isTyping(e.target) || isTyping(document.activeElement)) return;
      if (!App.bulk.selectAllLoaded()) return;         // empty list — let the browser have it
      e.preventDefault();
      return;
    }

    if (mod || e.altKey) return;                       // leave browser shortcuts alone
    if (isTyping(e.target) || isTyping(document.activeElement)) return;

    // "g then …" chord
    if (pendingG) {
      clearTimeout(pendingG);
      pendingG = null;
      const item = BY_CHORD["g " + e.key];
      if (item) { e.preventDefault(); item.run(e); }
      return;
    }
    if (e.key === "g") {
      e.preventDefault();
      pendingG = setTimeout(() => { pendingG = null; }, 1200);
      return;
    }

    const item = BY_KEY[e.key];
    if (!item || !item.run) return;
    // A run() that answers false didn't handle the key — PageUp with no thread
    // open, say — so leave the default behaviour to the browser.
    if (item.run(e) !== false) e.preventDefault();
  }

  // --- Cheat-sheet box in the sidebar ---
  function collapsed() { return localStorage.getItem(STORE_KEY) === "1"; }

  function applyCollapsed(state) {
    const box = $("#shortcut-box");
    if (!box) return;
    box.classList.toggle("collapsed", state);
    const btn = box.querySelector(".sc-toggle");
    btn.setAttribute("aria-expanded", String(!state));
    btn.title = state ? "Show shortcuts" : "Minimize";
    // Minimize bar while open; an up-chevron to restore it once minimized.
    box.querySelector(".sc-glyph").innerHTML = App.icon(state ? "chevron" : "minimize", 14);
    localStorage.setItem(STORE_KEY, state ? "1" : "0");
  }

  function toggleBox() { applyCollapsed(!collapsed()); }

  function renderBox() {
    const box = $("#shortcut-box");
    if (!box) return;
    const body = SHORTCUTS.map((g) => `
      <div class="sc-group">${App.esc(g.group)}</div>
      ${g.items.map((i) => `<div class="sc-row">
        <kbd>${App.esc(i.show)}</kbd><span>${App.esc(i.label)}</span>
      </div>`).join("")}`).join("");

    box.innerHTML = `
      <button class="sc-toggle" type="button" aria-expanded="true">
        <span>Shortcuts</span>
        <span class="sc-glyph"></span>
      </button>
      <div class="sc-body">${body}</div>`;
    box.querySelector(".sc-toggle").addEventListener("click", toggleBox);
    applyCollapsed(collapsed());
  }

  function init() {
    renderBox();
    document.addEventListener("keydown", handle);
  }

  return { init, handle };
})();
