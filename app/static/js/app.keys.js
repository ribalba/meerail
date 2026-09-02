/* meerail keyboard shortcuts.

   The SHORTCUTS table below is the single source of truth: it drives both the
   key handling and the cheat-sheet box in the sidebar, so the two cannot drift.

   The keyboard lives in one of three panes — folders → list → thread — which
   are the same three the narrow layout pages through (see app.mobile.js). It
   starts on the folders. Enter goes one pane deeper, Escape comes one back, and
   j/k/arrows always act on whichever pane holds it, so the same four keys walk
   the sidebar, the list, and the thread without any of them needing a chord.

   The arrows and j/k deliberately differ: ↑/↓ open each row as you land on it
   (preview-as-you-go), while j/k only move the cursor and leave opening to
   Enter/o. Opening a conversation fetches the thread and marks it read, so j/k
   is the way to skim a mailbox without burning through your unread state. */

App.keys = (function () {
  const $ = (s) => document.querySelector(s);
  const STORE_KEY = "meerail.shortcuts.collapsed";

  const PANES = ["folders", "list", "reader"];

  let pendingG = null;       // timer for the "g then …" chord
  let pane = "folders";      // which of PANES the arrows and j/k act on

  // The reader is only really a pane while a thread is up — or on its way, since
  // the keystroke that moves the keyboard in is the one that asks for it. If the
  // thread went away under us (mailbox switch, archive) the keyboard is handed
  // back here, so the arrows walk the list again instead of costing an Escape.
  function hasThread() { return App.reader.isOpen() || App.reader.isBusy(); }

  function current() {
    if (pane === "reader" && !hasThread()) setPane("list");
    return pane;
  }

  function setPane(next) {
    if (next === "reader" && !hasThread()) next = "list";
    pane = next;
    // Each pane draws its own cursor; telling all three keeps exactly one of
    // them looking live, whichever way the move was made.
    App.shell.setFolderKeyFocus(pane === "folders");
    App.list.setKeyFocus(pane === "list");
    App.reader.setKeyFocus(pane === "reader");   // the ↑↓ marker in the action bar
    syncMobile(next);
  }

  // Narrow layouts show one pane at a time, so moving the keyboard has to turn
  // the page as well — going back through history rather than pushing onto it,
  // so Escape and the Back button agree about where "back" is. Desktop shows
  // all three at once and has nothing to turn.
  function syncMobile(next) {
    if (!App.mobile || !App.mobile.narrow() || App.mobile.current() === next) return;
    if (PANES.indexOf(next) < PANES.indexOf(App.mobile.current())) App.mobile.back(next);
    else App.mobile.show(next);
  }

  // Enter: one pane deeper, but only if there is something there to go to —
  // an empty sidebar or a cursor on no row leaves the keyboard where it is.
  function enter() {
    if (current() === "folders") { if (App.shell.openFocusedFolder()) setPane("list"); return; }
    if (pane !== "list") return;
    if (!App.list.hasFocus()) App.list.move(1);   // no cursor yet — start at the top
    if (App.list.openFocused()) setPane("reader");
  }

  // Escape steps back one pane, in two halves called from different points in
  // onEscape(): dropping out of a thread comes before clearing the search box,
  // and dropping out of the list comes after it. Each answers false when it is
  // not the step to take, so onEscape() can carry on down its list.
  function leaveReader() {
    if (current() !== "reader") return false;
    setPane("list");
    return true;
  }

  function leaveList() {
    if (current() !== "list") return false;
    setPane("folders");     // the sidebar is the floor — Escape there does nothing
    return true;
  }

  // j/k never open anything, so they are also how you take the keyboard back
  // off the thread without giving up your place in the list.
  function move(delta) {
    if (current() === "folders") return App.shell.moveFolder(delta);
    if (pane === "reader") setPane("list");
    App.list.move(delta);
  }

  function arrow(delta) {
    if (current() === "folders") return App.shell.moveFolderAndOpen(delta);
    if (pane === "reader") return App.reader.scrollBy(delta, 0.15);
    App.list.moveAndOpen(delta);
  }

  const SHORTCUTS = [
    {
      group: "Navigate",
      items: [
        { keys: ["j"], show: "j", label: "Next folder / message",
          run: () => move(1) },
        { keys: ["k"], show: "k", label: "Previous folder / message",
          run: () => move(-1) },
        { keys: ["ArrowDown"], show: "↓", label: "Next + open / scroll",
          run: () => arrow(1) },
        { keys: ["ArrowUp"], show: "↑", label: "Previous + open / scroll",
          run: () => arrow(-1) },
        // The two labels are the same three panes read each way, and the key
        // beside them says which way. Anything longer than this is truncated by
        // the box, which is what makes them terse rather than a sentence.
        { keys: ["Enter", "o"], show: "↵ / o", label: "folder → list → thread",
          run: () => enter() },
        // Handled ahead of the table in handle(), like the modified keys below,
        // because Escape has to work while a modal or the composer has focus.
        { show: "Esc", label: "thread → list → folder" },
        { keys: ["PageDown"], show: "PgDn", label: "End of thread",
          run: () => App.reader.scrollEnd(1) },
        { keys: ["PageUp"], show: "PgUp", label: "Top of thread",
          run: () => App.reader.scrollEnd(-1) },
        // A whole message per press. PgUp/PgDn jump to the ends of a thread and
        // Space pages through it; in a conversation of thirty replies neither
        // is a way to get to the next message, which is what these two are for.
        { keys: ["n"], show: "n", label: "Next message in thread",
          run: (e) => App.reader.scrollMsg(1, e.repeat) },
        { keys: ["p"], show: "p", label: "Previous message in thread",
          run: (e) => App.reader.scrollMsg(-1, e.repeat) },
        // Display-only, and handled ahead of the table in handle() like the
        // other modified keys: the same jump for hands that reach for a paging
        // key. Alt rather than Ctrl because Chrome and Firefox keep
        // Ctrl+PageUp/PageDown for switching tabs and never hand it to a page.
        // "Alt" without the ⌥ the other modified rows carry, because the pair
        // of glyphs pushes the label past the width of the box — and Apple
        // prints "alt" on that key next to the symbol anyway.
        { show: "Alt PgDn/PgUp", label: "Jump one message" },
        { keys: [" "], show: "Space", label: "Scroll message",
          run: (e) => App.reader.scrollBy(e.shiftKey ? -1 : 1) },
        { chord: ["g", "i"], show: "g i", label: "Go to Inbox",
          run: () => App.shell.goto("inbox") },
        { chord: ["g", "a"], show: "g a", label: "Go to All Inboxes",
          run: () => App.shell.goto("unified") },
        { chord: ["g", "f"], show: "g f", label: "Go to Flagged",
          run: () => App.shell.goto("flagged") },
        // Reachable even while the row is hidden, which it is whenever the
        // outbox is empty: "did that actually go?" is a question worth being
        // able to ask at any moment, and the answer is the empty folder.
        { chord: ["g", "o"], show: "g o", label: "Go to Outbox",
          run: () => App.shell.goto("outbox") },
        // Same reasoning as the Outbox: the row is only in the sidebar while
        // something is waiting, and "what did I put off?" is a question worth
        // being able to ask when the answer is nothing.
        { chord: ["g", "r"], show: "g r", label: "Go to Reminders",
          run: () => App.shell.goto("reminders") },
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
        { keys: ["b"], show: "b", label: "Remind me later…",
          run: () => App.reader.action("remind") },
        // Backspace too: the Mac "delete" key reports Backspace, not Delete.
        // With rows ticked this deletes the selection rather than the open
        // thread — that is the whole point of having ticked them.
        //
        // Holding Shift is the permanent version, and only on the two Delete
        // keys: "#" is typed *with* Shift on most layouts, so reading shiftKey
        // alone would quietly turn the ordinary trash shortcut into one that
        // destroys mail. It also only applies where App.bulk offers it at all —
        // ticked rows, every one of them in an imported account — and falls
        // back to the plain trash everywhere else, which is what the unmodified
        // key would have done anyway.
        { keys: ["#", "Delete", "Backspace"], show: "# / Del", label: "Move to trash",
          run: (e) => {
            if (!App.bulk.isActive()) return App.reader.action("trash");
            const forever = e.shiftKey && (e.key === "Delete" || e.key === "Backspace");
            return forever && App.bulk.canPurge() ? App.bulk.purge() : App.bulk.trash();
          } },
        // Display-only, like the other modified keys: the row above is what
        // runs it. Listed because it is the one shortcut here that mail does
        // not come back from, and an undocumented destructive key is worse than
        // no key at all.
        { show: "⇧ Del", label: "Delete permanently (imported)" },
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
        // `z` rather than `u`, which is Mark unread, and rather than ⌘Z, which
        // belongs to whatever is being typed. It is also what every other mail
        // client binds undo to. Pressing it repeatedly walks back through the
        // Recent actions box, one operation per press.
        { keys: ["z"], show: "z", label: "Undo last action",
          run: () => App.undo.undoLast() },
        // Handled ahead of the table in handle(), like the other modified keys:
        // it has to work while the composer has the keyboard, since swapping
        // the draft you are typing in for another one is the point of it.
        { show: "⌥/Alt C", label: "Next minimized draft" },
        // Also display-only. Listed because the Cc/Bcc buttons are deliberately
        // not tab stops, which leaves this as the only way to those rows for
        // hands that never touch the mouse.
        { show: "⌥/Alt ⇧ C/B", label: "Cc / Bcc line" },
        // Same again for the "Also" row of suggested recipients: the chips are
        // out of the tab ring too, and ↓ only reaches them from a recipient
        // field. The composer's own footer lists this beside the keys that are
        // only its; this row is here so the sheet is not missing one.
        { show: "⌥/Alt ⇧ S", label: "Suggested recipients" },
        { keys: ["/"], show: "/", label: "Search", run: () => App.search.focusInput() },
        // Display-only, like the other modified keys — handled ahead of the
        // table in handle(), since this one has to work with the caret already
        // in the find box, where pressing it again means "type over that".
        { show: "⌘/Ctrl F", label: "Find in thread" },
        { show: "⌘/Ctrl ↵", label: "Send message" },
        // Handled ahead of the table in handle() — modified keys never reach it.
        { show: "⌘/Ctrl A", label: "Select all in list" },
        // Escape is listed under Navigate — it steps back a pane once there is
        // nothing left to close, and one row for it is enough.
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
    if (App.ai.offOpen()) return App.ai.closeOff();
    if (App.ai.searchOpen()) return App.ai.closeSearch();
    if (App.ai.threadOpen()) return App.ai.closeThread();
    if (App.ai.attachmentOpen()) return App.ai.closeAttachment();
    if (App.shell.folderOpen()) return App.shell.closeFolder();
    if (App.shell.settingsOpen()) return App.shell.closeSettings();
    if (App.status.isOpen()) return App.status.close();
    if (App.stats.isOpen()) return App.stats.close();
    if (App.cleanup.isOpen()) return App.cleanup.close();
    if (App.search.helpOpen()) return App.search.closeHelp();
    // A pending bulk selection is the most recent thing you set up, so it is
    // the first thing Escape should take back.
    if (App.bulk.isActive()) return App.bulk.clear();
    // A find still lit in the thread — or, on a phone, still standing open over
    // the verbs — is the most recent thing you set up in this pane, so Escape
    // takes that back before it gives up the pane itself.
    // (Escape with the caret still in the box never reaches here — app.reader
    // handles it there, so that press also puts the caret back on the thread.)
    if (App.reader.findActive()) return App.reader.closeFind();
    // Hand the arrows back to the list before Escape starts closing things:
    // leaving a thread you were reading is the smaller, more likely intent.
    if (leaveReader()) return;
    // A search is narrowing the list you are standing in, so clear it before
    // stepping out of the list altogether — otherwise backing up to the folders
    // strands the results behind you with no way to see them again.
    const input = $("#search-input");
    if (App.search.isActive() || document.activeElement === input) {
      App.search.clear(true);
      input.blur();
      return;
    }
    if (leaveList()) return;
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
    // Alt+Shift+C/B open the composer's Cc and Bcc rows and put the caret in
    // them; Alt+Shift+S puts it on the first suggested recipient. All three
    // stand for things outside the tab ring — Tab in the composer walks the
    // fields a message is written into and nothing else — so this is the
    // keyboard's way to them. Ahead of Alt+C below, which would otherwise
    // answer Alt+Shift+C with a different draft, and swallowed rather than
    // passed on when no composer is open: a shortcut for a window that is not
    // there does nothing.
    if (e.altKey && !mod && e.shiftKey
        && (e.code === "KeyC" || e.code === "KeyB" || e.code === "KeyS")) {
      if (!App.compose.isOpen()) return;
      e.preventDefault();
      if (e.code === "KeyS") App.compose.focusSuggestions();
      else App.compose.focusExtra(e.code === "KeyC" ? "cc" : "bcc");
      return;
    }
    // Alt+C brings the next minimized draft up, parking whatever is in the
    // composer to make room, so repeated presses walk all of them. Up here
    // rather than in the table because it has to work with the caret in a
    // draft — and matched on the physical key, since Option+C on a Mac reports
    // e.key as "ç" and never as "c".
    if (e.altKey && !mod && e.code === "KeyC") {
      e.preventDefault();
      App.compose.cycle();
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

    // Ctrl/Cmd+F finds inside the open conversation. Taken from the browser
    // because the browser's own find cannot see into the body iframes — which
    // is where most of an HTML mail is — so the built-in one reports nothing on
    // a thread the word is plainly in. Handed straight back when there is no
    // thread, or when the caret is in some other field: App.reader decides
    // that and says so by answering false, and an unclaimed Ctrl+F still opens
    // the browser's find bar.
    if (mod && !e.altKey && (e.key === "f" || e.key === "F")) {
      if (App.reader.focusFind()) e.preventDefault();
      return;
    }

    // Alt+PageDown/PageUp is n/p for people who page rather than type letters.
    // Up here because a modified key never reaches the table below.
    if (e.altKey && !mod && (e.key === "PageDown" || e.key === "PageUp")) {
      if (App.reader.scrollMsg(e.key === "PageDown" ? 1 : -1, e.repeat)) e.preventDefault();
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
    // The folders are where the keyboard starts, before there is a list or a
    // thread to hand it to.
    setPane("folders");
    document.addEventListener("keydown", handle);
  }

  // `focus` is for the mouse: a click that lands in a pane takes the keyboard
  // with it, so the two never end up pointing at different things.
  return { init, handle, focus: setPane, pane: () => pane };
})();
