/* Draggable divider between the message list and the reading pane.
   The width lives on --list-w (read by .app's grid template) and is persisted,
   so it survives reloads. */

App.split = (function () {
  const STORE_KEY = "meerail.listWidth";
  const DEFAULT = 340;
  const MIN = 240;          // narrower and the sender/date row starts truncating
  const READER_MIN = 380;   // the reader must stay wide enough to be worth having

  const $ = (s) => document.querySelector(s);

  // The sidebar is a fixed track, so the reader gets whatever is left over after
  // it and the list. Measure it rather than hard-coding 232px — the media query
  // narrows it, and a stale constant would let the reader be squeezed below its
  // minimum.
  function maxWidth() {
    const sidebar = $("#sidebar");
    const used = sidebar ? sidebar.offsetWidth : 232;
    return Math.max(MIN, window.innerWidth - used - READER_MIN);
  }

  function clamp(px) { return Math.min(Math.max(px, MIN), maxWidth()); }

  function apply(px, persist) {
    const w = clamp(px);
    document.documentElement.style.setProperty("--list-w", w + "px");
    if (persist) localStorage.setItem(STORE_KEY, String(Math.round(w)));
    return w;
  }

  function stored() {
    const raw = parseInt(localStorage.getItem(STORE_KEY), 10);
    return Number.isFinite(raw) ? raw : DEFAULT;
  }

  function init() {
    const bar = $("#pane-divider");
    if (!bar) return;
    apply(stored(), false);

    // Position is taken from the list pane's left edge, not from a delta, so a
    // pointer that outruns the clamp doesn't accumulate phantom offset and the
    // handle stays glued to the cursor on the way back.
    const pane = $("#list-pane");

    bar.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      const left = pane.getBoundingClientRect().left;
      bar.setPointerCapture(e.pointerId);
      bar.classList.add("dragging");
      document.body.classList.add("resizing");

      const move = (ev) => apply(ev.clientX - left, false);
      const up = (ev) => {
        bar.releasePointerCapture(ev.pointerId);
        bar.classList.remove("dragging");
        document.body.classList.remove("resizing");
        bar.removeEventListener("pointermove", move);
        bar.removeEventListener("pointerup", up);
        // Write once at the end — dragging fires far too often for localStorage.
        apply(ev.clientX - left, true);
      };
      bar.addEventListener("pointermove", move);
      bar.addEventListener("pointerup", up);
    });

    bar.addEventListener("dblclick", () => apply(DEFAULT, true));

    // Keyboard resize, so the divider is usable from the tab order it advertises.
    bar.addEventListener("keydown", (e) => {
      const step = e.shiftKey ? 40 : 10;
      if (e.key === "ArrowLeft") apply(pane.offsetWidth - step, true);
      else if (e.key === "ArrowRight") apply(pane.offsetWidth + step, true);
      else return;
      e.preventDefault();
    });

    // A window that shrank may have left the stored width overlapping the
    // reader's minimum; re-clamp without overwriting what the user chose, so the
    // original width returns when there is room for it again.
    window.addEventListener("resize", () => apply(stored(), false));
  }

  return { init };
})();
