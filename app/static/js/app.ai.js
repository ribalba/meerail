/* meerail AI: the two robot buttons, and the settings behind them.

   Both go through /api/ai/* — the browser never talks to Anthropic, OpenAI or
   whatever else is configured, and the API key never reaches this file (see
   app/routers/ai.py for why). The buttons only exist once a model is saved, so
   `enabled` is fetched at boot and re-read whenever Settings saves, the same way
   App.tasks handles the Meerato URL. */

App.ai = (function () {
  const $ = (s) => document.querySelector(s);

  let cfg = { enabled: false, providers: [], presets: {}, keys: {}, examples: [] };
  let written = null;       // the last {query, mode} the search writer produced
  let threadMsg = null;     // the message whose conversation the dialog is asking about
  let answer = "";          // the last answer, for the buttons that put it in a mail
  let attachment = null;    // the attachment the "what is this?" dialog is on
  let attAnswer = "";
  let asking = false;       // one request at a time, per dialog

  // The four one-click instructions, in the order the dialog offers them. The
  // text of each lives on the server (app/aiprompts.py) so that the prompt and
  // the button cannot drift apart; these are only the labels.
  const PRESET_LABELS = [
    ["summary", "Summarise"],
    ["actions", "What do I need to do?"],
    ["reply", "Draft a reply"],
    ["explain", "Explain it to me"],
  ];

  async function init() {
    await refreshConfig();

    $("#search-ai-btn").innerHTML = App.icon("robot", 15);
    $("#search-ai-btn").addEventListener("click", openSearch);

    $("#btn-close-ai-search").innerHTML = App.icon("close", 18);
    $("#btn-close-ai-search").addEventListener("click", closeSearch);
    $("#ai-search-modal").addEventListener("click", (e) => {
      if (e.target.id === "ai-search-modal") closeSearch();
    });
    $("#ai-search-go").addEventListener("click", writeQuery);
    $("#ai-search-use").addEventListener("click", () => applyQuery(true));
    $("#ai-search-edit").addEventListener("click", () => applyQuery(false));
    // Enter alone is a newline — the box is for a sentence or two, and a
    // description that lost its second line to a stray Return would be worse
    // than one extra keystroke. Cmd/Ctrl+Enter is the send, as in the composer.
    $("#ai-search-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); writeQuery(); }
    });

    $("#btn-close-ai-thread").innerHTML = App.icon("close", 18);
    $("#btn-close-ai-thread").addEventListener("click", closeThread);
    $("#ai-thread-modal").addEventListener("click", (e) => {
      if (e.target.id === "ai-thread-modal") closeThread();
    });
    $("#ai-thread-go").addEventListener("click", () => askThread(""));
    $("#ai-thread-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); askThread(""); }
    });
    $("#ai-thread-presets").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-preset]");
      if (btn) askThread(btn.dataset.preset);
    });
    $("#ai-thread-reply").addEventListener("click", () => intoMail("reply"));
    $("#ai-thread-new").addEventListener("click", () => intoMail("new"));
    $("#ai-thread-copy").addEventListener("click", () => copy(answer, "#ai-thread-done"));

    $("#btn-close-ai-att").innerHTML = App.icon("close", 18);
    $("#btn-close-ai-att").addEventListener("click", closeAttachment);
    $("#ai-att-modal").addEventListener("click", (e) => {
      if (e.target.id === "ai-att-modal") closeAttachment();
    });
    $("#ai-att-go").addEventListener("click", askAttachment);
    $("#ai-att-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); askAttachment(); }
    });
    $("#ai-att-copy").addEventListener("click", () => copy(attAnswer, "#ai-att-done"));

    wireSettings();
  }

  // The robot buttons appear and disappear with the setting, so the reader's bar
  // is redrawn on every change rather than only when a thread opens.
  async function refreshConfig() {
    let next = { enabled: false, providers: [], presets: {}, keys: {}, examples: [] };
    try { next = await App.api.aiConfig(); } catch (_) {}
    const was = cfg.enabled;
    cfg = next;
    $("#search-ai-btn").hidden = !cfg.enabled;
    if (was !== cfg.enabled && App.reader) App.reader.redraw();
    return cfg;
  }

  function enabled() { return !!cfg.enabled; }

  function setStatus(id, text, isError) {
    const el = $(id);
    el.textContent = text || "";
    el.classList.toggle("error", !!isError);
  }

  // --- Feature 1: describe a search, get a query --------------------------

  function searchOpen() { return !$("#ai-search-modal").hidden; }

  function openSearch() {
    if (!cfg.enabled) return;
    $("#ai-search-modal").hidden = false;
    $("#ai-search-result").hidden = true;
    setStatus("#ai-search-status", "");
    // Whatever is already in the search box is a decent starting description —
    // somebody who typed two words and gave up is exactly who presses this.
    const typed = $("#search-input").value.trim();
    const box = $("#ai-search-input");
    if (!box.value.trim() && typed) box.value = typed;
    box.focus();
    box.select();
  }

  function closeSearch() { $("#ai-search-modal").hidden = true; }

  async function writeQuery() {
    if (asking) return;
    const description = $("#ai-search-input").value.trim();
    if (!description) { $("#ai-search-input").focus(); return; }
    asking = true;
    written = null;
    $("#ai-search-go").disabled = true;
    $("#ai-search-result").hidden = true;
    setStatus("#ai-search-status", "Writing…");
    try {
      const out = await App.api.aiSearch(description);
      written = { query: out.query, mode: out.mode, warning: out.warning || "" };
      // The query is the mode's as much as the text's: a regex pasted into the
      // box with the switch off is a search for those characters literally.
      $("#ai-search-query").textContent =
        out.mode === "regex" ? `${out.query}    (regex)` : out.query;
      $("#ai-search-note").textContent = out.note || "";
      $("#ai-search-warning").textContent = out.warning || "";
      $("#ai-search-warning").hidden = !out.warning;
      $("#ai-search-result").hidden = false;
      setStatus("#ai-search-status", out.model ? `Written by ${out.model}` : "");
      $("#ai-search-use").focus();
    } catch (e) {
      setStatus("#ai-search-status", e.message || "Could not write a query", true);
    } finally {
      asking = false;
      $("#ai-search-go").disabled = false;
    }
  }

  // The query goes into the box either way. That is the point of the feature —
  // the search syntax is worth learning, and it is learnt by seeing it written
  // for something you actually wanted.
  //
  // A query that came back with a warning on it is never run, whichever button
  // was pressed: the warning is "this will not compile", and running it only
  // trades a sentence that says what is wrong for the search's own "the engine
  // rejected that pattern", which does not.
  function applyQuery(run) {
    if (!written || !written.query) return;
    closeSearch();
    App.search.applyQuery(written.query, written.mode, run && !written.warning);
  }

  // --- Feature 2: ask something about a conversation ----------------------

  function threadOpen() { return !$("#ai-thread-modal").hidden; }

  function openThread(m) {
    if (!m || !cfg.enabled) return;
    threadMsg = m;
    answer = "";
    $("#ai-thread-modal").hidden = false;
    $("#ai-thread-result").hidden = true;
    $("#ai-thread-input").value = "";
    setStatus("#ai-thread-status", "");
    setStatus("#ai-thread-done", "");
    $("#ai-thread-title").textContent = m.subject && m.subject !== "(no subject)"
      ? m.subject : "Ask about this conversation";
    const n = (App.reader && App.reader.threadSize && App.reader.threadSize()) || 1;
    // Said before anything is sent, not after: "the whole conversation goes to
    // the provider" is the one thing worth knowing before pressing a button.
    $("#ai-thread-scope").textContent =
      `The whole conversation — ${n} message${n === 1 ? "" : "s"} — will be sent to ` +
      `${cfg.model || "the configured model"} as text.`;
    $("#ai-thread-presets").innerHTML = PRESET_LABELS
      .filter(([key]) => cfg.presets && cfg.presets[key])
      .map(([key, label]) =>
        `<button type="button" class="ai-preset" data-preset="${key}">${App.esc(label)}</button>`)
      .join("");
    $("#ai-thread-input").focus();
  }

  function closeThread() {
    $("#ai-thread-modal").hidden = true;
    threadMsg = null;
  }

  async function askThread(preset) {
    if (asking || !threadMsg) return;
    const instruction = $("#ai-thread-input").value.trim();
    if (!preset && !instruction) {
      // "Ask" with an empty box and no preset picked has no question in it. The
      // presets are right there, so say so rather than guessing at one.
      setStatus("#ai-thread-status", "Pick one of the buttons above, or type what to do.", true);
      return;
    }
    asking = true;
    $("#ai-thread-go").disabled = true;
    setStatus("#ai-thread-status", "Thinking… this can take a while on a long thread.");
    setStatus("#ai-thread-done", "");
    try {
      const out = await App.api.aiThread({
        message_id: threadMsg.id, preset: preset || "", instruction,
      });
      answer = out.text;
      $("#ai-thread-answer").textContent = out.text;
      const notes = [];
      if (out.thread && out.thread.dropped) {
        notes.push(`The ${out.thread.dropped} oldest message` +
          `${out.thread.dropped === 1 ? "" : "s"} did not fit and ` +
          `${out.thread.dropped === 1 ? "was" : "were"} not sent.`);
      }
      if (out.thread && out.thread.shortened) {
        notes.push(`${out.thread.shortened} long message` +
          `${out.thread.shortened === 1 ? " was" : "s were"} cut short.`);
      }
      if (out.truncated) notes.push("The model ran out of room, so the answer stops mid-way.");
      $("#ai-thread-warning").textContent = notes.join(" ");
      $("#ai-thread-warning").hidden = !notes.length;
      $("#ai-thread-result").hidden = false;
      setStatus("#ai-thread-status", out.model ? `Answered by ${out.model}` : "");
    } catch (e) {
      setStatus("#ai-thread-status", e.message || "Could not ask the model", true);
    } finally {
      asking = false;
      $("#ai-thread-go").disabled = false;
    }
  }

  // The answer is text and nothing else — putting it in a message is a second
  // press, and the message it lands in is a draft like any other. Nothing here
  // sends anything.
  async function intoMail(how) {
    if (!answer) return;
    const id = threadMsg && threadMsg.id;
    closeThread();
    await App.compose.openWithBody(answer, how === "reply" ? { messageId: id } : {});
  }

  async function copy(text, statusId) {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setStatus(statusId, "Copied");
      setTimeout(() => setStatus(statusId, ""), 2000);
    } catch (_) {
      setStatus(statusId, "The browser would not let us copy — select it instead.", true);
    }
  }

  // --- Feature 3: when should this come back? -----------------------------

  // Called by the reminder menu, which owns the row this fills in. Returns
  // `{when: Date, reason}` or throws — the menu shows the message either way.
  //
  // The browser's own clock and zone go with the request. Every preset in that
  // menu is computed here for the same reason: "Thursday morning" is a question
  // about this calendar, and the server has none.
  async function suggestReminder(messageId) {
    const now = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    const local = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
      + `T${pad(now.getHours())}:${pad(now.getMinutes())}`;
    let zone = "";
    try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (_) {}

    const out = await App.api.aiRemindSuggest(messageId, local, zone);
    // Parsed as local time, exactly as the menu's own datetime-local field is:
    // the server sent back wall-clock without a zone precisely so this browser
    // is what resolves it.
    const when = new Date(out.when);
    if (isNaN(when)) throw new Error("It did not answer with a usable date.");
    return { when, reason: out.reason || "" };
  }

  // --- Feature 4: what is this attachment? --------------------------------

  function attachmentOpen() { return !$("#ai-att-modal").hidden; }

  function openAttachment(att) {
    if (!att || !cfg.enabled) return;
    attachment = att;
    attAnswer = "";
    $("#ai-att-modal").hidden = false;
    $("#ai-att-result").hidden = true;
    $("#ai-att-input").value = "";
    setStatus("#ai-att-status", "");
    setStatus("#ai-att-done", "");
    $("#ai-att-title").textContent = att.filename || "Attachment";
    // Said before anything is sent, and specific about which of the two it is:
    // a picture goes to the provider as a picture, and that is worth knowing
    // before you press a button on a photo of somebody's passport.
    const image = /^image\//.test(att.content_type || "");
    $("#ai-att-scope").textContent = image
      ? `This image will be sent to ${cfg.model || "the configured model"} to look at.`
      : `The text of this file will be sent to ${cfg.model || "the configured model"}.`;
    $("#ai-att-input").focus();
  }

  function closeAttachment() {
    $("#ai-att-modal").hidden = true;
    attachment = null;
  }

  async function askAttachment() {
    if (asking || !attachment) return;
    asking = true;
    $("#ai-att-go").disabled = true;
    setStatus("#ai-att-status", "Reading it…");
    setStatus("#ai-att-done", "");
    try {
      const out = await App.api.aiAttachment({
        attachment_id: attachment.id,
        instruction: $("#ai-att-input").value.trim(),
      });
      attAnswer = out.text;
      $("#ai-att-answer").textContent = out.text;
      const notes = [];
      if (out.cut) notes.push("The file was too long to send whole — only its first part went.");
      if (out.truncated) notes.push("The model ran out of room, so the answer stops mid-way.");
      $("#ai-att-warning").textContent = notes.join(" ");
      $("#ai-att-warning").hidden = !notes.length;
      $("#ai-att-result").hidden = false;
      setStatus("#ai-att-status", out.model ? `Answered by ${out.model}` : "");
    } catch (e) {
      setStatus("#ai-att-status", e.message || "Could not read that file", true);
    } finally {
      asking = false;
      $("#ai-att-go").disabled = false;
    }
  }

  // --- The Settings section ------------------------------------------------

  function provider() { return $("#ai-provider").value; }

  function providerMeta() {
    return (cfg.providers || []).find((p) => p.id === provider()) || {};
  }

  // A base URL only means anything for the compatible provider, and a key means
  // nothing for a local model that has no auth — so both fields follow the menu
  // rather than sitting there being ignored.
  function syncProviderFields() {
    const meta = providerMeta();
    const custom = !!meta.needs_base;
    $("#ai-base-label").hidden = !custom;
    $("#ai-base-url").hidden = !custom;
    $("#ai-base-examples").hidden = !custom;
    $("#ai-key").placeholder = cfg.keys && cfg.keys[provider()]
      ? "A key is saved — leave blank to keep it"
      : (meta.needs_key ? "Paste the key" : "Usually blank for a local model");
  }

  function wireSettings() {
    $("#ai-provider").addEventListener("change", () => {
      // Switching provider re-points the model list at a different service, so
      // the one on screen is no longer an answer to anything.
      $("#ai-model-select").innerHTML = `<option value="">— fetch the list first —</option>`;
      $("#ai-model").value = "";
      $("#ai-key").value = "";
      syncProviderFields();
    });
    $("#ai-model-select").addEventListener("change", () => {
      if ($("#ai-model-select").value) $("#ai-model").value = $("#ai-model-select").value;
    });
    $("#ai-models-load").addEventListener("click", loadModels);
    $("#ai-save").addEventListener("click", saveSettings);
    $("#ai-clear").addEventListener("click", clearSettings);
  }

  // Called when the Settings modal opens. Never clobbers a field being typed —
  // the modal re-opens on every settings click, and a half-pasted key must
  // survive it.
  async function onSettingsOpen() {
    const typing = document.activeElement;
    const owned = ["#ai-provider", "#ai-base-url", "#ai-key", "#ai-model"].map($);
    if (owned.includes(typing)) return;
    await refreshConfig();
    $("#ai-provider").innerHTML = (cfg.providers || [])
      .map((p) => `<option value="${App.esc(p.id)}">${App.esc(p.label)}</option>`).join("");
    $("#ai-provider").value = cfg.provider || "anthropic";
    $("#ai-base-url").value = cfg.base_url || "";
    $("#ai-model").value = cfg.model || "";
    $("#ai-key").value = "";
    $("#ai-base-examples").innerHTML = "For example: " + (cfg.examples || [])
      .map((e) => `${App.esc(e.label)} <code>${App.esc(e.url)}</code>`).join(", ");
    // A saved model belongs in the menu even before the list is fetched, or the
    // select sits there saying "fetch the list first" about a working setup.
    $("#ai-model-select").innerHTML = cfg.model
      ? `<option value="${App.esc(cfg.model)}" selected>${App.esc(cfg.model)}</option>`
      : `<option value="">— fetch the list first —</option>`;
    syncProviderFields();
    setStatus("#ai-status", "");
  }

  // The list comes from the provider rather than from a table in here: models
  // are released and retired on a schedule this app knows nothing about, and a
  // menu offering one the key cannot reach is worse than no menu at all.
  async function loadModels() {
    setStatus("#ai-status", "Asking the provider…");
    try {
      const out = await App.api.aiModels({
        provider: provider(),
        api_key: $("#ai-key").value.trim(),
        base_url: $("#ai-base-url").value.trim(),
      });
      if (!out.models.length) {
        setStatus("#ai-status", out.listed
          ? "That key has no models on it."
          : "This endpoint cannot list its models — type the name instead.", !out.listed);
        return;
      }
      const chosen = $("#ai-model").value.trim();
      $("#ai-model-select").innerHTML = out.models
        .map((m) => `<option value="${App.esc(m.id)}"${m.id === chosen ? " selected" : ""}
          >${App.esc(m.label)}</option>`).join("");
      if (!chosen) $("#ai-model").value = out.models[0].id;
      setStatus("#ai-status", `${out.models.length} model${out.models.length === 1 ? "" : "s"}`);
    } catch (e) {
      setStatus("#ai-status", e.message || "Could not reach the provider", true);
    }
  }

  async function saveSettings() {
    setStatus("#ai-status", "Checking…");
    try {
      // The server probes the provider before storing anything, so "Saved" here
      // means the key actually works — not merely that it was written down.
      const out = await App.api.saveAiConfig({
        provider: provider(),
        model: $("#ai-model").value.trim(),
        base_url: $("#ai-base-url").value.trim(),
        api_key: $("#ai-key").value.trim(),
      });
      $("#ai-key").value = "";      // saved and encrypted; nothing left to hold here
      await refreshConfig();
      setStatus("#ai-status", out.warning || "Saved", !!out.warning);
      if (!out.warning) setTimeout(() => setStatus("#ai-status", ""), 2500);
    } catch (e) {
      setStatus("#ai-status", e.message || "Could not save", true);
    }
  }

  async function clearSettings() {
    if (!confirm("Turn the AI features off and forget every saved key?")) return;
    setStatus("#ai-status", "Removing…");
    try {
      await App.api.clearAiConfig();
      await refreshConfig();
      $("#ai-key").value = "";
      $("#ai-model").value = "";
      setStatus("#ai-status", "Removed");
      setTimeout(() => setStatus("#ai-status", ""), 2500);
    } catch (e) {
      setStatus("#ai-status", e.message || "Could not remove", true);
    }
  }

  return { init, enabled, refreshConfig, onSettingsOpen,
           openSearch, closeSearch, searchOpen,
           openThread, closeThread, threadOpen,
           openAttachment, closeAttachment, attachmentOpen,
           suggestReminder };
})();
