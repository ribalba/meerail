/* meerail search: the Apple-Mail search bar — regex/keyword, scope, time window. */

App.search = (function () {
  let active = false;
  let timer = null;
  let requestSeq = 0;
  // The fetch belonging to `requestSeq`. Superseding a search has always
  // dropped its answer; this also stops asking for it, so a query the user has
  // already typed past is not still being computed when the one they meant
  // arrives.
  let inflight = null;
  const $ = (s) => document.querySelector(s);

  // The time window sticks across reloads. Someone who searches their mail a
  // year at a time means it every time — unlike the scope, which clear() drops
  // because a folder narrower than the box suggests is invisible once the query
  // is gone. The window is written on the control itself, so it stays readable.
  const YEARS_KEY = "meerail.search.years";

  // --- Paging ---
  // Results are paged exactly as the folder list is, through the footer button
  // App.list owns. Without it a query that matched four hundred conversations
  // showed the newest sixty and the rest were unreachable — the list does not
  // scroll past what was fetched, so "60 of 400" looked identical to a search
  // that had simply missed the mail you were after.
  const PAGE = 60;
  const MAX_ROWS = 1000;     // the server's ceiling on ?limit, and so on ours

  let total = 0;             // conversations matching the query, not just the page
  let capped = false;        // ...and whether that number is a floor — see total_capped
  // The params the rows on screen came from. loadMore() reads them from here
  // rather than off the controls, so a page still in flight when the box is
  // edited cannot come back holding rows for a different query.
  let pageParams = null;

  function els() {
    return {
      input: $("#search-input"), clear: $("#search-clear"), controls: $("#search-controls"),
      rx: $("#rx-toggle"), scope: $("#scope-select"), years: $("#years-select"),
      status: $("#search-status"), help: $("#search-help-btn"), helpModal: $("#search-help-modal"),
    };
  }

  // Mirrors app/searchquery.py. The server parses the filters itself — this
  // copy exists so the thread view doesn't highlight `:unread` as if it were a
  // word someone searched for.
  const FILTER_RE = /(?:(?<=\s)|^):(?:unread|read|has-attachments?|no-trash)(?:\s+|$)/gi;
  const ADDR_RE = /(?:(?<=\s)|^):(?:from|to|similar)(?:\s+|=)(?:"[^"]*"|[^\s:]\S*)(?:\s+|$)/gi;
  const PARTIAL_RE = /(?:(?<=\s)|^):(?:from|to|similar)=?\s*$/i;

  function textOf(q) {
    return q.replace(ADDR_RE, "").replace(FILTER_RE, "").replace(PARTIAL_RE, "").trim();
  }

  // The scope menu names every folder rather than offering "This Mailbox": the
  // smart rows (All Inboxes, Flagged) are not one mailbox, so "this" quietly
  // meant "all" whenever the search started from one of them — a narrower
  // search than the user asked for was indistinguishable from a wider one.
  // Naming the folder makes the scope of the results readable off the control.
  function syncScope() {
    const scope = els().scope;
    if (!scope) return;
    const accounts = App.shell.accounts();
    const keep = scope.value;
    let html = `<option value="all">All Mailboxes</option>`;
    const multi = accounts.length > 1;
    for (const acc of accounts) {
      const opts = acc.mailboxes
        // Paths, for the same reason the move menu uses them: a list holding
        // two folders called "2024" says nothing about which is which.
        .map((mb) => `<option value="${mb.id}">${App.esc(mb.path || mb.display_name)}</option>`)
        .join("");
      if (!opts) continue;
      html += multi
        ? `<optgroup label="${App.esc(acc.label || acc.email)}">${opts}</optgroup>`
        : opts;
    }
    scope.innerHTML = html;
    // A folder that has since been deleted (or renamed away) can't stay
    // selected — falling back to "all" searches wider than asked, which is the
    // failure that shows results rather than none.
    scope.value = keep;
    if (!scope.value) scope.value = "all";
  }

  // refresh: re-fetch the rows for the query already in the box, rather than
  // starting a search someone just typed. The difference is the reader and the
  // cursor — a refresh leaves both alone, because the thread on screen is not
  // necessarily the thread that changed, and closing it would be a second,
  // unasked-for effect of pressing Delete.
  async function run(request, refresh = false) {
    clearTimeout(timer);
    timer = null;
    if (typeof request !== "number") request = ++requestSeq;
    if (request !== requestSeq) return false;
    const e = els();
    const q = e.input.value.trim();
    e.clear.hidden = q === "";
    if (!q) { clear(false); return false; }

    active = true;
    e.controls.hidden = false;
    e.status.classList.remove("error");
    e.status.textContent = "Searching…";

    const params = { q, mode: e.rx.checked ? "regex" : "keyword", years: e.years.value };
    if (e.scope.value !== "all") params.mailbox_id = Number(e.scope.value);
    // A refresh re-fetches every page that is on screen rather than the first
    // one: collapsing a search the reader has paged through, because a message
    // was archived out of it, loses their place. Same rule, and the same
    // arithmetic, as App.shell.loadList(keepPaged).
    params.limit = refresh ? Math.min(MAX_ROWS, Math.max(PAGE, App.list.count())) : PAGE;

    if (inflight) inflight.abort();
    const ctrl = new AbortController();
    inflight = ctrl;

    try {
      const data = await App.api.search(params, ctrl.signal);
      if (request !== requestSeq) return false;
      if (!refresh) {
        App.list.reset();
        App.reader.clear();
      }
      pageParams = params;
      total = data.total || 0;
      capped = !!data.total_capped;
      // render() prunes ticks whose rows are gone and keeps the keyboard cursor
      // on the slot a deleted row vacated, so a refresh needs nothing further.
      App.list.render(data.rows, true);
      // render() draws the footer from whatever hook was set last, so this has
      // to come after it — and it has to come at all, because App.list.reset()
      // above cleared the hook the previous search left.
      App.list.setMore(hasMore() ? loadMore : null);
      paintStatus();
      $("#list-title").textContent = e.rx.checked ? "Regex search" : "Search";
      // The folder's own verb goes with its name: what is on screen is no
      // longer the folder. See App.shell.paintPaneAction.
      App.shell.paintPaneAction();
      return true;
    } catch (ex) {
      if (request !== requestSeq) return false;
      // Our own abort, not a failure: the search that replaced this one owns
      // the status line now and is about to write to it.
      if (ex.name === "AbortError") return false;
      e.status.classList.add("error");
      e.status.textContent = ex.message || "Search failed";
      return false;
    } finally {
      if (inflight === ctrl) inflight = null;
    }
  }

  // A "+" where the server stopped counting. It reads the newest matches and
  // stops once it has enough conversations to fill the page, so the count is
  // exact for every search but the ones that match a large slice of the
  // mailbox — and for those the honest answer is a floor, not a number the user
  // waited an extra second for. Paging refines it: a later page scans wider, so
  // this is repainted whenever a page lands rather than written once.
  function paintStatus() {
    const more = capped ? "+" : "";
    els().status.textContent = total === 0 ? "No results"
      : `${total}${more} result${total === 1 && !more ? "" : "s"}`;
  }

  // `total` is a floor while the count is capped, so a capped search that has
  // shown every conversation it counted may still have more behind it — hence
  // the second clause. The button offering one page that comes back empty is
  // the cost of that, and it is the page itself that then takes the button
  // away; claiming there is nothing more when the server has said "N+" would be
  // the same lie the missing button was.
  function hasMore() {
    if (App.list.count() >= MAX_ROWS) return false;
    return App.list.count() < total || capped;
  }

  // Appends the next page. Non-abort errors deliberately propagate: the footer
  // button that called this re-enables itself, so the click can simply be
  // retried — see App.list.renderMore().
  async function loadMore() {
    if (!active || !pageParams) return;
    const request = requestSeq;
    // Into the same slot the query itself uses, so typing over the box drops a
    // page still being computed instead of leaving it to finish for a list it
    // is no longer about.
    if (inflight) inflight.abort();
    const ctrl = new AbortController();
    inflight = ctrl;
    try {
      // Clamped to what is left under the ceiling rather than a flat page:
      // 1000 is not a multiple of 60, so the last page would otherwise land
      // more rows than a refresh is allowed to ask back for, and the overshoot
      // would silently disappear the next time anything reloaded the list.
      // hasMore() has already established there is room for at least one.
      const data = await App.api.search(
        Object.assign({}, pageParams, {
          limit: Math.min(PAGE, MAX_ROWS - App.list.count()),
          offset: App.list.count(),
        }), ctrl.signal);
      // A new query landed while this page was in flight — these rows belong to
      // a search that is no longer on screen.
      if (request !== requestSeq || !active) return;
      total = data.total || 0;
      capped = !!data.total_capped;
      App.list.append(data.rows, true);
      // An empty page is the end of the results whatever the count said.
      App.list.setMore(data.rows.length && hasMore() ? loadMore : null);
      paintStatus();
    } catch (ex) {
      if (ex.name !== "AbortError") throw ex;
    } finally {
      if (inflight === ctrl) inflight = null;
    }
  }

  // What App.shell.reloadList() means while a search is showing. The results
  // came from /api/search, so the rows that an action just changed are only
  // dropped by asking that query again — reloading the folder underneath would
  // replace the search with mail nobody asked to see.
  async function rerun() {
    if (!active) return;
    await run(undefined, true);
  }

  // How long the box waits before asking, and why it is not one number.
  //
  // A search used to cost the better part of a second, so waiting 280ms before
  // starting one was cheap next to running it. It is now answered from the
  // search index in tens of milliseconds, which makes that wait almost the
  // whole of what a search feels like — so it comes down to where a keystroke
  // and its answer still read as one action.
  //
  // Except at the very start of a word. One or two characters match most of a
  // mailbox, and no index helps with that: `de` alone is a second of work
  // against 100k messages. It is also the least likely thing anyone meant to
  // search for — it is a word being typed — so a short query waits for the rest
  // of it instead of costing a second to answer a question nobody asked.
  const DEBOUNCE = 120;
  const DEBOUNCE_SHORT = 450;
  const SHORT = 2;

  function debouncedRun() {
    clearTimeout(timer);
    const request = ++requestSeq;
    const typed = textOf(els().input.value.trim());
    timer = setTimeout(() => run(request), typed.length <= SHORT ? DEBOUNCE_SHORT : DEBOUNCE);
  }

  // Enter commits the search: open the top hit and hand the keyboard to the
  // list, so the results are walkable with j/k straight away — the shortcut
  // table ignores every key while a text field has focus, which would otherwise
  // leave Escape (and losing the search) as the only way out of the box.
  async function openFirst() {
    // Typed and committed inside the debounce window: run now rather than
    // opening whatever the previous keystroke happened to find.
    clearTimeout(timer);
    if (!els().input.value.trim()) return;   // Enter on an empty box is not a search
    const applied = await run();
    if (!applied) return;
    if (!App.list.count()) return;      // no results — stay in the box and keep typing
    if (App.keys) App.keys.focus("list");
    App.list.setFocus(0);
    App.list.openFocused();
    els().input.blur();
  }

  function clear(restore = true) {
    clearTimeout(timer);
    timer = null;
    requestSeq += 1;
    const e = els();
    active = false;
    total = 0;
    capped = false;
    pageParams = null;
    e.input.value = "";
    e.clear.hidden = true;
    e.controls.hidden = true;
    e.status.textContent = "";
    e.status.classList.remove("error");
    e.scope.value = "all";   // a scope left over from the last search is invisible while the box is empty
    if (restore) App.shell.reloadList();
  }

  // A stored value that no longer names an option leaves the select empty, so
  // it is checked against the menu rather than assigned on trust. Nothing
  // stored leaves the markup's own choice standing, which is the narrow window
  // the server would have applied anyway had the box not sent one.
  function restoreYears() {
    const years = els().years;
    if (!years) return;
    let saved = null;
    try { saved = localStorage.getItem(YEARS_KEY); } catch { /* private mode */ }
    if (saved === null) return;
    const known = [...years.options].some((o) => o.value === saved);
    if (known) years.value = saved;
  }

  function init() {
    const e = els();
    restoreYears();
    e.input.addEventListener("input", debouncedRun);
    e.input.addEventListener("keydown", (ev) => {
      if (ev.key !== "Enter") return;
      ev.preventDefault();
      openFirst();
    });
    e.input.addEventListener("focus", () => { if (e.input.value.trim()) e.controls.hidden = false; });
    e.clear.addEventListener("click", () => { clear(true); e.input.focus(); });
    e.rx.addEventListener("change", () => run());
    e.scope.addEventListener("change", () => run());
    e.years.addEventListener("change", () => {
      try { localStorage.setItem(YEARS_KEY, e.years.value); } catch { /* private mode */ }
      run();
    });
    e.clear.innerHTML = App.icon("close", 15);

    e.help.innerHTML = App.icon("info", 15);
    e.help.addEventListener("click", () => { e.helpModal.hidden = false; });
    $("#btn-close-search-help").innerHTML = App.icon("close", 18);
    $("#btn-close-search-help").addEventListener("click", closeHelp);
    e.helpModal.addEventListener("click", (ev) => {
      if (ev.target.id === "search-help-modal") closeHelp();
    });
  }

  function focusInput() {
    const input = els().input;
    input.focus();
    input.select();
  }

  // Put a query in the box, from somewhere other than the keyboard — App.ai's
  // search writer is the only caller. The mode travels with it because half a
  // query is not a query: a regex pasted in with the switch off is a search for
  // those characters literally, and finds nothing.
  //
  // `run` false stops at the box: that is the "let me read it first" button, and
  // it leaves the cursor in the field so the query can be edited where it is.
  function applyQuery(q, mode, run = true) {
    const e = els();
    e.input.value = q;
    e.rx.checked = mode === "regex";
    e.clear.hidden = !q;
    e.controls.hidden = false;
    if (run) return openFirst();
    e.input.focus();
    // The caret goes to the end rather than selecting the lot: the next thing
    // somebody does to a query they are unsure about is tighten it, and a
    // selected query is one keystroke from being gone.
    e.input.setSelectionRange(q.length, q.length);
    return Promise.resolve(false);
  }

  /* Put a query in the box, run it, and leave the results on screen — without
     opening any of them.

     applyQuery's `true` opens the first hit, which is right when the query came
     from a person describing what they were looking for. It is wrong when the
     query came from the Cleanup panel: that is somebody about to delete a
     hundred messages who wants to look at them first, and opening the top one
     would mark it read on the way past. Reviewing mail must not change it.

     The list still takes the keyboard cursor, so j/k walk the group from the
     first row the moment it appears.

     `years` is why this takes the window rather than leaving it alone. The box
     defaults to the last year, which is right for a search somebody is typing
     and wrong for one handed over by the Cleanup panel: that group was counted
     over the whole mailbox, so a view of it cut to twelve months would show a
     third of the rows under a heading that said sixty-four. The control is set
     rather than bypassed, so what ran is readable off the screen. */
  async function showQuery(q, mode = "keyword", years = null) {
    const e = els();
    clearTimeout(timer);
    e.input.value = q;
    if (years !== null) e.years.value = String(years);
    e.rx.checked = mode === "regex";
    e.clear.hidden = !q;
    e.controls.hidden = false;
    const applied = await run();
    if (!applied || !App.list.count()) return applied;
    if (App.keys) App.keys.focus("list");
    App.list.setFocus(0);
    return true;
  }

  // What the reader needs to mark up the thread it is about to open. Filters
  // narrowed the results rather than matching text in them, so a query that is
  // only filters has nothing to highlight.
  function query() {
    const e = els();
    const q = textOf(e.input.value.trim());
    return q ? { q, mode: e.rx.checked ? "regex" : "keyword" } : null;
  }

  function helpOpen() { return !els().helpModal.hidden; }
  function closeHelp() { els().helpModal.hidden = true; }

  return { init, clear, focusInput, query, syncScope, rerun, applyQuery, showQuery,
           isActive: () => active, helpOpen, closeHelp };
})();
