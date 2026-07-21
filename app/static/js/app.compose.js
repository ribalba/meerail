/* meerail compose: new message / reply / reply-all / forward. */

App.compose = (function () {
  let accounts = [];
  let replyTo = null;      // in_reply_to message-id
  let references = [];
  let staged = [];         // [{id, filename, size}]
  const $ = (s) => document.querySelector(s);

  function renderAttachments() {
    const host = $("#compose-attachments");
    host.innerHTML = staged.map((a, i) =>
      `<span class="compose-chip">${App.icon("paperclip", 13)}
        <span class="cc-name">${App.esc(a.filename)}</span>
        <span class="cc-size">${App.fmtSize(a.size)}</span>
        <button class="cc-remove" data-i="${i}" title="Remove">×</button></span>`).join("");
    host.querySelectorAll(".cc-remove").forEach((b) =>
      b.addEventListener("click", () => { staged.splice(Number(b.dataset.i), 1); renderAttachments(); }));
  }

  async function onFiles(files) {
    const status = $("#compose-status");
    for (const file of files) {
      status.textContent = `Uploading ${file.name}…`;
      try {
        staged.push(await App.api.uploadAttachment(file));
        renderAttachments();
        status.textContent = "";
      } catch (e) { status.textContent = e.message || "Upload failed"; }
    }
    $("#compose-file").value = "";
  }

  function parseAddrs(v) {
    return (v || "").split(",").map((s) => s.trim()).filter(Boolean);
  }

  function fillFrom(selectedId) {
    const sel = $("#compose-from");
    sel.innerHTML = accounts.map((a) =>
      `<option value="${a.id}">${App.esc(a.label || a.email)} &lt;${App.esc(a.email)}&gt;</option>`).join("");
    if (selectedId) sel.value = String(selectedId);
    $("#compose-from-row").style.display = accounts.length > 1 ? "" : "none";
  }

  function show(title) {
    $("#compose-title").textContent = title;
    $("#compose-status").textContent = "";
    $("#compose-modal").hidden = false;
  }

  function close() { $("#compose-modal").hidden = true; }

  function openWith(ctx) {
    replyTo = ctx.in_reply_to || null;
    references = ctx.references || [];
    staged = [];
    renderAttachments();
    fillFrom(ctx.account_id);
    $("#compose-to").value = (ctx.to || []).join(", ");
    $("#compose-cc").value = (ctx.cc || []).join(", ");
    $("#compose-bcc").value = "";
    $("#compose-subject").value = ctx.subject || "";
    $("#compose-body").value = ctx.body_text || "";
    show(ctx.title || "New Message");
    ($("#compose-to").value ? $("#compose-body") : $("#compose-to")).focus();
  }

  function openNew() {
    openWith({ account_id: accounts[0] && accounts[0].id, title: "New Message" });
  }

  async function openReply(messageId, mode) {
    try {
      const ctx = await App.api.replyContext(messageId, mode);
      ctx.title = mode === "forward" ? "Forward" : (mode === "replyall" ? "Reply All" : "Reply");
      openWith(ctx);
    } catch (e) { alert("Could not open composer: " + e.message); }
  }

  async function sendNow() {
    const status = $("#compose-status");
    const to = parseAddrs($("#compose-to").value);
    if (!to.length) { status.textContent = "Add at least one recipient."; return; }
    status.textContent = "Sending…";
    $("#compose-send").disabled = true;
    try {
      await App.api.sendMail({
        account_id: Number($("#compose-from").value),
        to, cc: parseAddrs($("#compose-cc").value), bcc: parseAddrs($("#compose-bcc").value),
        subject: $("#compose-subject").value,
        body_text: $("#compose-body").value,
        in_reply_to: replyTo, references,
        attachments: staged.map((a) => a.id),
      });
      status.textContent = "Sent ✓";
      setTimeout(close, 700);
    } catch (e) {
      status.textContent = e.message || "Send failed";
    } finally {
      $("#compose-send").disabled = false;
    }
  }

  async function init() {
    $("#compose-close").innerHTML = App.icon("close", 18);
    $("#compose-attach").innerHTML = App.icon("paperclip", 18);
    $("#compose-close").addEventListener("click", close);
    $("#compose-send").addEventListener("click", sendNow);
    $("#compose-attach").addEventListener("click", () => $("#compose-file").click());
    $("#compose-file").addEventListener("change", (e) => onFiles(e.target.files));
    ["#compose-to", "#compose-cc", "#compose-bcc"].forEach((s) => App.autocomplete.attach($(s)));
    $("#compose-modal").addEventListener("click", (e) => { if (e.target.id === "compose-modal") close(); });
    try { accounts = await App.api.accounts(); } catch (_) { accounts = []; }
  }

  return { init, openNew, openReply, refreshAccounts: async () => { accounts = await App.api.accounts(); } };
})();
