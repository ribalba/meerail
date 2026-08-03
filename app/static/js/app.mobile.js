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
  // The attachment on screen over the reader — {url, name, download} — or null.
  // It lives in the history entry as well, so Back closes the viewer before it
  // starts walking back through the panes.
  let att = null;

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

  // --- Attachment viewer ------------------------------------------------
  // Tapping a PDF chip opens the browser's own viewer, full screen, with the
  // thread nowhere in reach — on a phone that is a dead end. Viewable
  // attachments are shown here instead: the same URL the chip pointed at, in a
  // frame under a header that goes back.
  function paintViewer() {
    const box = $("#att-viewer");
    const frame = $("#att-viewer-frame");
    box.hidden = !att;
    if (!att) {
      // Drop the bytes on the way out; a reopened viewer sets a fresh src, and
      // a PDF left loaded in a hidden frame is a few MB doing nothing.
      frame.removeAttribute("src");
      return;
    }
    if (frame.getAttribute("src") !== att.url) frame.setAttribute("src", att.url);
    $("#att-viewer-name").textContent = att.name;
    $("#att-viewer-open").href = att.url;
    $("#att-viewer-save").href = att.download;
    $("#att-viewer-save").setAttribute("download", att.name);
  }

  function openAttachment(a) {
    att = a;
    paintViewer();
    history.pushState({ mview: view, att: a }, "");
  }

  // Mirrors back(): prefer unwinding the entry we pushed, so the header button
  // and the browser's own Back leave the stack in the same shape.
  function closeAttachment() {
    if (history.state && history.state.att) history.back();
    else { att = null; paintViewer(); }
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

    $("#btn-back-message").insertAdjacentHTML("afterbegin", App.icon("chevron", 20));
    $("#att-viewer-open").innerHTML = App.icon("external", 18);
    $("#att-viewer-save").innerHTML = App.icon("download", 18);
    $("#btn-back-message").addEventListener("click", closeAttachment);

    // Delegated, because the reader redraws its chips on every render. Only the
    // chips the server marked viewable carry target=_blank; the rest are plain
    // downloads and are left alone.
    document.addEventListener("click", (e) => {
      if (!MQ.matches) return;
      const chip = e.target.closest && e.target.closest('a.attachment-chip[target="_blank"]');
      if (!chip || e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;
      e.preventDefault();
      // Same resource without the inline disposition — what the save button
      // hands the browser, so it offers a file rather than another preview.
      const dl = new URL(chip.href, location.href);
      dl.searchParams.delete("inline");
      openAttachment({
        url: chip.getAttribute("href"),
        name: chip.title || chip.querySelector(".att-name").textContent,
        download: dl.pathname + dl.search,
      });
    });

    window.addEventListener("popstate", (e) => {
      view = (e.state && e.state.mview) || "folders";
      att = (e.state && e.state.att) || null;
      paint();
      paintViewer();
    });

    // A window dragged wide with an attachment up: the viewer is a phone
    // affordance, and desktop already opens these in a tab of their own.
    MQ.addEventListener("change", () => {
      if (!MQ.matches && att) { att = null; paintViewer(); }
    });

    paint();
  }

  // `narrow` is app.keys.js asking whether moving the keyboard between panes
  // also has to turn a page — on desktop all three are already on screen.
  return { init, show, back, current: () => view, narrow: () => MQ.matches };
})();
