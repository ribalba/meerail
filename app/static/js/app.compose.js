/* meerail compose: new message / reply / reply-all / forward. */

App.compose = (function () {
  let accounts = [];
  let identities = [];     // flat [{account_id, address, label}] across all accounts
  let replyTo = null;      // in_reply_to message-id
  let references = [];
  let staged = [];         // [{id, filename, size}]
  const $ = (s) => document.querySelector(s);

  // One entry per sendable address: the account primary plus its extra
  // "send as" addresses (Proton aliases). Drives the From dropdown.
  function buildIdentities() {
    identities = [];
    for (const a of accounts) {
      const addrs = [a.email, ...(a.send_addresses || []).filter((x) => x && x !== a.email)];
      for (const address of addrs) {
        identities.push({ account_id: a.id, address, label: a.label || a.email });
      }
    }
  }

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

  // --- Drag & drop attachments -----------------------------------------
  // The backdrop covers the viewport while the composer is open, so a drop
  // anywhere in the window attaches. Dropped folders are skipped: they arrive
  // as unreadable zero-byte Files that the upload would choke on.
  let dragDepth = 0;   // dragenter/leave fire per element, so nesting needs a count

  function hasFiles(e) {
    return Array.from(e.dataTransfer?.types || []).includes("Files");
  }

  function showDropHint(on) {
    if (!on) dragDepth = 0;
    $("#compose-drop").hidden = !on;
  }

  function droppedFiles(dt) {
    const items = Array.from(dt.items || []);
    if (!items.length || !items[0].webkitGetAsEntry) return Array.from(dt.files);
    const out = [];
    items.forEach((item, i) => {
      const entry = item.webkitGetAsEntry();
      if (entry && !entry.isFile) return;       // directory — skip
      const file = item.getAsFile() || dt.files[i];
      if (file) out.push(file);
    });
    return out;
  }

  function onDrop(e) {
    e.preventDefault();
    showDropHint(false);
    if (!hasFiles(e)) return;
    const files = droppedFiles(e.dataTransfer);
    if (files.length) onFiles(files);          // sets its own status as it uploads
    else if (e.dataTransfer.items?.length) $("#compose-status").textContent = "Folders can't be attached.";
  }

  function parseAddrs(v) {
    return (v || "").split(",").map((s) => s.trim()).filter(Boolean);
  }

  // Pick the identity index matching an account (and optionally a specific
  // address). Falls back to the account's first identity, then to 0.
  function findIdentity(accountId, address) {
    const lc = address ? address.toLowerCase() : null;
    let byAccount = -1;
    for (let i = 0; i < identities.length; i++) {
      const id = identities[i];
      if (accountId != null && id.account_id !== accountId) continue;
      if (byAccount < 0) byAccount = i;
      if (lc && id.address.toLowerCase() === lc) return i;
    }
    return byAccount >= 0 ? byAccount : 0;
  }

  function fillFrom(accountId, address) {
    const sel = $("#compose-from");
    sel.innerHTML = identities.map((id, i) =>
      `<option value="${i}">${App.esc(id.label)} &lt;${App.esc(id.address)}&gt;</option>`).join("");
    sel.value = String(findIdentity(accountId, address));
    $("#compose-from-row").style.display = identities.length > 1 ? "" : "none";
  }

  // --- Window dragging -------------------------------------------------
  // The window starts centred by the backdrop's flexbox; the first drag pins
  // it to pixel coordinates and it stays where the user left it.
  let lastDragEnd = 0;   // suppresses the backdrop click-to-close right after a drag

  function placeAt(left, top) {
    const win = $("#compose-window");
    // Keep at least a grabbable strip of the header on screen.
    const minLeft = 60 - win.offsetWidth;
    win.style.left = `${Math.min(Math.max(left, minLeft), window.innerWidth - 60)}px`;
    win.style.top = `${Math.min(Math.max(top, 0), Math.max(0, window.innerHeight - 40))}px`;
  }

  function startDrag(e) {
    if (e.button !== 0 || e.target.closest("button")) return;
    const win = $("#compose-window");
    const rect = win.getBoundingClientRect();
    if (!win.classList.contains("dragging-placed")) {
      // Freeze the current size so leaving the flex layout doesn't reflow it.
      win.style.width = `${rect.width}px`;
      win.style.height = `${rect.height}px`;
      win.classList.add("dragging-placed");
    }
    const dx = e.clientX - rect.left, dy = e.clientY - rect.top;
    let moved = false;

    const onMove = (ev) => {
      moved = true;
      placeAt(ev.clientX - dx, ev.clientY - dy);
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      if (moved) lastDragEnd = performance.now();
    };
    placeAt(rect.left, rect.top);
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    e.preventDefault();
  }

  function show(title) {
    $("#compose-title").textContent = title;
    $("#compose-status").textContent = "";
    $("#compose-modal").hidden = false;
  }

  function close() { showDropHint(false); $("#compose-modal").hidden = true; }

  function openWith(ctx) {
    replyTo = ctx.in_reply_to || null;
    references = ctx.references || [];
    staged = [];
    renderAttachments();
    fillFrom(ctx.account_id, ctx.from_address);
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
      const from = identities[Number($("#compose-from").value)] || identities[0] || {};
      await App.api.sendMail({
        account_id: from.account_id,
        from_address: from.address,
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
    $("#compose-modal").addEventListener("click", (e) => {
      if (e.target.id === "compose-modal" && performance.now() - lastDragEnd > 200) close();
    });
    $("#compose-head").addEventListener("pointerdown", startDrag);

    const modal = $("#compose-modal");
    modal.addEventListener("dragenter", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      if (++dragDepth === 1) showDropHint(true);
    });
    modal.addEventListener("dragover", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();                       // required, or the drop never fires
      e.dataTransfer.dropEffect = "copy";
    });
    modal.addEventListener("dragleave", (e) => {
      if (hasFiles(e) && --dragDepth <= 0) showDropHint(false);
    });
    modal.addEventListener("drop", onDrop);
    // Without this the browser navigates away when a file misses the composer.
    document.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
    document.addEventListener("drop", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      showDropHint(false);
    });
    // A shrinking viewport must not strand the window off-screen.
    window.addEventListener("resize", () => {
      const win = $("#compose-window");
      if (!win.classList.contains("dragging-placed")) return;
      placeAt(parseFloat(win.style.left) || 0, parseFloat(win.style.top) || 0);
    });
    try { accounts = await App.api.accounts(); } catch (_) { accounts = []; }
    buildIdentities();
  }

  return {
    init, openNew, openReply,
    refreshAccounts: async () => {
      try { accounts = await App.api.accounts(); } catch (_) { accounts = []; }
      buildIdentities();
    },
  };
})();
