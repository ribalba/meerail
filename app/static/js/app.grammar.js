/* meerail grammar: spelling and grammar underlines in the composer.

   The checking is done by a LanguageTool server that this install runs on its
   own network ([grammar] in meerail.toml; see app/grammar.py). The browser
   never talks to it: every check goes to /api/grammar/check on meerail's own
   server, which is the only thing that knows where the checker is and which
   refuses to send a draft to a public address unless the operator said so.

   What is sent is the prose the user wrote and nothing else. The draft is cut
   into paragraphs (App.markdown.proseRanges), and quoted lines, code, the
   footer the composer prefilled and the quote or forwarded original it opened
   with (App.compose.notYours) are left out, as is anything below a "-- "
   signature line. Those are somebody else's words, or not words at all, and
   checked they only bury the one typo that matters under a page of red.

   Underlines are drawn with the CSS Custom Highlight API: a Range per match,
   registered under a name the stylesheet gives a wavy underline. Nothing is
   inserted into the editor, which matters twice over here: the markdown editor
   rewrites a line's HTML on every keystroke, so a <span> of ours would not
   survive the next letter, and its textContent has to stay exactly the text
   that is sent. A browser without the API gets no underlines, but the count,
   the suggestions and Alt+Shift+G all still work.

   Offsets are the currency throughout, in UTF-16 units like every JS string
   (and like LanguageTool, which is Java). Matches live as [start, end) into the
   draft; typing moves them along (carry) until the next check replaces them,
   and they are turned into Ranges again after every repaint. */

