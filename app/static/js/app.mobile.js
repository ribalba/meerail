/* Narrow-screen navigation: folders → list → reader as three pages.

   There is no separate mobile layout to keep in step — the same three panes
   are simply shown one at a time, which mail.css does off `data-mview` on
   #app. All this module owns is which of the three is up, and keeping the
   browser's own Back button walking them backwards. */

App.mobile = (function () {
  // The pages only exist below this width, and it has to agree with the
  // breakpoint in mail.css. The CSS query is what actually moves the layout;
  // this one only decides whether a move is worth a history entry.
  const MQ = window.matchMedia("(max-width: 900px)");

  let view = "folders";

  const $ = (s) => document.querySelector(s);

  function paint() {
    $("#app").dataset.mview = view;
    // The reader's Back button is labelled with where it goes, not where you
    // are — the folder name is the only thing telling one list page from
    // another, and the thread's own subject is already on screen below it.
    const label = $("#mobile-back-label");
    if (label && App.shell) label.textContent = App.shell.currentTitle() || "Back";
  }

  // `push` is off when the browser is the one driving: popstate has already
  // moved the stack, and pushing there would fight the entry it just popped.
  function show(v, push = true) {
    if (v === view) { paint(); return; }
    view = v;
    paint();
    // Desktop shows all three panes at once, so there is nothing to go back
    // *to* — leaving those transitions out keeps the stack clean for someone
    // who never crosses the breakpoint.
    if (push && MQ.matches) history.pushState({ mview: v }, "");
  }

  // Only pop when the entry on top is demonstrably one of ours, which is the
  // one case where the browser has somewhere to put us. Otherwise — a window
  // dragged narrow with a thread already open, say — switch directly rather
  // than risk a history.back() that leaves the app altogether.
  function back(fallback) {
    if (history.state && history.state.mview === view) history.back();
    else show(fallback, false);
  }

  function init() {
    // Stamp the entry we launched on, so the first real push has something
    // coherent underneath it and a pop back to it lands on the folder list.
    history.replaceState({ mview: "folders" }, "");

    $("#btn-back-folders").innerHTML = App.icon("chevron", 20);
    $("#btn-back-list").insertAdjacentHTML("afterbegin", App.icon("chevron", 20));
    $("#btn-back-folders").addEventListener("click", () => back("folders"));
    $("#btn-back-list").addEventListener("click", () => back("list"));

    window.addEventListener("popstate", (e) => {
      view = (e.state && e.state.mview) || "folders";
      paint();
    });

    paint();
  }

  return { init, show, current: () => view };
})();
