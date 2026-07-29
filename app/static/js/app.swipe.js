/* Swipe actions on message rows — touch, narrow layout only.

   Right takes the conversation to Trash, left to Archive: the two filing verbs
   the reader already has as buttons, reachable without opening the mail first.
   Right is delete because it is the one that carries a colour everyone reads as
   "careful", and it wants the deliberate direction on a right-handed thumb.

   The row is not moved — its *contents* are, over a coloured bed that only
   exists once someone actually swipes. That keeps the row's place in the list
   (and the reveal clipped by its own rounded corners) and costs a list that is
   only ever read nothing at all.

   Nothing here fires until the gesture has been called horizontal, and a
   gesture that starts out vertical is handed back to the scroller for good —
   flicking through a folder must never arm a delete. */

App.swipe = (function () {
  // Same breakpoint as app.mobile.js/mail.css: swiping belongs to the layout
  // where a row fills the width and there is no hover to reveal anything.
  const MQ = window.matchMedia("(max-width: 900px)");

  const DECIDE = 10;    // px of travel before the gesture is called an axis
  const COMMIT = 0.32;  // share of the row's width that arms the action
  const MIN_COMMIT = 72;
  const SETTLE = 200;   // must match the transition in mail.css

  let g = null;         // the gesture in flight, or null
  let blockUntil = 0;   // a finished swipe must not also read as a tap

  const now = () => (window.performance ? performance.now() : Date.now());

  // The contents move by --swipe-x and the coloured bed fills exactly what they
  // uncover, so the two can never drift apart into a seam or an overlap.
  function setX(el, px) {
    el.style.setProperty("--swipe-x", px.toFixed(1) + "px");
    el.style.setProperty("--swipe-w", Math.abs(px).toFixed(1) + "px");
  }

  function threshold(el) { return Math.max(MIN_COMMIT, el.offsetWidth * COMMIT); }

  // Past the commit point the finger keeps moving but the row barely does, so
  // "as far as it goes" is felt rather than read off the screen.
  function damp(dx, lim) {
    const a = Math.abs(dx);
    return a <= lim ? dx : Math.sign(dx) * (lim + (a - lim) * 0.3);
  }

  // Built on the first swipe of a row and kept afterwards — a row is usually
  // swiped once, and the second swipe of the same one should not have to wait
  // for a layout. Both verbs live in it; CSS shows the one being swiped towards.
  function bed(el) {
    let back = el.querySelector(":scope > .swipe-back");
    if (back) return back;
    back = document.createElement("div");
    back.className = "swipe-back";
    back.setAttribute("aria-hidden", "true");
    back.innerHTML =
      `<span class="swipe-act act-trash">${App.icon("trash", 17)}<span>Delete</span></span>` +
      `<span class="swipe-act act-archive"><span>Archive</span>${App.icon("archive", 17)}</span>`;
    el.insertBefore(back, el.firstChild);
    return back;
  }

  function clearRow(el) {
    el.classList.remove("swiping", "swipe-left", "swipe-right", "swipe-armed");
  }

  // Back to rest, then drop the direction classes — dropping them straight away
  // would recolour the bed while it is still on screen sliding shut.
  function release(el) {
    el.classList.remove("swiping", "swipe-armed");
    setX(el, 0);
    setTimeout(() => { if (el.isConnected) clearRow(el); }, SETTLE);
  }

  // Which folder a lone message is being moved out of. Folder rows carry it;
  // search results do not (they span folders and never name one), so that one
  // case asks the server rather than guessing — the same order of preference
  // the reader's move menu uses: the folder on screen, else the inbox copy.
  async function sourceMailbox(r) {
    if (r.mailbox_id) return r.mailbox_id;
    const locs = (await App.api.message(r.id)).locations || [];
    const here = App.shell && App.shell.currentMailboxId();
    const loc = locs.find((l) => l.mailbox_id === here)
      || locs.find((l) => l.role === "inbox") || locs[0];
    if (!loc) throw new Error("No source mailbox for this message");
    return loc.mailbox_id;
  }

  // A threaded conversation is filed whole — the same call the reader makes, so
  // the row can't leave a stray reply behind in the folder. A message that never
  // got threaded stands alone, and its one placement is the whole job.
  async function apply(r, act) {
    if (r.thread_id) {
      return act === "trash" ? App.api.trashThread(r.thread_id, r.account_id)
                             : App.api.archiveThread(r.thread_id, r.account_id);
    }
    const source = await sourceMailbox(r);
    return act === "trash" ? App.api.trashMsg(r.id, source)
                           : App.api.archiveMsg(r.id, source);
  }

  async function commit(el, r, act) {
    el.classList.remove("swiping");
    el.classList.add("swipe-armed");
    // The row sits there, emptied, until the request comes back; a second swipe
    // across it in the meantime would file the same mail twice.
    el.dataset.swipeBusy = "1";
    setX(el, act === "trash" ? el.offsetWidth : -el.offsetWidth);
    try {
      await apply(r, act);
    } catch (e) {
      // The mail is still where it was, so the row has to come back rather than
      // sit blank over a red bed.
      delete el.dataset.swipeBusy;
      release(el);
      alert((act === "trash" ? "Could not delete: " : "Could not archive: ")
        + (e.message || "action failed"));
      return;
    }
    // Out of the flow before the reload lands: the rows below close the gap at
    // once instead of holding an empty slot for the width of a round trip.
    el.style.display = "none";
    // The reader is a separate page down here and would otherwise keep showing
    // the conversation that just left the folder.
    if (App.reader && App.list.activeId() === r.id) App.reader.clear();
    if (App.shell) await App.shell.reloadList();
  }

  function stop(swiped) {
    if (!g) return;
    if (swiped) blockUntil = now() + 400;
    g = null;
    document.removeEventListener("touchmove", onMove, { passive: false });
    document.removeEventListener("touchend", onEnd);
    document.removeEventListener("touchcancel", onCancel);
  }

  function onMove(e) {
    if (!g) return;
    // A refresh can replace the list mid-gesture; there is nothing left to drag.
    if (!g.el.isConnected) return stop(false);
    const t = e.touches[0];
    const dx = t.clientX - g.x0;
    const dy = t.clientY - g.y0;

    if (!g.axis) {
      if (Math.max(Math.abs(dx), Math.abs(dy)) < DECIDE) return;
      // Decided once and never revisited: a diagonal drift partway through a
      // scroll must not turn into a swipe under the finger.
      if (Math.abs(dy) >= Math.abs(dx)) return stop(false);
      g.axis = "x";
      bed(g.el);
      g.el.classList.add("swiping");
    }

    // Ours now — without this the list scrolls under the moving row.
    e.preventDefault();
    g.dx = dx;
    const lim = threshold(g.el);
    setX(g.el, damp(dx, lim));
    g.el.classList.toggle("swipe-right", dx > 0);
    g.el.classList.toggle("swipe-left", dx < 0);

    const armed = Math.abs(dx) >= lim;
    if (armed !== g.armed) {
      g.armed = armed;
      g.el.classList.toggle("swipe-armed", armed);
      // Crossing the commit point is the only thing here you cannot see without
      // looking down at the screen.
      if (armed && navigator.vibrate) navigator.vibrate(8);
    }
  }

  function onEnd() {
    if (!g) return;
    const { el, r, dx, axis } = g;
    const armed = axis === "x" && Math.abs(dx) >= threshold(el);
    const act = dx > 0 ? "trash" : "archive";
    stop(axis === "x");
    if (armed) commit(el, r, act);
    else if (axis === "x") release(el);
  }

  // A cancelled touch (a call arriving, the browser taking the gesture) is not
  // a decision — it always springs back.
  function onCancel() {
    if (!g) return;
    const el = g.el;
    const swiped = g.axis === "x";
    stop(swiped);
    if (swiped) release(el);
  }

  function onStart(e, el, r) {
    if (g || !MQ.matches || e.touches.length !== 1) return;
    // Ticking rows is its own mode with its own delete button; a swipe there
    // would act on one row while the bar counts another number.
    if (App.bulk && App.bulk.isActive()) return;
    if (el.dataset.swipeBusy || e.target.closest(".msg-check")) return;
    const t = e.touches[0];
    g = { el, r, x0: t.clientX, y0: t.clientY, dx: 0, axis: null, armed: false };
    document.addEventListener("touchmove", onMove, { passive: false });
    document.addEventListener("touchend", onEnd);
    document.addEventListener("touchcancel", onCancel);
  }

  // Called by app.list.js for every row it builds. Only touchstart is bound per
  // row; the rest of the gesture is followed on the document, so a finger that
  // wanders off the row it started on still finishes the swipe it began.
  function attach(el, r) {
    el.addEventListener("touchstart", (e) => onStart(e, el, r), { passive: true });
  }

  // Consulted by the row's click handler. preventDefault on the move usually
  // stops the synthetic click on its own, but not on every browser, and opening
  // the conversation you just archived is the one outcome worth being sure about.
  function blocked() { return now() < blockUntil; }

  return { attach, blocked };
})();
