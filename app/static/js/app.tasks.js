/* meerail tasks: file a mail as a Meerato task.

   The "Add Task" buttons only exist once a Meerato private URL is saved in
   Settings, so `configured` is fetched at boot and re-read whenever that field
   is saved. Everything goes through /api/tasks/* — the browser never talks to
   Meerato directly (see app/routers/tasks.py for why). */

App.tasks = (function () {
  const $ = (s) => document.querySelector(s);

  let configured = false;
  let current = null;     // the message the open dialog is filing
  let options = null;     // last /api/tasks/options payload

  async function init() {
    await refreshConfig();
    $("#btn-close-task").innerHTML = App.icon("close", 18);
    $("#btn-close-ticket").innerHTML = App.icon("close", 18);
    $("#btn-close-task").addEventListener("click", close);
    $("#task-modal").addEventListener("click", (e) => {
      if (e.target.id === "task-modal") close();
    });
    $("#task-create").addEventListener("click", create);
  }

  // Buttons appear and disappear with the setting, so the reader's bar is
  // redrawn on every change rather than only when a thread opens.
  async function refreshConfig() {
    let cfg = { configured: false };
    try { cfg = await App.api.taskConfig(); } catch (_) {}
    const was = configured;
    configured = !!cfg.configured;
    if (was !== configured && App.reader) App.reader.redraw();
    return cfg;
  }

  function enabled() { return configured; }
  function isOpen() { return !$("#task-modal").hidden; }

  function close() {
    $("#task-modal").hidden = true;
    current = null;
  }

  // --- The dialog ---

  async function open(m) {
    if (!m || !configured) return;
    current = m;
    $("#task-modal").hidden = false;
    $("#task-title").value = m.subject && m.subject !== "(no subject)" ? m.subject : "";
    $("#task-create").disabled = true;
    setStatus("");
    renderAttachments(m);
    // Buckets and statuses are Meerato's, not ours, so they are fetched fresh
    // each time — a bucket added over there shows up without a reload here.
    $("#task-selects").innerHTML = `<div class="task-loading">Loading buckets…</div>`;
    try {
      options = await App.api.taskOptions();
    } catch (e) {
      $("#task-selects").innerHTML = "";
      setStatus(e.message || "Could not reach Meerato", true);
      return;
    }
    renderSelects();
    $("#task-create").disabled = false;
  }

  // Shared with the composer's ticket dialog, which offers a bucket but not a
  // status — it always files into the Backlog.
  function bucketField(id, opts) {
    const buckets = opts.buckets || [];
    const def = opts.default_bucket_id;
    return `<label class="task-field">
        <span>Bucket</span>
        <select id="${id}">${
          buckets.length
            ? buckets.map((b) => `<option value="${App.esc(b.id)}"${b.id === def ? " selected" : ""}
                >${App.esc(b.name)}</option>`).join("")
            : `<option value="">No buckets — Meerato will pick one</option>`
        }</select>
      </label>`;
  }

  function renderSelects() {
    const statuses = options.statuses || [];
    $("#task-selects").innerHTML = bucketField("task-bucket", options) + `
      <label class="task-field">
        <span>Status</span>
        <select id="task-status">${
          statuses.map((s) => `<option value="${App.esc(s.value)}">${App.esc(s.label)}</option>`).join("")
        }</select>
      </label>`;
  }

  // Every file is ticked by default — the mail's attachments are usually the
  // reason it became a task — but each is a separate upload to Meerato, so a
  // 20 MB scan stays something you can leave behind.
  function renderAttachments(m) {
    const box = $("#task-attachments");
    const files = (m.attachments || []).filter((a) => !a.is_inline);
    if (!files.length) { box.innerHTML = ""; box.hidden = true; return; }
    box.hidden = false;
    box.innerHTML = `<div class="task-att-head">Attach ${files.length} file${files.length > 1 ? "s" : ""}</div>` +
      files.map((a) => `<label class="task-att">
        <input type="checkbox" data-att="${a.id}" checked />
        ${App.icon("paperclip", 14)}
        <span class="task-att-name">${App.esc(a.filename)}</span>
        <span class="task-att-size">${App.fmtSize(a.size)}</span>
      </label>`).join("");
  }

  function setStatus(text, error) {
    const el = $("#task-status-line");
    el.textContent = text;
    el.classList.toggle("error", !!error);
  }

  async function create() {
    if (!current) return;
    const btn = $("#task-create");
    btn.disabled = true;
    setStatus("Creating…");
    const payload = {
      message_id: current.id,
      title: $("#task-title").value.trim(),
      bucket_id: ($("#task-bucket") || {}).value || null,
      status: ($("#task-status") || {}).value || null,
      attachment_ids: Array.from(document.querySelectorAll("#task-attachments [data-att]"))
        .filter((c) => c.checked).map((c) => Number(c.dataset.att)),
    };
    try {
      const res = await App.api.createTask(payload);
      // A file that would not upload is not a failed task — say what landed and
      // what did not, and leave the dialog closed either way.
      close();
      if (res.failed && res.failed.length) {
        alert(`Task created, but ${res.failed.length} attachment(s) could not be uploaded:\n` +
          res.failed.join("\n"));
      }
    } catch (e) {
      setStatus(e.message || "Could not create the task", true);
      btn.disabled = false;
    }
  }

  // --- The composer's ticket dialog ---
  // Bucket and date only, and it resolves rather than acting: the composer has
  // to know the answer *before* it sends, so a cancel here means nothing went
  // out. Creating the task is the composer's job once the mail is away.

  // The user's own calendar day. toISOString() is UTC and would name yesterday
  // for anyone east of Greenwich in the small hours — and Meerato reads these
  // dates as days in the owner's timezone.
  function localISO(offsetDays) {
    const d = new Date();
    d.setDate(d.getDate() + (offsetDays || 0));
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }

  function setTicketStatus(text, error) {
    const el = $("#ticket-status-line");
    el.textContent = text;
    el.classList.toggle("error", !!error);
  }

  function promptTicket() {
    if (!configured) return Promise.resolve(null);
    const modal = $("#ticket-modal");
    const date = $("#ticket-date");
    const confirmBtn = $("#ticket-confirm");

    modal.hidden = false;
    setTicketStatus("");
    // Tomorrow, not today: a task you want in front of you now would not be
    // going into the Backlog in the first place.
    date.min = localISO(0);
    date.value = localISO(1);
    confirmBtn.disabled = true;
    $("#ticket-selects").innerHTML = `<div class="task-loading">Loading buckets…</div>`;

    return new Promise((resolve) => {
      let settled = false;
      const finish = (value) => {
        if (settled) return;
        settled = true;
        modal.hidden = true;
        modal.removeEventListener("click", onBackdrop);
        document.removeEventListener("keydown", onKey, true);
        $("#btn-close-ticket").removeEventListener("click", onCancel);
        confirmBtn.removeEventListener("click", onConfirm);
        resolve(value);
      };
      const onCancel = () => finish(null);
      const onBackdrop = (e) => { if (e.target === modal) finish(null); };
      // Captured, so Escape closes this dialog rather than the composer beneath it.
      const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); finish(null); } };
      const onConfirm = () => {
        if (!date.value) return setTicketStatus("Pick a date.", true);
        finish({ bucket_id: ($("#ticket-bucket") || {}).value || null, date: date.value });
      };

      modal.addEventListener("click", onBackdrop);
      document.addEventListener("keydown", onKey, true);
      $("#btn-close-ticket").addEventListener("click", onCancel);
      confirmBtn.addEventListener("click", onConfirm);

      App.api.taskOptions().then((opts) => {
        if (settled) return;
        options = opts;
        $("#ticket-selects").innerHTML = bucketField("ticket-bucket", opts);
        confirmBtn.disabled = false;
      }).catch((e) => {
        if (settled) return;
        $("#ticket-selects").innerHTML = "";
        setTicketStatus(e.message || "Could not reach Meerato", true);
      });
    });
  }

  return { init, open, close, isOpen, enabled, refreshConfig, promptTicket };
})();
