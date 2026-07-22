/* meerail search: the Apple-Mail search bar — regex/keyword, scope, time window. */

App.search = (function () {
  let active = false;
  let timer = null;
  let requestSeq = 0;
  const $ = (s) => document.querySelector(s);

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
  const FILTER_RE = /(?:(?<=\s)|^):(?:unread|read|has-attachments?)(?:\s+|$)/gi;
  const ADDR_RE = /(?:(?<=\s)|^):(?:from|to)(?:\s+|=)(?:"[^"]*"|[^\s:]\S*)(?:\s+|$)/gi;
  const PARTIAL_RE = /(?:(?<=\s)|^):(?:from|to)=?\s*$/i;

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
        .map((mb) => `<option value="${mb.id}">${App.esc(mb.display_name)}</option>`)
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

    try {
      const data = await App.api.search(params);
      if (request !== requestSeq) return false;
      if (!refresh) {
        App.list.reset();
        App.reader.clear();
      }
      // render() prunes ticks whose rows are gone and keeps the keyboard cursor
      // on the slot a deleted row vacated, so a refresh needs nothing further.
      App.list.render(data.rows, true);
      e.status.textContent = data.total === 0 ? "No results"
        : `${data.total} result${data.total === 1 ? "" : "s"}`;
      $("#list-title").textContent = e.rx.checked ? "Regex search" : "Search";
      return true;
    } catch (ex) {
      if (request !== requestSeq) return false;
      e.status.classList.add("error");
      e.status.textContent = ex.message || "Search failed";
      return false;
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

  function debouncedRun() {
    clearTimeout(timer);
    const request = ++requestSeq;
    timer = setTimeout(() => run(request), 280);
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
    e.input.value = "";
    e.clear.hidden = true;
    e.controls.hidden = true;
    e.status.textContent = "";
    e.status.classList.remove("error");
    e.scope.value = "all";   // a scope left over from the last search is invisible while the box is empty
    if (restore) App.shell.reloadList();
  }

  function init() {
    const e = els();
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
    e.years.addEventListener("change", () => run());
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

  return { init, clear, focusInput, query, syncScope, rerun, isActive: () => active,
           helpOpen, closeHelp };
})();