App.grammar = (function () {
  const $ = (s) => document.querySelector(s);

  // Quiet after the last keystroke before the draft is checked. Long enough
  // not to check a word half typed, short enough that the underline turns up
  // while the sentence is still in mind.
  const CHECK_AFTER_MS = 700;
  // A draft that opens with text in it (a restored one, an AI reply) is checked
  // almost at once rather than after a pause nobody is making.
  const OPEN_AFTER_MS = 150;
  // Below the server's own caps (60000 characters, 500 segments), so a very
  // long draft is checked from the top as far as it goes instead of refused.
  const MAX_CHARS = 50000;
  const MAX_SEGMENTS = 450;
  const MAX_SUGGESTIONS = 6;

  // Stretches inside a paragraph that are not language: an inline code span,
  // a link's address, a bare URL, an email address. LanguageTool skips URLs by
  // itself; the code span it would read as a misspelt word.
  const MASKS = [
    /`[^`\n]+`/g,
    /\]\([^)\s]+\)/g,
    /\bhttps?:\/\/[^\s<>]+/g,
    /[^\s@<>()"']+@[^\s@<>()"']+\.[A-Za-z]{2,}/g,
  ];

  const HL = (typeof Highlight === "function" && window.CSS && CSS.highlights)
    ? { spelling: new Highlight(), grammar: new Highlight(), style: new Highlight(), active: new Highlight() }
    : null;

  let config = null;        // GET /api/grammar/config; null until it answers
  let editor = null;        // the composer's App.markdown editor
  let el = null;            // #compose-body
  let text = "";            // the draft as `matches` knows it
  let matches = [];         // [{start, end, text, type, rule, ...}] sorted by start
  let state = "idle";       // idle (no answer yet) | ok | error
  let errorText = "";
  let language = "auto";    // this draft's language; the setting's until changed
  let detected = null;      // {code, name, auto} the checker last answered with
  let ignored = new Set();  // rule + text taken back with Ignore, for this draft only
  let timer = null;
  let inflight = null;      // AbortController of the check on the wire
  let seq = 0;              // drops an answer a newer check has overtaken
  let pop = null;           // {m, sugs, caret} while the suggestions are up
  let languages = null;     // [{code, name}] from the checker, once fetched
  let languagesLoading = false;

  function enabled() {
    return !!(config && config.available && config.enabled && editor);
  }

  // --- Following the text -------------------------------------------------

  // Move matches from `before` onto `after`. One edit separates them (the
  // editor reports every change), found as the longest common prefix and
  // suffix. Matches clear of it slide along; a match the edit touches is
  // dropped, since its words changed and the verdict may have too, and the
  // next check brings it back if it still stands.
  //
  // Touching includes typing right up against a word, so that finishing "teh"
  // into "tehe" takes the old underline away at once. The exception is a space
  // or a newline typed next to a word with nothing deleted: that leaves the
  // word exactly as it was, and dropping its underline for the length of a
  // check would make every flagged word blink off as the sentence carries on.
  function carry(list, before, after) {
    if (!list.length || before === after) return list;
    const max = Math.min(before.length, after.length);
    let p = 0;
    while (p < max && before.charCodeAt(p) === after.charCodeAt(p)) p++;
    let s = 0;
    while (s < max - p
           && before.charCodeAt(before.length - 1 - s) === after.charCodeAt(after.length - 1 - s)) s++;
    const cut = before.length - s;             // [p, cut) of before became [p, after.length - s) of after
    const typed = after.slice(p, after.length - s);
    const delta = after.length - before.length;
    const inserted = cut === p;
    const keepLeft = inserted && /^\s/.test(typed);
    const keepRight = inserted && /\s$/.test(typed);
    const out = [];
    for (const m of list) {
      if (m.end < p || (keepLeft && m.end === p)) out.push(m);
      else if (m.start > cut || (keepRight && m.start === cut)) {
        out.push({ ...m, start: m.start + delta, end: m.end + delta });
      }
    }
    return out;
  }

  // Called by the editor after every repaint, which is also after every change.
  function onRender() {
    if (!enabled()) return;
    const now = editor.getText();
    if (now !== text) {
      matches = carry(matches, text, now);
      text = now;
      closePop();
      schedule(CHECK_AFTER_MS);
      renderStatus();
    }
    paint();
  }

  // --- What is sent -------------------------------------------------------

  // [a, b) with every range in `cuts` taken out of it.
  function subtract(a, b, cuts) {
    let parts = [[a, b]];
    for (const [c, d] of cuts) {
      const next = [];
      for (const [s, e] of parts) {
        if (d <= s || c >= e) { next.push([s, e]); continue; }
        if (c > s) next.push([s, c]);
        if (d < e) next.push([d, e]);
      }
      parts = next;
    }
    return parts;
  }

  // The draft as the list of pieces the checker gets: [{start, text}], in order.
  function segments(src) {
    const skip = App.compose.notYours(src).slice();
    // RFC 3676's signature delimiter: what follows is a signature the user
    // typed or pasted by hand, which is no more prose than the prefilled one.
    const sig = /^-- ?$/m.exec(src);
    if (sig) skip.push([sig.index, src.length]);
    const out = [];
    let total = 0;
    for (const [a, b] of App.markdown.proseRanges(src)) {
      for (const [s, e] of subtract(a, b, skip)) {
        const piece = src.slice(s, e);
        if (!piece.trim()) continue;
        if (total + piece.length > MAX_CHARS || out.length >= MAX_SEGMENTS) return out;
        total += piece.length;
        out.push({ start: s, text: piece });
      }
    }
    return out;
  }

  function masked(src) {
    const out = [];
    for (const re of MASKS) {
      for (const m of src.matchAll(re)) out.push([m.index, m.index + m[0].length]);
    }
    return out;
  }

  // --- Checking -----------------------------------------------------------

  function schedule(ms) {
    clearTimeout(timer);
    timer = setTimeout(check, ms);
  }

  function abort() {
    seq++;
    if (inflight) inflight.abort();
    inflight = null;
    busy(false);
  }

  const ignoreKey = (rule, words) => `${rule} ${words}`;

  async function check() {
    timer = null;
    if (!enabled() || !App.compose.isOpen()) return;
    const sent = editor.getText();
    const segs = segments(sent);
    abort();
    if (!segs.length) {
      matches = [];
      state = "ok";
      paint();
      renderStatus();
      return;
    }
    const ac = new AbortController();
    const my = seq;
    inflight = ac;
    busy(true);
    let res;
    try {
      res = await App.api.grammarCheck({ segments: segs.map((s) => s.text), language }, ac.signal);
    } catch (e) {
      if (e.name === "AbortError" || my !== seq) return;
      inflight = null;
      busy(false);
      state = "error";
      errorText = e.message || "The grammar checker did not answer.";
      renderStatus();
      // Turned off, or taken away, since this page asked: another tab saved
      // Settings, or the server was restarted without a checker. Ask again
      // rather than keep checking into a refusal.
      if (e.status === 409) loadConfig();
      return;
    }
    if (my !== seq) return;
    inflight = null;
    busy(false);
    detected = res.language || null;
    const masks = masked(sent);
    const list = [];
    (res.matches || []).forEach((found, i) => {
      const seg = segs[i];
      if (!seg || !Array.isArray(found)) return;
      for (const m of found) {
        const start = seg.start + m.offset;
        const end = start + m.length;
        if (!(m.length > 0) || end > seg.start + seg.text.length) continue;
        if (masks.some(([a, b]) => start < b && end > a)) continue;
        const words = sent.slice(start, end);
        if (ignored.has(ignoreKey(m.rule, words))) continue;
        list.push({ ...m, start, end, text: words });
      }
    });
    list.sort((a, b) => a.start - b.start);
    // Typing carried on while the check was out: the answer is about the text
    // as it was sent, so it is moved onto the text as it is now, the same way
    // the underlines already on screen were.
    const now = editor.getText();
    matches = carry(list, sent, now);
    text = now;
    state = "ok";
    autoLabel();
    paint();
    renderStatus();
  }

  // --- Drawing ------------------------------------------------------------

  function paint() {
    if (!HL) return;
    for (const h of Object.values(HL)) h.clear();
    if (!enabled()) return;
    for (const m of matches) {
      const r = editor.rangeFor(m.start, m.end);
      if (r) (HL[m.type] || HL.grammar).add(r);
    }
    if (pop) {
      const r = editor.rangeFor(pop.m.start, pop.m.end);
      if (r) HL.active.add(r);
    }
  }

  function busy(on) {
    const btn = $("#compose-grammar-next");
    if (btn) btn.classList.toggle("busy", on);
    if (on && state === "idle") renderStatus();
  }

  function renderStatus() {
    const btn = $("#compose-grammar-next");
    if (!btn) return;
    const n = matches.length;
    btn.classList.toggle("has-issues", state === "ok" && n > 0);
    btn.classList.toggle("is-error", state === "error");
    if (state === "error") {
      btn.textContent = "Checker unavailable";
      btn.title = `${errorText} Nothing about the draft is affected; it is checked again after the next change.`;
    } else if (state === "idle") {
      btn.textContent = "Checking…";
      btn.title = "Spelling and grammar, checked by the LanguageTool server this install runs.";
    } else {
      btn.textContent = n === 0 ? "No issues" : n === 1 ? "1 issue" : `${n} issues`;
      btn.title = n
        ? "Spelling and grammar. Click, or press Alt+Shift+G, for the next one after the caret."
        : "Spelling and grammar: nothing flagged in what you wrote.";
    }
    // The composer's key strip: the hint only means something with an
    // underline on screen to walk to.
    document.querySelectorAll('#compose-keys [data-ck="grammar"]').forEach((k) =>
      k.classList.toggle("off", !(state === "ok" && n > 0)));
  }

  // --- The suggestions ----------------------------------------------------

  function matchAt(off) {
    let best = null;
    for (const m of matches) {
      if (m.start <= off && off <= m.end && (!best || m.end - m.start < best.end - best.start)) best = m;
    }
    return best;
  }

  function place(box, m) {
    const r = editor.rangeFor(m.start, m.end);
    const rects = r ? r.getClientRects() : [];
    const at = rects.length ? rects[0] : el.getBoundingClientRect();
    const w = box.offsetWidth;
    const h = box.offsetHeight;
    const left = Math.max(8, Math.min(at.left, window.innerWidth - w - 8));
    let top = at.bottom + 6;
    if (top + h > window.innerHeight - 8) top = Math.max(8, at.top - h - 6);
    box.style.left = `${left}px`;
    box.style.top = `${top}px`;
  }

  // Scroll the editor so the match is on screen before anything is drawn
  // against it; Alt+Shift+G can land on one far below the fold.
  function reveal(m) {
    const r = editor.rangeFor(m.start, m.start);
    const node = r && r.startContainer;
    const host = node && (node.nodeType === 3 ? node.parentElement : node);
    const line = host && host.closest(".md-line");
    if (line) line.scrollIntoView({ block: "nearest" });
  }

  function openPop(m, withFocus) {
    const box = $("#grammar-pop");
    const sugs = (m.replacements || []).slice(0, MAX_SUGGESTIONS);
    const rule = m.rule_description || m.rule;
    // A replacement that is empty means "take these words out", which is what
    // LanguageTool suggests for a word typed twice.
    box.innerHTML = `
      <div class="gp-msg">${App.esc(m.message || m.short || "Possible mistake")}</div>
      ${sugs.length ? `<div class="gp-sugs">${sugs.map((s, i) =>
        `<button type="button" class="gp-sug" data-i="${i}">${s ? App.esc(s) : "<em>remove</em>"}</button>`).join("")}</div>` : ""}
      <div class="gp-acts">
        <button type="button" class="gp-act" data-act="ignore"
                title="Stop flagging this here, for this message only">Ignore</button>
        ${m.type === "spelling" ? `<button type="button" class="gp-act" data-act="word"
                title="Never flag “${App.esc(m.text)}” as misspelt again, in any message">Add to dictionary</button>` : ""}
        <button type="button" class="gp-act" data-act="rule"
                title="${App.esc(`Turn off “${rule}” in every message. Settings lists what is turned off.`)}">Turn off this rule</button>
      </div>`;
    pop = { m, sugs, caret: editor.caret() };
    box.hidden = false;
    place(box, m);
    paint();
    if (withFocus) {
      const first = box.querySelector("button");
      if (first) first.focus();
    }
  }

  function closePop() {
    if (!pop) return;
    pop = null;
    const box = $("#grammar-pop");
    box.hidden = true;
    box.innerHTML = "";
    paint();
  }

  // Back to the editor, where the caret was when the suggestions came up.
  function back() {
    const caret = pop ? pop.caret : -1;
    closePop();
    el.focus({ preventScroll: true });
    if (caret >= 0) editor.setCaret(caret);
  }

  function apply(m, replacement) {
    const cur = editor.getText();
    closePop();
    // The words moved or changed since the suggestion was drawn (another tab's
    // draft sync, an undo in between): nothing here is about them any more.
    if (cur.slice(m.start, m.end) !== m.text) { schedule(0); return; }
    el.focus({ preventScroll: true });
    // One undoable edit, so Ctrl-Z takes a suggestion back like anything typed.
    editor.replaceText(cur.slice(0, m.start) + replacement + cur.slice(m.end), m.start + replacement.length);
    // replaceText repaints without an input event, and the input event is how
    // the composer learns that a draft has changed and needs saving.
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function forget(what, m) {
    if (what === "word") {
      const w = m.text.toLocaleLowerCase();
      matches = matches.filter((x) => !(x.type === "spelling" && x.text.toLocaleLowerCase() === w));
    } else if (what === "rule") {
      matches = matches.filter((x) => x.rule !== m.rule);
    } else {
      ignored.add(ignoreKey(m.rule, m.text));
      matches = matches.filter((x) => ignoreKey(x.rule, x.text) !== ignoreKey(m.rule, m.text));
    }
    paint();
    renderStatus();
  }

  // Ignore is this draft's alone and costs nothing. The other two are kept on
  // the server (the dictionary and the list of turned-off rules in Settings),
  // so they hold in every message and every browser from here on. They come
  // off the screen at once rather than when the server answers.
  async function act(what, m) {
    back();
    forget(what, m);
    if (what === "ignore") return;
    try {
      config = await App.api.grammarIgnore(what === "word" ? { word: m.text } : { rule: m.rule });
    } catch (e) {
      $("#compose-status").textContent = e.message || "Could not save that.";
    }
  }

  // Alt+Shift+G, and the count button: the next underline after the caret (or
  // after the one whose suggestions are up), round to the first after the last.
  function next() {
    if (!enabled() || !matches.length) return false;
    const from = pop ? pop.m.start : editor.caret();
    const m = matches.find((x) => x.start > from) || matches[0];
    closePop();
    el.focus({ preventScroll: true });
    editor.setCaret(m.end);
    reveal(m);
    openPop(m, true);
    return true;
  }

  function onPopKey(e) {
    if (!pop) return;
    if (e.ctrlKey || e.metaKey) return;          // Ctrl/Cmd+Enter still sends
    if (e.altKey && e.shiftKey && e.code === "KeyG") {
      e.preventDefault();
      e.stopPropagation();
      next();
      return;
    }
    // Nothing else pressed in here belongs to the composer or to the
    // shortcuts behind it: Escape would minimize the draft, Tab would walk off
    // into the window's own ring, and Enter on a focused button is taken by
    // app.keys.js for "open what is selected" before the browser can click it.
    e.stopPropagation();
    const btns = Array.from($("#grammar-pop").querySelectorAll("button"));
    const i = btns.indexOf(document.activeElement);
    const step = (d) => btns[(i + d + btns.length) % btns.length].focus();
    if (e.key === "Escape") { e.preventDefault(); back(); }
    else if (e.key === "ArrowRight" || e.key === "ArrowDown" || (e.key === "Tab" && !e.shiftKey)) {
      e.preventDefault();
      step(1);
    } else if (e.key === "ArrowLeft" || e.key === "ArrowUp" || (e.key === "Tab" && e.shiftKey)) {
      e.preventDefault();
      step(-1);
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      if (i >= 0) btns[i].click();
    } else if (e.key.length === 1) {
      // Carried on typing instead of choosing. Focus moves during the keydown,
      // so the character lands in the draft, which is where it was going.
      back();
    }
  }

  // --- Language -----------------------------------------------------------

  async function ensureLanguages() {
    if (languages || languagesLoading) return;
    languagesLoading = true;
    try {
      languages = (await App.api.grammarLanguages()).languages || [];
    } catch (_) {
      languages = null;                 // the checker is down: try again next draft
    } finally {
      languagesLoading = false;
    }
    if (languages) {
      renderLangSelect();
      renderSettingsLanguages();
    }
  }

  function languageOptions(current, autoLabel) {
    const list = (languages || []).slice();
    if (current && current !== "auto" && !list.some((l) => l.code === current)) {
      list.unshift({ code: current, name: current });
    }
    return `<option value="auto">${App.esc(autoLabel)}</option>` +
      list.map((l) => `<option value="${App.esc(l.code)}">${App.esc(l.name)}</option>`).join("");
  }

  function renderLangSelect() {
    const sel = $("#compose-grammar-lang");
    sel.innerHTML = languageOptions(language, "Auto");
    sel.value = language;
    autoLabel();
  }

  // Only the first option's text changes, so a list the user has open is not
  // rebuilt out from under them by a check landing.
  function autoLabel() {
    const first = $("#compose-grammar-lang").options[0];
    if (!first) return;
    first.textContent = detected && detected.auto && detected.name ? `Auto: ${detected.name}` : "Auto";
  }

  // --- A draft comes and goes --------------------------------------------

  // From App.compose's show(): a draft is on screen, its text already in the
  // editor. The language goes back to the setting for every draft; the one
  // picked for the last message was about that message.
  function onOpen() {
    closePop();
    clearTimeout(timer);
    abort();
    matches = [];
    ignored = new Set();
    detected = null;
    state = "idle";
    text = editor ? editor.getText() : "";
    if (!enabled()) { paint(); return; }
    language = config.language || "auto";
    renderLangSelect();
    ensureLanguages();
    paint();
    renderStatus();
    schedule(OPEN_AFTER_MS);
  }

  // From close() and minimize(): nothing is on screen to check.
  function onHide() {
    clearTimeout(timer);
    abort();
    closePop();
    matches = [];
    paint();
  }

  // --- Configuration ------------------------------------------------------

  async function loadConfig() {
    try { config = await App.api.grammarConfig(); } catch (_) { config = null; }
    applyConfig();
  }

  function applyConfig() {
    const on = enabled();
    $("#compose-grammar").hidden = !on;
    // The browser's own checker would underline the same words a second time,
    // in its own colour and with its own idea of the language. With this one
    // off, it is the only one there is, so it comes back.
    if (el) el.spellcheck = !on;
    document.querySelectorAll('#compose-keys [data-ck="grammar"]').forEach((k) => { k.hidden = !on; });
    if (App.compose.isOpen()) onOpen(); else onHide();
  }

  function renderSettingsLanguages() {
    const sel = $("#grammar-language");
    const current = config ? config.language : "auto";
    sel.innerHTML = languageOptions(current, "Detect from the text");
    sel.value = current || "auto";
  }

  function fillSettings() {
    const available = !!(config && config.available);
    $("#grammar-settings-form").hidden = !available;
    $("#grammar-settings-off").hidden = available;
    if (!available) return;
    $("#grammar-enabled").checked = !!config.enabled;
    renderSettingsLanguages();
    $("#grammar-variants").value = (config.variants || []).join(", ");
    $("#grammar-picky").checked = !!config.picky;
    $("#grammar-words").value = (config.words || []).join("\n");
    $("#grammar-rules").value = (config.disabled_rules || []).join("\n");
  }

  async function onSettingsOpen() {
    $("#grammar-save-status").textContent = "";
    await loadConfig();
    fillSettings();
    if (config && config.available) ensureLanguages();
  }

  async function saveSettings() {
    const status = $("#grammar-save-status");
    status.classList.remove("error");
    const lines = (v) => v.split("\n").map((x) => x.trim()).filter(Boolean);
    const payload = {
      enabled: $("#grammar-enabled").checked,
      language: $("#grammar-language").value,
      variants: $("#grammar-variants").value.split(/[\s,]+/).filter(Boolean),
      picky: $("#grammar-picky").checked,
      words: lines($("#grammar-words").value),
      disabled_rules: lines($("#grammar-rules").value),
    };
    status.textContent = "Saving…";
    try {
      config = await App.api.saveGrammarConfig(payload);
    } catch (e) {
      status.textContent = e.message || "Could not save";
      status.classList.add("error");
      return;
    }
    status.textContent = "Saved";
    setTimeout(() => { status.textContent = ""; }, 2500);
    fillSettings();
    applyConfig();
  }

  // --- Wiring -------------------------------------------------------------

  function wire() {
    const box = $("#grammar-pop");
    // Clicking a suggestion must not take the caret out of the draft first:
    // mousedown is where focus moves, and the click still arrives.
    box.addEventListener("mousedown", (e) => e.preventDefault());
    box.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b || !pop) return;
      const m = pop.m;
      if (b.dataset.i != null) apply(m, pop.sugs[Number(b.dataset.i)]);
      else act(b.dataset.act, m);
    });
    box.addEventListener("keydown", onPopKey);

    // A click on an underlined word brings its suggestions up, the way every
    // word processor does it. A drag that selects text is not that click.
    el.addEventListener("click", () => {
      if (!enabled()) return;
      const sel = window.getSelection();
      if (!sel || !sel.isCollapsed) { closePop(); return; }
      const m = matchAt(editor.caret());
      if (m) openPop(m, false); else closePop();
    });
    el.addEventListener("keydown", (e) => {
      if (!pop) return;
      // Escape with the suggestions up takes them away and nothing else. Left
      // to app.keys.js it would minimize the draft.
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closePop(); return; }
      if (!["Shift", "Alt", "Control", "Meta", "AltGraph"].includes(e.key)) closePop();
    });
    el.addEventListener("scroll", closePop);
    window.addEventListener("resize", closePop);
    document.addEventListener("mousedown", (e) => {
      if (pop && !box.contains(e.target) && !el.contains(e.target)) closePop();
    });

    $("#compose-grammar-next").addEventListener("click", () => {
      if (state === "error") { schedule(0); return; }     // the button is also "try again"
      next();
    });
    const sel = $("#compose-grammar-lang");
    // Stopped here: the composer counts any input or change inside its window
    // as writing in the draft, and choosing a language to check in is not.
    ["input", "change"].forEach((t) => sel.addEventListener(t, (e) => {
      e.stopPropagation();
      if (t !== "change") return;
      language = sel.value || "auto";
      detected = null;
      autoLabel();
      schedule(0);
    }));

    $("#grammar-save").addEventListener("click", saveSettings);
  }

  async function init() {
    editor = App.compose.editor();
    el = $("#compose-body");
    if (!editor || !el) return;
    editor.onRender(onRender);
    if (HL) for (const [name, h] of Object.entries(HL)) CSS.highlights.set(`lt-${name}`, h);
    wire();
    await loadConfig();
  }

  return { init, next, onOpen, onHide, onSettingsOpen };
})();
