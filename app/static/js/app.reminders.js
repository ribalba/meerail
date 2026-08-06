/* meerail reminders: "not today".

   Two things live here. The menu that files a conversation away and says when it
   should come back, and the strip the reader draws over a conversation that is
   waiting on one — which is the only place the promise can be taken back from,
   since the mail has left the folder the button was pressed in.

   Every preset is computed here rather than asked for by name. "Next Monday,
   nine o'clock" is a question about *this* browser's calendar: its timezone, its
   week, its daylight-saving changeover. The server has none of that and would
   have to be told the offset to answer — and would then be wrong for half the
   year. So the date arithmetic happens where the calendar is and what crosses
   the wire is an absolute instant (see App.api.remind). */

App.reminders = (function () {
  // What the presets mean, in the reader's own local hours. Whole hours on
  // purpose: a reminder is a rough intention ("Monday morning"), and a list of
  // options reading 09:37 would be answering a question nobody asked.
  const MORNING = 9;
  const EVENING = 18;
  const LATER_HOURS = 3;      // how far out "later today" pushes something
  // Past this hour "later today" is not later today any more — it is the middle
  // of the night, and the option stops being offered rather than lying.
  const LATE_CUTOFF = 22;

  let openMenu = null;

  // --- Working out when ---

  // A copy of `d` at `hour:00` local, `addDays` later. setDate before setHours
  // so a month or year boundary rolls over on its own, and so the hour is set on
  // the day it lands on — which is what keeps a clock-change week honest.
  function at(d, hour, addDays = 0) {
    const out = new Date(d);
    out.setDate(out.getDate() + addDays);
    out.setHours(hour, 0, 0, 0);
    return out;
  }

  // The next `weekday` (0 = Sunday) that is not today, at `hour`. Today never
  // counts: "next week" pressed on a Monday means the Monday after this one, and
  // "this weekend" pressed on a Friday means tomorrow.
  function nextWeekday(now, weekday, hour) {
    const delta = ((weekday - now.getDay() + 7) % 7) || 7;
    return at(now, hour, delta);
  }

  // Three hours out, rounded up to the hour, so the menu offers "16:00" and not
  // "15:47".
  function laterToday(now) {
    const out = new Date(now.getTime() + LATER_HOURS * 3600000);
    out.setMinutes(0, 0, 0);
    out.setHours(out.getHours() + 1);
    return out;
  }

  // The offered times, in the order they come round. Options that have gone by
  // are left out rather than shown greyed: an evening reminder offered at half
  // past eleven at night is a menu entry that cannot mean what it says.
  function presets(now = new Date()) {
    const out = [];
    const later = laterToday(now);
    if (later.getDate() === now.getDate() && later.getHours() <= LATE_CUTOFF) {
      out.push({ key: "later", label: "Later today", when: later });
    }
    if (now.getHours() < EVENING - 1) {
      out.push({ key: "evening", label: "This evening", when: at(now, EVENING) });
    }
    out.push({ key: "tomorrow", label: "Tomorrow", when: at(now, MORNING, 1) });
    const day = now.getDay();
    // Monday to Thursday only: on Friday the weekend is "tomorrow", and once it
    // has started "this weekend" is now.
    if (day >= 1 && day <= 4) {
      out.push({ key: "weekend", label: "This weekend", when: nextWeekday(now, 6, MORNING) });
    }
    out.push({ key: "week", label: "Next week", when: nextWeekday(now, 1, MORNING) });
    return out;
  }

  // When a reminder lands, said the way someone reads a calendar: a time for
  // today, a weekday for this week, a date beyond it.
  function stamp(d, now = new Date()) {
    if (!d || isNaN(d)) return "";
    const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    if (d.toDateString() === now.toDateString()) return time;
    const days = (d.getTime() - now.getTime()) / 86400000;
    if (days >= 0 && days < 6) return d.toLocaleDateString([], { weekday: "short" }) + " " + time;
    return d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + time;
  }

  // The value a <input type="datetime-local"> wants: local wall-clock, no zone.
  function localValue(d) {
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
      + `T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  // --- The menu ---

  function close() {
    if (!openMenu) return;
    openMenu.el.remove();
    document.removeEventListener("mousedown", openMenu.onOutside, true);
    document.removeEventListener("keydown", openMenu.onKey, true);
    document.removeEventListener("scroll", close, true);
    window.removeEventListener("resize", close);
    openMenu = null;
  }

  // What the menu leads with when the conversation is already parked: what it is
  // waiting for, and the two ways out of it.
  //
  // The strip over the thread offers the same two, and this is not a duplicate
  // of it. The strip can only be read by someone who has the conversation open
  // and has looked at the top of the pane; this is on the button — and on `b` —
  // which is where anyone who set a reminder goes to change their mind about it.
  // Without it the bell answered "remind me again when?" to a conversation that
  // was already waiting, which is the one question that was not being asked.
  function currentBlock(rem) {
    if (!rem) return "";
    const when = stamp(App.utcDate(rem.due_at));
    return `<div class="rm-head">${rem.overdue ? "Due back" : "Coming back"} ${App.esc(when)}</div>
      <button class="move-item" data-act="wake">
        <span class="mm-icon">${App.icon("inbox", 15)}</span>
        <span class="mm-name">Bring back now</span></button>
      <button class="move-item" data-act="drop">
        <span class="mm-icon">${App.icon("close", 15)}</span>
        <span class="mm-name">Clear reminder</span></button>
      <div class="rm-sep"></div>`;
  }

  function open(m, anchor) {
    close();
    if (!anchor) return;
    const now = new Date();
    const items = presets(now);
    // Read off the open conversation rather than the message: a reminder is set
    // on the thread, and the toolbar's target is only one message of it.
    const rem = App.reader.currentReminder();

    const el = document.createElement("div");
    el.className = "move-menu remind-menu";
    el.innerHTML = currentBlock(rem)
      + `<div class="rm-head">${rem ? "Change to…" : "Remind me…"}</div>`
      + items.map((p, i) => `<button class="move-item" data-idx="${i}">
          <span class="mm-name">${App.esc(p.label)}</span>
          <span class="rm-when">${App.esc(stamp(p.when, now))}</span></button>`).join("")
      + `<div class="rm-sep"></div>
         <form class="rm-custom">
           <input type="datetime-local" class="rm-input" aria-label="Remind me at"
                  min="${localValue(new Date(now.getTime() + 60000))}"
                  value="${localValue(at(now, MORNING, 1))}" />
           <button type="submit" class="rm-set">Set</button>
         </form>`;
    document.body.appendChild(el);

    // Right under the button, nudged back on screen when the menu would run off
    // the bottom or the right — same placement as the move menu.
    const r = anchor.getBoundingClientRect();
    el.style.top = Math.min(r.bottom + 4, window.innerHeight - el.offsetHeight - 8) + "px";
    el.style.left = Math.max(8, Math.min(r.left, window.innerWidth - el.offsetWidth - 8)) + "px";

    el.addEventListener("click", (e) => {
      const verb = e.target.closest("[data-act]");
      if (verb) {
        close();
        // "wake" returns the mail, "drop" leaves it filed and forgets the
        // promise — see App.reader.unremindThread.
        return App.reader.unremindThread(verb.dataset.act === "wake");
      }
      const item = e.target.closest("[data-idx]");
      if (!item) return;
      const chosen = items[Number(item.dataset.idx)];
      close();
      if (chosen) App.reader.remindThread(chosen.when);
    });
    el.querySelector(".rm-custom").addEventListener("submit", (e) => {
      e.preventDefault();
      const raw = el.querySelector(".rm-input").value;
      // Parsed as local time, which is what the field means and what every
      // preset above is. The server refuses anything already past, so a date
      // typed by hand into yesterday comes back as an error rather than a
      // conversation that vanishes and never returns.
      const when = raw ? new Date(raw) : null;
      if (!when || isNaN(when)) return;
      close();
      App.reader.remindThread(when);
    });

    openMenu = {
      el,
      onOutside: (e) => { if (!el.contains(e.target) && e.target !== anchor) close(); },
      onKey: (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } },
    };
    document.addEventListener("mousedown", openMenu.onOutside, true);
    document.addEventListener("keydown", openMenu.onKey, true);
    // Fixed positioning: without these the menu would sit still while the pane
    // it is anchored to scrolls out from under it.
    document.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
  }

  // --- The strip over a parked conversation ---

  // Drawn by the reader above the thread. It exists because the conversation is
  // not in the folder the reminder was set from any more — this is where "when
  // is this coming back", "bring it back now" and "never mind" live, and there
  // is nowhere else they could.
  function strip(thread) {
    const rem = thread && thread.reminder;
    if (!rem) return null;
    const due = App.utcDate(rem.due_at);
    const el = document.createElement("div");
    el.className = "remind-strip" + (rem.overdue ? " late" : "");
    // Overdue means the moment has passed and the mail has not landed yet: the
    // server was off, or the move has not been applied. It is late, not lost —
    // the wording says so, and `error` says what is holding it up when anything
    // is.
    const when = stamp(due);
    const head = rem.overdue
      ? `Due back ${App.esc(when)} — still on its way`
      : `Coming back ${App.esc(when)}`;
    el.innerHTML = `
      <span class="rs-icon">${App.icon("bell", 15)}</span>
      <div class="rs-text">
        <span class="rs-head">${head}</span>
        <span class="rs-sub">Filed in Archive until then.${
          rem.error ? " " + App.esc(rem.error) : ""}</span>
      </div>
      <button class="rs-btn" data-act="wake">Bring back now</button>
      <button class="rs-btn ghost" data-act="drop">Clear reminder</button>`;
    el.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-act]");
      if (!btn) return;
      App.reader.unremindThread(btn.dataset.act === "wake");
    });
    return el;
  }

  return { presets, stamp, open, close, strip };
})();
