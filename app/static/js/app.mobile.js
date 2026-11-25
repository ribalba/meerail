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
  // The attachment on screen over the reader, as {url, name, download, pages},
  // or null. `pages` is the attachment's id when it is a PDF, which is shown
  // as pictures of its pages and not in the frame. It lives in the history
  // entry as well, so Back closes the viewer before it starts walking back
  // through the panes.
  let att = null;

  const $ = (s) => document.querySelector(s);

  function paint() {
    $("#app").dataset.mview = view;
    // The reader's Back button is labelled with where it goes, not where you
    // are — the folder name is the only thing telling one list page from
    // another, and the thread's own subject is already on screen below it.
    const label = $("#mobile-back-label");
    if (label && App.shell) label.textContent = App.shell.currentTitle() || "Back";
    queuePin();
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
  //
  // That holds for pictures. A PDF in a frame is where phones give up. Safari
  // on iOS draws its first page and offers no way to the second, and Chrome on
  // Android draws nothing. So a PDF is asked for page by page, as pictures the
  // server draws (app/pdfpages.py), and the pages are stacked in a scroller of
  // the viewer's own. The frame stays as the fallback for a PDF the server
  // cannot draw, which is no worse than what every PDF got before.

  // Which PDF the page scroller holds, so that a repaint (Back and Forward
  // both come through here) does not fetch and redraw what is already up.
  let pagesFor = null;
  // Counts what the scroller has been asked to show. A page list that arrives
  // after its viewer was closed, or after another PDF was opened, finds the
  // number moved on and is dropped.
  let pagesGen = 0;
  // Watches the sheets come near the screen and leave again. See drawPages.
  let pageWatch = null;

  // How wide to ask for a page, in real pixels. That is what the screen has
  // across the viewer and half as much again, because nobody reads an A4 page
  // at the width of a phone without zooming in. In steps of 200 so two phones
  // of nearly the same width share an address, and capped under the server's
  // own limit because past this a page is megabytes for sharpness no eye can
  // see.
  function pageWidth(host) {
    const want = host.clientWidth * (window.devicePixelRatio || 1) * 1.5;
    return Math.max(600, Math.min(1800, Math.ceil(want / 200) * 200));
  }

  function dropPages() {
    pagesGen += 1;
    pagesFor = null;
    if (pageWatch) { pageWatch.disconnect(); pageWatch = null; }
    const host = $("#att-viewer-pages");
    for (const t of host.querySelectorAll(".att-page")) clearTimeout(t._wait);
    host.textContent = "";
    host.hidden = true;
  }

  function showFrame() {
    dropPages();
    const frame = $("#att-viewer-frame");
    frame.hidden = false;
    if (frame.getAttribute("src") !== att.url) frame.setAttribute("src", att.url);
  }

  function pagesNote(text) {
    const note = document.createElement("div");
    note.className = "att-pages-note";
    note.textContent = text;
    return note;
  }

  async function showPages() {
    if (pagesFor === att.pages) return;
    dropPages();
    const id = pagesFor = att.pages;
    const gen = pagesGen;
    const frame = $("#att-viewer-frame");
    frame.hidden = true;
    frame.removeAttribute("src");
    const host = $("#att-viewer-pages");
    host.hidden = false;
    host.appendChild(pagesNote("Loading…"));
    let info;
    try {
      info = await App.api.attachmentPages(id);
    } catch (_) {
      // Password-protected, damaged, or a server without the renderer. The
      // browser's own viewer may still manage, and can ask for a password.
      if (gen === pagesGen && att) showFrame();
      return;
    }
    if (gen !== pagesGen) return;
    drawPages(host, id, info);
  }

  // One empty sheet per page, each already the shape its page will be, so the
  // document has its full length from the start and nothing jumps as pictures
  // arrive. A sheet gets its picture when it comes near the screen and gives
  // it up when it is far away again. A decoded page is some fifteen megabytes
  // of bitmap, and a phone that kept two hundred of them would lose the tab.
  // The phone's cache still has the file, so scrolling back costs a decode.
  function drawPages(host, id, info) {
    const width = pageWidth(host);
    host.textContent = "";
    info.pages.forEach(([w, h], i) => {
      const sheet = document.createElement("div");
      sheet.className = "att-page";
      sheet.style.aspectRatio = `${Number(w) || 595} / ${Number(h) || 842}`;
      sheet.dataset.n = String(i + 1);
      host.appendChild(sheet);
    });
    if (info.total > info.pages.length) {
      host.appendChild(pagesNote(`These are the first ${info.pages.length} of ` +
        `${info.total} pages. Open the file in a new tab or download it for the rest.`));
    }

    const load = (sheet) => {
      const n = sheet.dataset.n;
      const img = new Image();
      img.alt = `Page ${n} of ${info.total}`;
      img.decoding = "async";
      img.addEventListener("error", () => {
        if (img.parentNode !== sheet) return;
        sheet.textContent = `Page ${n} could not be drawn. ` +
          "Open the file in a new tab or download it to see it.";
        sheet.classList.add("failed");
      });
      img.src = `/api/attachments/${id}/pages/${n}?w=${width}`;
      sheet.appendChild(img);
    };

    pageWatch = new IntersectionObserver((entries) => {
      for (const e of entries) {
        const sheet = e.target;
        clearTimeout(sheet._wait);
        if (e.isIntersecting) {
          if (sheet.firstChild) continue;
          // Not at once. A flick through a long document passes fifty sheets
          // on its way, and the server draws one page at a time, so asking
          // for all of them would queue the page you stopped on behind the
          // ones you only passed. The wait costs nothing in ordinary reading,
          // because a sheet is asked for a screen and a half before it shows.
          sheet._wait = setTimeout(() => { if (!sheet.firstChild) load(sheet); }, 200);
        } else if (sheet.firstChild) {
          // Emptying the address first is what stops a download in flight.
          const img = sheet.querySelector("img");
          if (img) img.removeAttribute("src");
          sheet.textContent = "";
          // A page that failed gets another try the next time it comes round.
          sheet.classList.remove("failed");
        }
      }
    }, { root: host, rootMargin: "150% 0px" });
    host.querySelectorAll(".att-page").forEach((sheet) => pageWatch.observe(sheet));
  }

  function paintViewer() {
    const box = $("#att-viewer");
    const frame = $("#att-viewer-frame");
    box.hidden = !att;
    queuePin();
    if (!att) {
      // Drop the bytes on the way out; a reopened viewer sets a fresh src, and
      // a PDF left loaded in a hidden frame is a few MB doing nothing. The
      // same goes for the pages of one, which are more.
      frame.removeAttribute("src");
      dropPages();
      return;
    }
    $("#att-viewer-name").textContent = att.name;
    $("#att-viewer-open").href = att.url;
    $("#att-viewer-save").href = att.download;
    $("#att-viewer-save").setAttribute("download", att.name);
    if (att.pages) showPages(); else showFrame();
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

  // --- Zoomed in ----------------------------------------------------------
  // The bars with a Back button on them stick to the top of their page, and a
  // page is as tall as the screen, so they are always in reach. Until the page
  // is zoomed. A mail laid out for a desktop is scaled down to fit and has to
  // be pinched larger to be read, and so does every page of a PDF. Zoomed, the
  // screen shows a part of the page, and the top of the page, bars included,
  // is somewhere above it. Worse, on iOS a finger dragged over the mail
  // scrolls the mail, and the zoomed page itself only moves once the mail is
  // at its very top. So getting back to the inbox from the foot of a long
  // mail meant scrolling all of it back up first.
  //
  // CSS has no "top of what is visible". The visual viewport API has, so while
  // the page is zoomed the bars are moved to the top left corner of the part
  // on screen and scaled back down by the zoom, which draws them exactly the
  // size and width they have unzoomed. At a scale of 1 nothing is touched.
  //
  // Checked in Chromium with an emulated pinch and not on an iPhone, where the
  // viewport events are known to arrive late. If the bars trail the finger
  // there, that is the place to look.
  //
  // Each entry is a bar, where its top left corner sits unzoomed, and whether
  // that is measured on the page or on the window. Worked out from the layout
  // and not measured off the bar, because a measurement would include the
  // transform this is about to replace.
  const VV = window.visualViewport;

  function pinned() {
    const pane = $("#reading-pane");
    const nav = $("#mobile-nav");
    const vbar = $("#att-viewer-bar");
    return [
      [nav, pane.offsetLeft, pane.offsetTop, false],
      // The action bar sticks directly under the nav. See mail.css.
      [$("#reader-bar"), pane.offsetLeft, pane.offsetTop + nav.offsetHeight, false],
      // The viewer is position: fixed, so its bar is placed against the
      // window and stays there if the page behind it should ever scroll.
      [vbar, vbar.offsetLeft, vbar.offsetTop, true],
    ];
  }

  // Once per frame however many events arrive. A pinch reports its progress in
  // bursts, and each of them would otherwise write the same three transforms.
  let pinQueued = false;
  function queuePin() {
    if (pinQueued) return;
    pinQueued = true;
    requestAnimationFrame(pinBars);
  }

  function pinBars() {
    pinQueued = false;
    // The 1% is slack for a scale that comes back as 1.0000001. A real zoom
    // is never that small, and iOS's own zoom on a small input starts at 1.1.
    const s = MQ.matches && VV ? VV.scale : 1;
    for (const [bar, x, y, fixed] of pinned()) {
      if (!(s > 1.01)) { bar.style.transform = ""; continue; }
      // Where the visible part starts, in the coordinates the bar is laid out
      // in. The two only differ if the page itself has scrolled, which this
      // app's page does not do, but a keyboard coming up can push it.
      const left = fixed ? VV.offsetLeft : VV.pageLeft;
      const top = fixed ? VV.offsetTop : VV.pageTop;
      bar.style.transform =
        `translate(${left + x / s - x}px, ${top + y / s - y}px) scale(${1 / s})`;
    }
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
        // Set by app.reader.js on a PDF. Absent, the viewer frames the file.
        pages: chip.dataset.pages || null,
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
      queuePin();
    });

    // `resize` is the zoom changing and `scroll` is the zoomed page being
    // dragged about. Both move the corner the bars are pinned to.
    if (VV) {
      VV.addEventListener("resize", queuePin);
      VV.addEventListener("scroll", queuePin);
    }

    paint();
  }

  // `narrow` is app.keys.js asking whether moving the keyboard between panes
  // also has to turn a page — on desktop all three are already on screen.
  return { init, show, back, current: () => view, narrow: () => MQ.matches };
})();
