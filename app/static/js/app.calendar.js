/* meerail calendar. Puts the event a mail carries into meercal.

   A mail with an .ics file gets a strip in the reader, and the strip opens a
   dialog that says what is in the file and asks which meercal calendar it
   goes into. Neither exists until a meercal address is saved in Settings, so
   `configured` is fetched at boot and re-read whenever that section is saved,
   the same arrangement App.tasks has for Meerato. Everything goes through
   /api/calendar/*. The browser never talks to meercal itself, because the
   password meercal wants is kept on the server and never sent here (see
   app/routers/calendar.py and app/meercal.py). */

App.calendar = (function () {
  const $ = (s) => document.querySelector(s);

  // The calendar the last import went into, so the next invitation offers the
  // same one. Per browser rather than on the server, because it is a
  // convenience, and losing it costs one click in the dropdown.
  const LAST_KEY = "meerail.calendar.last";
  // meercal's own colours, passed on by the server. Checked again here before
  // one is painted, because a value that is not a colour has no business in a
  // style property even when nothing could come of it.
  const COLOR_RE = /^#[0-9a-f]{6}$/i;
  // How long the result stays readable before the dialog closes on its own.
  // Longer when it carries the second sentence about the calendar server,
  // which nobody reads in a second and a half.
  const CLOSE_AFTER_MS = 1500;
  const CLOSE_AFTER_QUEUED_MS = 3500;

  let configured = false;
  let current = null;     // { m, a }, the message and the file the open dialog imports
  let calendars = [];     // the writable calendars of the last options answer
  // Bumped by every open() and close(). A request remembers the value it was
  // sent under and drops its answer when that has moved on, so a slow meercal
  // cannot fill the dialog for one invitation with what it said about another.
  let seq = 0;
  let closeTimer = null;

  async function init() {
    // Wired before the config is asked for, so the dialog works even if that
    // first request is slow to come back.
    $("#btn-close-calendar").innerHTML = App.icon("close", 18);
    $("#btn-close-calendar").addEventListener("click", close);
    $("#calendar-modal").addEventListener("click", (e) => {
      if (e.target.id === "calendar-modal") close();
    });
    $("#calendar-import").addEventListener("click", importIt);
    $("#calendar-body").addEventListener("change", (e) => {
      if (e.target.id === "calendar-target") paintSwatch();
    });
    await refreshConfig();
  }

  // The strip appears and disappears with the setting, so the reader is redrawn
  // on every change rather than only when a thread opens. Answers null when the
  // server could not be asked. The state stays as it was then, because a
  // network blip is no reason to take the strips down, and Settings leaves
  // its fields alone rather than emptying them.
  async function refreshConfig() {
    let cfg;
    try { cfg = await App.api.calendarConfig(); } catch (_) { return null; }
    const was = configured;
    configured = !!(cfg && cfg.configured);
    if (was !== configured && App.reader) App.reader.redraw();
    return cfg;
  }

  function enabled() { return configured; }
  function isOpen() { return !$("#calendar-modal").hidden; }

  function close() {
    $("#calendar-modal").hidden = true;
    clearTimeout(closeTimer);
    closeTimer = null;
    seq++;
    current = null;
  }

  function setStatus(text, error) {
    const el = $("#calendar-status-line");
    el.textContent = text;
    el.classList.toggle("error", !!error);
  }

  function lastUsed() {
    try { return Number(localStorage.getItem(LAST_KEY)) || null; } catch (_) { return null; }
  }

  function remember(id) {
    try { localStorage.setItem(LAST_KEY, String(id)); } catch (_) { /* private mode */ }
  }

  // --- Dates ---
  // meercal's preview names days ("2026-10-14"), not instants. new Date() reads
  // that spelling as midnight UTC, which anywhere west of Greenwich is the
  // evening before, so the parts are taken apart and built into a local date.
  function plainDate(s) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s || ""));
    if (!m) return null;
    const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    return isNaN(d) ? null : d;
  }

  // Long with the weekday for a single day, which is the question an invitation
  // raises ("which Tuesday?"). Short for the two ends of a range, so the pair
  // fits on one line of the dialog.
  function fmtDay(s, long) {
    const d = plainDate(s);
    if (!d) return String(s || "");
    return d.toLocaleDateString([], long
      ? { weekday: "long", year: "numeric", month: "long", day: "numeric" }
      : { year: "numeric", month: "short", day: "numeric" });
  }

  // --- The dialog ---

  async function open(m, a) {
    if (!m || !a || !configured) return;
    clearTimeout(closeTimer);
    closeTimer = null;
    const mine = ++seq;
    current = { m, a };
    calendars = [];
    $("#calendar-modal").hidden = false;
    $("#calendar-import").disabled = true;
    setStatus("");
    $("#calendar-body").innerHTML = `<div class="task-loading">Loading…</div>`;
    let opts;
    try {
      // Asked of meercal each time rather than cached here. A calendar made
      // over there a minute ago belongs in the list without a reload, and the
      // preview is meercal's reading of the file, which is the one that counts.
      opts = await App.api.calendarImportOptions(a.id);
    } catch (e) {
      if (mine !== seq) return;
      $("#calendar-body").innerHTML = "";
      setStatus(e.message || "Could not reach meercal", true);
      return;
    }
    if (mine !== seq) return;
    render(opts || {});
  }

  function render(opts) {
    calendars = Array.isArray(opts.calendars) ? opts.calendars : [];
    const name = opts.filename || (current && current.a.filename) || "";
    let html = preview(name, opts.preview || {});
    if (!calendars.length) {
      // Read-only calendars (subscriptions, shared ones) are left out by the
      // server, so this is a meercal with nothing an import can go into. The
      // button stays off rather than offering a choice that can only fail.
      html += `<p class="cal-empty">meercal has no calendar you can add events to.</p>`;
      $("#calendar-body").innerHTML = html;
      return;
    }
    const last = lastUsed();
    const chosen = calendars.some((c) => c.id === last) ? last : calendars[0].id;
    html += `<label class="task-field">
        <span>Calendar</span>
        <span class="cal-pick">
          <i class="cal-swatch" id="calendar-swatch" aria-hidden="true"></i>
          <select id="calendar-target">${calendarOptions(chosen)}</select>
        </span>
      </label>`;
    $("#calendar-body").innerHTML = html;
    paintSwatch();
    $("#calendar-import").disabled = false;
    // The keyboard goes into the dialog. Left on the strip behind it, a
    // shortcut letter would act on the conversation while the dialog is up,
    // and a select counts as typing, so the letters stay out of the way.
    $("#calendar-target").focus();
  }

  // What meercal read in the file. Titles and dates are the sender's text, so
  // every one of them goes through App.esc.
  function preview(name, p) {
    const events = Number(p.events) || 0;
    const recurring = Number(p.recurring) || 0;
    const outside = Number(p.outside_horizon) || 0;
    const titles = Array.isArray(p.titles) ? p.titles : [];
    const parts = [];

    if (name) {
      parts.push(`<div class="cal-file">${App.icon("calendar", 14)}
        <span class="cal-file-name">${App.esc(name)}</span></div>`);
    }
    if (titles.length) {
      const more = events > titles.length
        ? `<li class="cal-more">and ${events - titles.length} more</li>` : "";
      parts.push(`<ul class="cal-titles">${
        titles.map((t) => `<li>${App.esc(t || "(no title)")}</li>`).join("")}${more}</ul>`);
    }

    let when = "";
    if (p.first && p.last && p.first !== p.last) when = `${fmtDay(p.first)} to ${fmtDay(p.last)}`;
    else if (p.first || p.last) when = fmtDay(p.first || p.last, true);
    // Only said of a single event. With several, "all day" would describe
    // some of them and not others.
    if (when && events === 1 && Number(p.all_day) === 1) when += ", all day";
    if (when) parts.push(`<div class="cal-when">${App.esc(when)}</div>`);

    const facts = [];
    if (events > 1) facts.push(`The file holds ${events} events.`);
    if (recurring > 0) {
      if (events <= 1) facts.push("It repeats.");
      else if (recurring >= events) facts.push("They all repeat.");
      else if (recurring === 1) facts.push("One of them repeats.");
      else facts.push(`${recurring} of them repeat.`);
    }
    if (facts.length) parts.push(`<p class="cal-facts">${App.esc(facts.join(" "))}</p>`);

    // meercal keeps an event it cannot show, and says nothing about it in its
    // own views. Without this note an import of last year's booking looks like
    // it went nowhere.
    if (outside > 0) {
      const h = p.horizon || {};
      const span = h.from && h.to ? ` (${fmtDay(h.from)} to ${fmtDay(h.to)})` : "";
      const note = events <= 1
        ? `This event is outside the dates meercal shows${span}. meercal stores it but does not draw it.`
        : `${outside === events ? "All of these events are" : `${outside} of these events are`}` +
          ` outside the dates meercal shows${span}. meercal stores them but does not draw them.`;
      parts.push(`<p class="cal-note">${App.esc(note)}</p>`);
    }
    return parts.length ? `<div class="cal-preview">${parts.join("")}</div>` : "";
  }

  // Grouped by account only when there is more than one. Two calendars called
  // "Personal" are told apart by the account they belong to, which is also
  // how meercal's own sidebar groups them. With a single account the group
  // heading would only repeat itself.
  function calendarOptions(chosen) {
    const opt = (c) => `<option value="${App.esc(c.id)}"${c.id === chosen ? " selected" : ""}
      >${App.esc(c.name)}</option>`;
    const accounts = [];
    calendars.forEach((c) => {
      const k = c.account || "";
      if (!accounts.includes(k)) accounts.push(k);
    });
    if (accounts.length < 2) return calendars.map(opt).join("");
    return accounts.map((acc) => `<optgroup label="${App.esc(acc || "Other")}">${
      calendars.filter((c) => (c.account || "") === acc).map(opt).join("")}</optgroup>`).join("");
  }

  // A dot in the chosen calendar's colour beside the menu. A native select
  // cannot colour its options, and the colour is how most people recognise
  // their calendars in meercal.
  function paintSwatch() {
    const sel = $("#calendar-target");
    const sw = $("#calendar-swatch");
    if (!sel || !sw) return;
    const cal = calendars.find((c) => String(c.id) === sel.value);
    const color = cal && COLOR_RE.test(cal.color || "") ? cal.color : "";
    sw.style.backgroundColor = color || "transparent";
  }

  function resultText(res) {
    const name = (res.calendar && res.calendar.name) || "the calendar";
    const created = Number(res.created) || 0;
    const updated = Number(res.updated) || 0;
    let text;
    if (created && updated) text = `Added ${created} and updated ${updated} events in ${name}.`;
    else if (updated === 1) text = `It was already in ${name}. Updated it.`;
    else if (updated) text = `They were already in ${name}. Updated ${updated} events.`;
    else if (created > 1) text = `Added ${created} events to ${name}.`;
    else if (created) text = `Added to ${name}.`;
    // Neither number moved. Saying "Added" would claim an event nobody can find.
    else text = `meercal added no event to ${name}.`;
    // The event is in meercal already, but a CalDAV-backed calendar only
    // reaches the phone once meercal's agent has pushed it. Said, so an event
    // missing from the phone for a few minutes does not read as a failure.
    if (Number(res.queued) > 0) text += " meercal will copy it to the calendar server on its next sync.";
    return text;
  }

  async function importIt() {
    if (!current) return;
    const sel = $("#calendar-target");
    if (!sel || !sel.value) return;
    const calendarId = Number(sel.value);
    const mine = seq;
    const btn = $("#calendar-import");
    btn.disabled = true;
    sel.disabled = true;
    setStatus("Adding…");
    let res;
    try {
      res = await App.api.importCalendar(current.a.id, calendarId);
    } catch (e) {
      if (mine !== seq) return;
      // meercal upserts by the event's UID, so pressing the button again after
      // any failure (a timeout included) cannot add the event twice.
      setStatus(e.message || "Could not add it to meercal", true);
      btn.disabled = false;
      sel.disabled = false;
      return;
    }
    // Remembered even when the dialog has moved on. The import happened, and
    // the next one should offer the same calendar either way.
    remember(calendarId);
    if (mine !== seq) return;
    // The button stays off. A second press in the moment before the dialog
    // closes would only import the same file again.
    setStatus(resultText(res || {}));
    const wait = res && Number(res.queued) > 0 ? CLOSE_AFTER_QUEUED_MS : CLOSE_AFTER_MS;
    closeTimer = setTimeout(() => { if (mine === seq) close(); }, wait);
  }

  return { init, open, close, isOpen, enabled, refreshConfig };
})();
