/* meerail compose: new message / reply / reply-all / forward. */

App.compose = (function () {
  let accounts = [];
  let identities = [];     // flat [{account_id, address, label}] across all accounts
  let replyTo = null;      // in_reply_to message-id
  let references = [];
  let staged = [];         // [{id, filename, size}]
  let draftGeneration = 0; // invalidates uploads still running for a discarded draft
  let body = null;         // markdown live-preview editor over #compose-body
  let prefilledFooter = ""; // footer put in the editor on open; alone it is not a draft
  let archivable = false;  // opened off a thread, so "Send & Archive" has something to file
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
      b.addEventListener("click", () => {
        const [removed] = staged.splice(Number(b.dataset.i), 1);
        renderAttachments();
        if (removed) App.api.deleteAttachment(removed.id).catch(() => {});
      }));
  }

  function discardStaged() {
    draftGeneration += 1;
    const abandoned = staged;
    staged = [];
    if (body) renderAttachments();
    for (const attachment of abandoned) {
      App.api.deleteAttachment(attachment.id).catch(() => {});
    }
  }

  async function onFiles(files) {
    const status = $("#compose-status");
    const generation = draftGeneration;
    for (const file of files) {
      if (generation !== draftGeneration) break;
      status.textContent = `Uploading ${file.name}…`;
      try {
        const attachment = await App.api.uploadAttachment(file);
        if (generation !== draftGeneration) {
          App.api.deleteAttachment(attachment.id).catch(() => {});
          break;
        }
        staged.push(attachment);
        renderAttachments();
        status.textContent = "";
      } catch (e) {
        if (generation === draftGeneration) status.textContent = e.message || "Upload failed";
      }
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

    const onMove = (ev) => placeAt(ev.clientX - dx, ev.clientY - dy);
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    placeAt(rect.left, rect.top);
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    e.preventDefault();
  }

  function show(title) {
    $("#compose-title").textContent = title;
    $("#compose-min-title").textContent = title;
    $("#compose-status").textContent = "";
    $("#compose-min").hidden = true;
    $("#compose-modal").hidden = false;
  }

  function close() {
    showDropHint(false);
    discardStaged();
    $("#compose-modal").hidden = true;
    $("#compose-min").hidden = true;
  }

  // --- Minimize ---------------------------------------------------------
  // Minimizing only hides the window; every field keeps its value, so the
  // draft is still there when the bar at the bottom is clicked. Nothing but
  // the × (here or on the bar) throws a draft away.
  const isMinimized = () => !$("#compose-min").hidden;

  function minimize() {
    if ($("#compose-modal").hidden) return;
    showDropHint(false);
    const subject = $("#compose-subject").value.trim();
    $("#compose-min-title").textContent = subject || $("#compose-title").textContent;
    $("#compose-modal").hidden = true;
    $("#compose-min").hidden = false;
  }

  function restore() {
    $("#compose-min").hidden = true;
    $("#compose-modal").hidden = false;
    focusBody();
  }

  function hasDraft() {
    return ["#compose-to", "#compose-cc", "#compose-bcc", "#compose-subject"]
      .some((s) => $(s).value.trim())
      || body.getText().replace(prefilledFooter, "").trim() !== ""
      || staged.length > 0;
  }

  // A minimized draft must not be silently overwritten by a new composer.
  function mayReplaceDraft() {
    if (!isMinimized() || !hasDraft()) return true;
    return confirm("Discard the minimized draft and open this message instead?");
  }

  // The account's footer is prefilled into the editor rather than stapled on at
  // send time, so it is visible and can be edited or deleted per message. It
  // goes above any quoted text, where a signature belongs, with the caret
  // landing on the blank line above it.
  function withFooter(text, accountId) {
    const acct = accounts.find((a) => a.id === accountId) || accounts[0];
    prefilledFooter = ((acct && acct.footer) || "").replace(/\n+$/, "");
    if (!prefilledFooter) return text || "";
    return `\n\n${prefilledFooter}${text || ""}`;
  }

  // Cc/Bcc stay folded away until asked for. Hiding clears the field: a
  // recipient the user cannot see is one they cannot decide to remove, so an
  // invisible row must never carry an address into sendNow().
  function showExtra(which, on) {
    $(`#compose-${which}-row`).hidden = !on;
    $(`#compose-${which}-toggle`).setAttribute("aria-expanded", String(!!on));
    if (!on) $(`#compose-${which}`).value = "";
  }

  function toggleExtra(which) {
    const on = $(`#compose-${which}-row`).hidden;
    showExtra(which, on);
    if (on) $(`#compose-${which}`).focus();
  }

  function openWith(ctx) {
    discardStaged();
    replyTo = ctx.in_reply_to || null;
    references = ctx.references || [];
    archivable = !!ctx.archivable;
    updateSendButtons();
    renderAttachments();
    fillFrom(ctx.account_id, ctx.from_address);
    $("#compose-to").value = (ctx.to || []).join(", ");
    // A reply that already carries Cc/Bcc opens with those rows visible —
    // prefilled recipients have to be seen before the message goes out.
    const cc = (ctx.cc || []).join(", ");
    const bcc = (ctx.bcc || []).join(", ");
    showExtra("cc", !!cc);
    showExtra("bcc", !!bcc);
    $("#compose-cc").value = cc;
    $("#compose-bcc").value = bcc;
    $("#compose-subject").value = ctx.subject || "";
    body.setText(withFooter(ctx.body_text, ctx.account_id));
    show(ctx.title || "New Message");
    focusBody();
  }

  // A reply is pre-addressed, so the caret belongs in the body; a blank
  // message starts in To. Replies open with a leading blank line above the
  // quote, so the caret goes to the top rather than the end.
  function focusBody() {
    if ($("#compose-to").value) body.focus(false); else $("#compose-to").focus();
  }

  function openNew() {
    // A minimized draft is what "new message" would have been — bring it back.
    if (isMinimized()) return restore();
    openWith({ account_id: accounts[0] && accounts[0].id, title: "New Message" });
  }

  async function openReply(messageId, mode) {
    if (!mayReplaceDraft()) return restore();
    try {
      const ctx = await App.api.replyContext(messageId, mode);
      ctx.title = mode === "forward" ? "Forward" : (mode === "replyall" ? "Reply All" : "Reply");
      // The reader is what opened this, so its thread is the one to archive.
      ctx.archivable = !!(App.reader && App.reader.isOpen());
      openWith(ctx);
    } catch (e) { alert("Could not open composer: " + e.message); }
  }

  // --- Sending -----------------------------------------------------------
  // Three buttons share one path. `after` is the extra step the variants add
  // once the mail is away — archiving the thread, filing a ticket — and it is
  // deliberately *after* the send: a failure there is a failed follow-up, not a
  // failed send, and must never read as "the mail didn't go".

  const SEND_BUTTONS = ["#compose-send", "#compose-send-archive", "#compose-send-ticket"];

  // All of them, not just the one clicked — otherwise the other two stay live
  // during an in-flight send and a second click sends the mail twice.
  function busy(on) {
    for (const sel of SEND_BUTTONS) $(sel).disabled = on;
  }

  async function send(after) {
    const status = $("#compose-status");
    const to = parseAddrs($("#compose-to").value);
    if (!to.length) { status.textContent = "Add at least one recipient."; return false; }
    status.textContent = "Sending…";
    busy(true);
    try {
      const from = identities[Number($("#compose-from").value)] || identities[0] || {};
      await App.api.sendMail({
        account_id: from.account_id,
        from_address: from.address,
        to, cc: parseAddrs($("#compose-cc").value), bcc: parseAddrs($("#compose-bcc").value),
        subject: $("#compose-subject").value,
        // The editor only decorates; what leaves here is the markdown source
        // the user typed, sent as text/plain exactly as before.
        body_text: body.getText(),
        in_reply_to: replyTo, references,
        attachments: staged.map((a) => a.id),
      });
      // The server baked these files into the queued MIME and removed them.
      // They are no longer part of a draft, even if a follow-up action fails.
      staged = [];
      renderAttachments();
    } catch (e) {
      status.textContent = e.message || "Send failed";
      busy(false);
      return false;
    }
    status.textContent = "Sent ✓";
    if (after) {
      try {
        await after();
      } catch (e) {
        // The window stays open on purpose: the follow-up is the only thing
        // left to retry or do by hand, and closing would hide why.
        status.textContent = `Sent ✓ — ${e.message || "the follow-up failed"}`;
        busy(false);
        return true;
      }
    }
    setTimeout(close, 700);
    busy(false);
    return true;
  }

  function sendNow() { return send(null); }

  // Whatever the primary button currently is — Send & Archive when there is a
  // thread behind this, plain Send otherwise. Keeps the keyboard and the
  // buttons saying the same thing about what "the default" means.
  function sendDefault() { return archivable ? sendAndArchive() : sendNow(); }

  function sendAndArchive() {
    return send(async () => {
      $("#compose-status").textContent = "Archiving…";
      await App.reader.archiveThread();
      $("#compose-status").textContent = "Sent ✓ · archived";
    });
  }

  // The bucket and date are asked for before anything is sent, so backing out
  // of the dialog leaves the draft exactly as it was.
  async function sendAndTicket() {
    if (!ticketable()) return false;
    const choice = await App.tasks.promptTicket();
    if (!choice) return false;
    const title = $("#compose-subject").value.trim();
    const text = body.getText();
    return send(async () => {
      $("#compose-status").textContent = "Creating task…";
      const res = await App.api.createTask({
        title, text,
        bucket_id: choice.bucket_id,
        status: "open",                  // Meerato's Backlog
        schedule_date: choice.date,      // …until it moves itself to Now
      });
      $("#compose-status").textContent = `Sent ✓ · ${res.title} filed`;
    });
  }

  function ticketable() { return !!(App.tasks && App.tasks.enabled()); }

  // A button that cannot act is not shown at all: archiving needs the
  // conversation this is a reply to, ticketing needs a Meerato URL. The default
  // styling follows — Send & Archive carries it when it is there, plain Send
  // when it is not, so whatever is on screen has exactly one obvious default.
  // Both conditions can change while the composer sits minimized, so all three
  // buttons are decided on each open.
  function updateSendButtons() {
    const archive = $("#compose-send-archive");
    const plain = $("#compose-send");
    archive.hidden = !archivable;
    archive.classList.toggle("btn-primary", archivable);
    archive.classList.toggle("btn-secondary", !archivable);
    plain.classList.toggle("btn-primary", !archivable);
    plain.classList.toggle("btn-secondary", archivable);
    $("#compose-send-ticket").hidden = !ticketable();
  }

  async function init() {
    body = App.markdown.editor($("#compose-body"));
    $("#compose-close").innerHTML = App.icon("close", 18);
    $("#compose-minimize").innerHTML = App.icon("minimize", 18);
    $("#compose-min-close").innerHTML = App.icon("close", 16);
    $("#compose-attach").innerHTML = App.icon("paperclip", 18);
    $("#compose-close").addEventListener("click", close);
    $("#compose-minimize").addEventListener("click", minimize);
    $("#compose-min-restore").addEventListener("click", restore);
    $("#compose-min-close").addEventListener("click", close);
    $("#compose-send").addEventListener("click", sendNow);
    $("#compose-send-archive").addEventListener("click", sendAndArchive);
    $("#compose-send-ticket").addEventListener("click", sendAndTicket);
    $("#compose-attach").addEventListener("click", () => $("#compose-file").click());
    $("#compose-cc-toggle").addEventListener("click", () => toggleExtra("cc"));
    $("#compose-bcc-toggle").addEventListener("click", () => toggleExtra("bcc"));
    $("#compose-file").addEventListener("change", (e) => onFiles(e.target.files));
    ["#compose-to", "#compose-cc", "#compose-bcc"].forEach((s) => App.autocomplete.attach($(s)));
    // Deliberately no backdrop click handler: clicking outside the window
    // leaves the composer exactly as it is. Minimizing is the − button's job.
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
    init, openNew, openReply, close, sendNow, sendDefault, sendAndArchive, sendAndTicket,
    minimize, restore,
    isOpen: () => !$("#compose-modal").hidden,
    isMinimized,
    refreshAccounts: async () => {
      try { accounts = await App.api.accounts(); } catch (_) { accounts = []; }
      buildIdentities();
    },
  };
})();
