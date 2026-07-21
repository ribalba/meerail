/* meerail search: the Apple-Mail search bar — regex/keyword, scope, time window. */

App.search = (function () {
  let active = false;
  let timer = null;
  const $ = (s) => document.querySelector(s);

  function els() {
    return {
      input: $("#search-input"), clear: $("#search-clear"), controls: $("#search-controls"),
      rx: $("#rx-toggle"), scope: $("#scope-select"), years: $("#years-select"),
      status: $("#search-status"),
    };
  }

  async function run() {
    const e = els();
    const q = e.input.value.trim();
    e.clear.hidden = q === "";
    if (!q) return clear(false);

    active = true;
    e.controls.hidden = false;
    e.status.classList.remove("error");
    e.status.textContent = "Searching…";

    const params = { q, mode: e.rx.checked ? "regex" : "keyword", years: e.years.value };
    if (e.scope.value === "mailbox") {
      const mid = App.shell.currentMailboxId();
      if (mid) params.mailbox_id = mid;
    }

    try {
      const data = await App.api.search(params);
      App.list.reset();
      App.reader.clear();
      App.list.render(data.rows, true);
      e.status.textContent = data.total === 0 ? "No results"
        : `${data.total} result${data.total === 1 ? "" : "s"}`;
      $("#list-title").textContent = e.rx.checked ? "Regex search" : "Search";
    } catch (ex) {
      e.status.classList.add("error");
      e.status.textContent = ex.message || "Search failed";
    }
  }

  function debouncedRun() {
    clearTimeout(timer);
    timer = setTimeout(run, 280);
  }

  function clear(restore = true) {
    const e = els();
    active = false;
    e.input.value = "";
    e.clear.hidden = true;
    e.controls.hidden = true;
    e.status.textContent = "";
    e.status.classList.remove("error");
    if (restore) App.shell.reloadList();
  }

  function init() {
    const e = els();
    e.input.addEventListener("input", debouncedRun);
    e.input.addEventListener("focus", () => { if (e.input.value.trim()) e.controls.hidden = false; });
    e.clear.addEventListener("click", () => { clear(true); e.input.focus(); });
    e.rx.addEventListener("change", run);
    e.scope.addEventListener("change", run);
    e.years.addEventListener("change", run);
    e.clear.innerHTML = App.icon("close", 15);
  }

  return { init, clear, isActive: () => active };
})();
