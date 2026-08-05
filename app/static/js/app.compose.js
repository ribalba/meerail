/* meerail compose: new message / reply / reply-all / forward. */

App.compose = (function () {
  let accounts = [];
  let identities = [];     // flat [{account_id, address, label}] across all accounts
  let replyTo = null;      // in_reply_to message-id
  let references = [];
  let staged = [];         // [{id, filename, size}]
  let draftGeneration = 0; // which draft an upload belongs to; see stage()
  let generationSeq = 0;   // never reused: restoring puts an *old* generation back
  let body = null;         // markdown live-preview editor over #compose-body
  let prefilledFooter = ""; // footer put in the editor on open; alone it is not a draft
  let footerTail = "";     // text that opened *below* the footer (the quote), if any
  let archiveTicket = null; // the conversation this was opened off, for "Send & Archive"
  let fromPinned = false;  // From was decided (by the user, or by a reply) — stop guessing
  let fromDefault = 0;     // identity index the composer opened with
  let suggestSeq = 0;      // drops out-of-order sender-for replies
  let suggestKey = null;   // recipient set the last lookup was made for
  let suggestTimer = null;
  let relatedSeq = 0;      // same, for the co-recipient suggestions
  let relatedKey = null;
  let lastField = "#compose-to";  // recipient field a suggestion would be added to
  let suggestItems = [];   // the co-recipients currently offered, so a park can keep them
  let htmlMode = false;    // send a formatted copy alongside the plain text
  let minimized = [];      // parked drafts, oldest first — see "Minimize" below
  const $ = (s) => document.querySelector(s);
  const HTML_KEY = "meerail.compose.html";

  // One entry per sendable address: the account primary plus its extra
  // "send as" addresses (Proton aliases). Drives the From dropdown.
  //
  // The name shown is the one configured for that address in the agent config,
  // and so is exactly what the recipient will see on the From header — not the
  // account label, which names the account in the sidebar, is one per account
  // however many addresses it owns, and never goes out on a message.
  function buildIdentities() {
    identities = [];
    for (const a of accounts) {
      const addrs = [a.email, ...(a.send_addresses || []).filter((x) => x && x !== a.email)];
      const names = a.send_names || {};
      for (const address of addrs) {
        identities.push({ account_id: a.id, address, name: names[address.toLowerCase()] || "" });
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
    draftGeneration = ++generationSeq;
    const abandoned = staged;
    staged = [];
    if (body) renderAttachments();
    for (const attachment of abandoned) {
      App.api.deleteAttachment(attachment.id).catch(() => {});
    }
  }

  // A draft can be minimized while its files are still going up, so an upload
  // is followed home by its generation rather than assumed to belong to
  // whatever the composer is showing when it finishes. Answers false when that
  // draft is gone for good, which is the signal to delete the upload again.
  function stage(generation, attachment) {
    if (generation === draftGeneration) {
      staged.push(attachment);
      renderAttachments();
      return true;
    }
    const parked = minimized.find((d) => d.generation === generation);
    if (!parked) return false;
    parked.staged.push(attachment);
    renderBar();                 // the chip counts what it is carrying
    return true;
  }

  function draftAlive(generation) {
    return generation === draftGeneration || minimized.some((d) => d.generation === generation);
  }

  async function onFiles(files) {
    const status = $("#compose-status");
    const generation = draftGeneration;
    // The status line belongs to the window, so it is only written to while
    // this draft is the one in it.
    const onScreen = () => generation === draftGeneration;
    for (const file of files) {
      if (!draftAlive(generation)) break;
      if (onScreen()) status.textContent = `Uploading ${file.name}…`;
      try {
        const attachment = await App.api.uploadAttachment(file);
        if (!stage(generation, attachment)) {
          App.api.deleteAttachment(attachment.id).catch(() => {});
          break;
        }
        if (onScreen()) status.textContent = "";
      } catch (e) {
        if (onScreen()) status.textContent = e.message || "Upload failed";
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
      `<option value="${i}">${id.name ? `${App.esc(id.name)} &lt;${App.esc(id.address)}&gt;`
                                      : App.esc(id.address)}</option>`).join("");
    sel.value = String(findIdentity(accountId, address));
    $("#compose-from-row").style.display = identities.length > 1 ? "" : "none";
  }

  // --- From follows the recipients ---------------------------------------
  // Someone with a work address and a private one writes to each set of people
  // from a settled one of them. As addresses are added the server is asked
  // which of the user's own it has seen them written to from, and the From
  // dropdown follows the answer — but only while it is still an open question:
  // a From the user picked by hand, or one a reply inherited from the message
  // being answered, is a decision already made and is never overruled.

  const ADDRESS_RE = /^[^\s@,]+@[^\s@,]+\.[^\s@,]+$/;

  // Only whole addresses are worth asking about; a half-typed one matches
  // nothing and would just churn requests letter by letter. Cc/Bcc are read
  // from the fields, which showExtra() empties when it folds them away — an
  // invisible recipient must not steer the From either.
  function recipientAddresses() {
    const out = [];
    for (const sel of ["#compose-to", "#compose-cc", "#compose-bcc"]) {
      for (const token of parseAddrs($(sel).value)) {
        const angled = token.match(/<([^>]+)>/);           // "Name <addr>", if pasted that way
        const address = (angled ? angled[1] : token).trim().toLowerCase();
        if (ADDRESS_RE.test(address) && !out.includes(address)) out.push(address);
      }
    }
    return out;
  }

  function setFromNote(text) {
    const note = $("#compose-from-note");
    note.textContent = text;
    note.hidden = !text;
  }

  async function suggestFrom() {
    if (fromPinned || identities.length < 2) return;
    const addresses = recipientAddresses();
    const key = addresses.join(",");
    if (key === suggestKey) return;      // the same people as last time — same answer
    suggestKey = key;

    const seq = ++suggestSeq;
    const generation = draftGeneration;
    let hit = null;
    if (addresses.length) {
      try { hit = await App.api.senderFor(addresses); } catch (_) { return; }
    }
    // A newer lookup, a hand-picked From, or a different draft won the race.
    if (seq !== suggestSeq || generation !== draftGeneration || fromPinned) return;

    // No history means no opinion: fall back to what the composer opened with,
    // so deleting the recipient that caused a switch also undoes the switch.
    $("#compose-from").value = String(hit ? findIdentity(hit.account_id, hit.address) : fromDefault);
    swapFooter(currentAccountId());       // a guessed From signs with its own footer too

    // The note says where this From came from, so it belongs on every one the
    // history chose — including a choice that happens to match the default,
    // and including a switch back off an earlier guess. Falling back to the
    // default is not a choice and says nothing.
    setFromNote(hit ? "you usually write to these people from here" : "");
  }

  // --- Who else usually goes on this mail --------------------------------
  // People are written to in groups, and the groups repeat. Once the composer
  // knows one member the server can name the others, from who has actually
  // been addressed alongside them before.
  //
  // Offered, never added. A wrong guess here would put a stranger on a mail
  // silently, so every suggestion costs a click and nothing happens until it
  // is made — and the row says nothing at all when the history has no opinion.

  function setSuggestions(items) {
    const host = $("#compose-suggest");
    suggestItems = items;
    $("#compose-suggest-row").hidden = !items.length;
    host.innerHTML = items.map((c, i) =>
      `<button type="button" class="compose-suggest-btn" data-i="${i}"
               title="Add ${App.esc(c.address)}">
        <span class="cs-plus">+</span>
        <span class="cs-label">${App.esc(c.name || c.address)}</span>
      </button>`).join("");
    host.querySelectorAll(".compose-suggest-btn").forEach((b) =>
      b.addEventListener("click", () => addRecipient(items[Number(b.dataset.i)])));
  }

  // Into whichever recipient field was last used, so adding to Cc keeps adding
  // to Cc — but never into a row that has since been folded away, since that
  // clears on close and the address would vanish without ever being seen.
  function addRecipient(contact) {
    if (!contact) return;
    let sel = lastField;
    if (sel !== "#compose-to" && $(`${sel}-row`).hidden) sel = "#compose-to";
    const input = $(sel);
    const current = input.value.trim().replace(/,$/, "").trim();
    input.value = (current ? `${current}, ` : "") + contact.address + ", ";
    input.focus();
    // Scripted .value changes fire nothing, and this is a recipient arriving:
    // the From guess and the next round of suggestions both hang off it.
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }

  async function suggestRelated() {
    const addresses = recipientAddresses();
    const key = addresses.join(",");
    if (key === relatedKey) return;      // same people — same answer
    relatedKey = key;
    if (!addresses.length) return setSuggestions([]);

    const seq = ++relatedSeq;
    const generation = draftGeneration;
    let rows = [];
    try {
      rows = await App.api.relatedContacts(addresses);
    } catch (_) {
      relatedKey = null;                 // let the next keystroke try again
      return;
    }
    // A newer lookup or a different draft won the race.
    if (seq !== relatedSeq || generation !== draftGeneration) return;
    setSuggestions(rows);
  }

  function queueRecipientLookups() {
    clearTimeout(suggestTimer);
    suggestTimer = setTimeout(() => { suggestFrom(); suggestRelated(); }, 250);
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
    $("#compose-status").textContent = "";
    $("#compose-modal").hidden = false;
  }

  // Hiding the window does not move the caret out of it: the field keeps the
  // focus while invisible, and every keystroke after that — including the
  // shortcuts that would open the next message — disappears into it. So the
  // keyboard is handed back explicitly whenever the window goes away.
  function dropFocus() {
    const focused = document.activeElement;
    if (focused && $("#compose-modal").contains(focused)) focused.blur();
  }

  function close() {
    showDropHint(false);
    clearTimeout(suggestTimer);      // nothing to suggest to a discarded draft
    discardStaged();
    dropFocus();
    $("#compose-modal").hidden = true;
  }

  // --- Minimize ---------------------------------------------------------
  // There is one composer window but any number of drafts: minimizing lifts
  // the whole draft out of the window into a chip on the bar at the bottom,
  // which frees the window for the next message. Every field, attachment and
  // reply header travels with it, so bringing it back is the same draft and
  // not an approximation of it. Nothing but a × throws a draft away.
  //
  // Because a draft can be parked rather than lost, opening a second message
  // over a first never has to ask permission — see makeRoom().

  // Everything openWith() sets, plus the bookkeeping that decides how the
  // draft behaves from here on. The From is kept as an address rather than a
  // dropdown index: accounts can be reconfigured while a draft sits parked,
  // and an index would then point at somebody else.
  function snapshot() {
    const from = identities[Number($("#compose-from").value)] || {};
    const def = identities[fromDefault] || {};
    const subject = $("#compose-subject").value;
    return {
      generation: draftGeneration,
      title: $("#compose-title").textContent,
      label: subject.trim() || $("#compose-title").textContent,
      to: $("#compose-to").value,
      cc: $("#compose-cc").value,
      bcc: $("#compose-bcc").value,
      ccShown: !$("#compose-cc-row").hidden,
      bccShown: !$("#compose-bcc-row").hidden,
      subject,
      bodyText: body.getText(),
      status: $("#compose-status").textContent,
      fromNote: $("#compose-from-note").textContent,
      from: { account_id: from.account_id, address: from.address },
      fromDefault: { account_id: def.account_id, address: def.address },
      staged, replyTo, references, archiveTicket, fromPinned, htmlMode, lastField,
      prefilledFooter, footerTail, suggestKey, relatedKey,
      suggestions: suggestItems,
    };
  }

  // Put a snapshot back on screen. The staged attachments come back with their
  // original generation, so an upload that was still running when the draft was
  // parked lands in the composer it belongs to.
  function apply(s) {
    draftGeneration = s.generation;
    staged = s.staged;
    replyTo = s.replyTo;
    references = s.references;
    archiveTicket = s.archiveTicket;
    fromPinned = s.fromPinned;
    prefilledFooter = s.prefilledFooter;
    footerTail = s.footerTail;
    lastField = s.lastField;
    suggestKey = s.suggestKey;
    relatedKey = s.relatedKey;
    updateSendButtons();
    renderAttachments();
    fillFrom(s.from.account_id, s.from.address);
    fromDefault = findIdentity(s.fromDefault.account_id, s.fromDefault.address);
    showExtra("cc", s.ccShown);        // clears the field when off, so fill after
    showExtra("bcc", s.bccShown);
    $("#compose-to").value = s.to;
    $("#compose-cc").value = s.cc;
    $("#compose-bcc").value = s.bcc;
    $("#compose-subject").value = s.subject;
    setHtmlMode(s.htmlMode);
    setSuggestions(s.suggestions);
    setFromNote(s.fromNote);
    body.setText(s.bodyText);
    show(s.title);                     // clears the status line, so say this after
    $("#compose-status").textContent = s.status;
    focusBody();
  }

  // Hand the window over to another draft. Whatever is in it is parked, unless
  // there is nothing in it worth parking — an untouched composer is not a draft
  // and would only leave an empty chip behind.
  function makeRoom() {
    if ($("#compose-modal").hidden) return;
    if (hasDraft()) minimize(); else close();
  }

  function minimize() {
    if ($("#compose-modal").hidden) return;
    showDropHint(false);
    clearTimeout(suggestTimer);        // the lookups resume when it comes back
    minimized.push(snapshot());
    // The parked snapshot owns the files now: bumping the generation both stops
    // the next draft's discardStaged() from deleting them and tells an upload
    // still in flight which draft it is landing in.
    staged = [];
    draftGeneration = ++generationSeq;
    dropFocus();
    $("#compose-modal").hidden = true;
    renderBar();
  }

  // Bring one back. Anything already in the window is parked first, so
  // restoring never costs another draft.
  function restore(draft) {
    if (!draft) return false;
    makeRoom();
    minimized = minimized.filter((d) => d !== draft);
    apply(draft);
    renderBar();
    return true;
  }

  // Walks the parked drafts one press at a time: the open one goes to the back
  // of the queue and the one that has waited longest comes up, so holding the
  // key visits every draft in turn and lands back where it started.
  function cycle() {
    if (!minimized.length) return false;
    return restore(minimized[0]);
  }

  function discard(draft) {
    minimized = minimized.filter((d) => d !== draft);
    for (const attachment of draft.staged) {
      App.api.deleteAttachment(attachment.id).catch(() => {});
    }
    renderBar();
  }

  function renderBar() {
    const bar = $("#compose-min-bar");
    bar.hidden = !minimized.length;
    bar.innerHTML = minimized.map((d, i) => {
      const n = d.staged.length;
      return `<div class="compose-min">
        <button class="compose-min-label" data-act="restore" data-i="${i}"
                title="Restore (Alt+C walks the minimized drafts)">
          ${App.esc(d.label)}${n ? ` <span class="cm-count">${App.icon("paperclip", 11)}${n}</span>` : ""}
        </button>
        <button class="icon-btn" data-act="discard" data-i="${i}"
                title="Discard">${App.icon("close", 16)}</button>
      </div>`;
    }).join("");
    bar.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
      const draft = minimized[Number(b.dataset.i)];
      if (!draft) return;
      if (b.dataset.act === "restore") restore(draft); else discard(draft);
    }));
  }

  function hasDraft() {
    return ["#compose-to", "#compose-cc", "#compose-bcc", "#compose-subject"]
      .some((s) => $(s).value.trim())
      || body.getText().replace(prefilledFooter, "").trim() !== ""
      || staged.length > 0;
  }

  // The account's footer is prefilled into the editor rather than stapled on at
  // send time, so it is visible and can be edited or deleted per message. It
  // goes above any quoted text, where a signature belongs, with the caret
  // landing on the blank line above it.
  function withFooter(text, accountId) {
    prefilledFooter = footerFor(accountId);
    footerTail = text || "";
    if (!prefilledFooter) return footerTail;
    return `\n\n${prefilledFooter}${footerTail}`;
  }

  function footerFor(accountId) {
    const acct = accounts.find((a) => a.id === accountId) || accounts[0];
    return ((acct && acct.footer) || "").replace(/\n+$/, "");
  }

  function currentAccountId() {
    const from = identities[Number($("#compose-from").value)] || identities[0];
    return from ? from.account_id : null;
  }

  // The footer signs the message with the address it goes out from, so changing
  // From has to change it too — otherwise a mail sent from the work account
  // arrives carrying the private one's signature.
  //
  // Only the footer this composer put there is swapped. Once the user has
  // edited or deleted it, the text is theirs and rewriting it would throw away
  // something they typed; in that case the From changes alone. The swap is one
  // undoable edit, so Ctrl-Z brings the old footer back.
  function swapFooter(accountId) {
    const next = footerFor(accountId);
    if (next === prefilledFooter) return;
    const text = body.getText();
    let out;
    if (prefilledFooter) {
      const at = text.indexOf(prefilledFooter);
      if (at < 0) return;                       // not ours any more — leave it be
      // An account with no footer at all: the blank line that held it goes too,
      // or the draft keeps a gap where a signature used to be.
      const before = next ? text.slice(0, at) : text.slice(0, at).replace(/\n{1,2}$/, "");
      out = before + next + text.slice(at + prefilledFooter.length);
    } else {
      // Nothing to replace: put the new footer where a prefilled one would have
      // gone — above the quote if the draft still opens onto one, else at the
      // end of what has been written.
      const at = footerTail && text.endsWith(footerTail) ? text.length - footerTail.length : text.length;
      out = `${text.slice(0, at).replace(/\n+$/, "")}\n\n${next}${text.slice(at)}`;
    }
    prefilledFooter = next;
    body.replaceText(out);
  }

  // --- "Send as HTML email" ---------------------------------------------
  // Off, the message is the text that is on screen and nothing else — the way
  // every mail this composer has ever sent went out. On, that text is rendered
  // and the message goes as HTML instead.
  //
  // Instead, not alongside: sending both as a multipart/alternative is the
  // textbook answer and was the first one tried, but the pair does not survive
  // delivery — see _build_mime in app/routers/compose.py. So this really is a
  // choice between two kinds of mail, and the price of the formatted one is
  // that a reader who cannot render HTML sees the markup.
  //
  // It is decided per message, because whether formatting is worth that is a
  // fact about the message: a table of figures wants it, a two-line reply to a
  // mailing list does not. The setting only chooses which way it starts.

  function htmlDefault() { return localStorage.getItem(HTML_KEY) === "1"; }

  function setHtmlDefault(on) { localStorage.setItem(HTML_KEY, on ? "1" : "0"); }

  function setHtmlMode(on) {
    htmlMode = !!on;
    const btn = $("#compose-html");
    btn.setAttribute("aria-pressed", String(htmlMode));
    btn.classList.toggle("on", htmlMode);
    btn.title = htmlMode
      ? "On: the markdown is rendered and the message is sent as HTML"
      : "Off: the message goes out as plain text, exactly as it is written";
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
    makeRoom();                 // park whatever was in the window first
    discardStaged();
    replyTo = ctx.in_reply_to || null;
    references = ctx.references || [];
    archiveTicket = ctx.archiveTicket || null;
    updateSendButtons();
    // A forward opens with the original's attachments already staged (the
    // server copied them into the outbox). They are ordinary staged files from
    // here on: removable one by one, and thrown away with the draft.
    staged = ctx.attachments || [];
    renderAttachments();
    fillFrom(ctx.account_id, ctx.from_address);
    // A reply's From is the alias the original was addressed to — a better
    // answer than any history could give, so it stands. A new message or a
    // forward starts with the question still open.
    fromDefault = Number($("#compose-from").value);
    fromPinned = !!ctx.in_reply_to;
    suggestKey = null;
    relatedKey = null;
    lastField = "#compose-to";
    setSuggestions([]);         // last draft's people are not this one's
    setFromNote("");
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
    setHtmlMode(htmlDefault());       // per message, so every draft starts from the setting
    // The footer follows the From that is actually on screen, which is not
    // always ctx.account_id — fillFrom falls back when that account is gone.
    body.setText(withFooter(ctx.body_text, currentAccountId()));
    show(ctx.title || "New Message");     // clears the status line, so say this after
    // An attachment on a message whose content has been pruned is a filename
    // and nothing else — it cannot go along, and silence would read as if it had.
    if (ctx.attachments_missing) {
      const n = ctx.attachments_missing;
      $("#compose-status").textContent =
        `${n} attachment${n === 1 ? "" : "s"} could not be forwarded — no longer stored.`;
    }
    focusBody();
    suggestFrom();          // a forward can open already addressed
    suggestRelated();       // and a reply-all is a group with a known shape
  }

  // A reply is pre-addressed, so the caret belongs in the body; a blank
  // message starts in To. Replies open with a leading blank line above the
  // quote, so the caret goes to the top rather than the end.
  function focusBody() {
    if ($("#compose-to").value) body.focus(false); else $("#compose-to").focus();
  }

  // Always a blank message: a minimized draft is one the user put aside, not
  // the one they are asking for now. Alt+C is how those come back.
  function openNew() {
    openWith({ account_id: accounts[0] && accounts[0].id, title: "New Message" });
  }

  async function openReply(messageId, mode) {
    // The reader is what opened this, so the thread it is showing *now* is the
    // one to archive. Taken before the await rather than after: the reply
    // context is a round trip, and a keystroke landing while it loads can put
    // another conversation on screen.
    const ticket = App.reader && App.reader.archiveTicket();
    try {
      const ctx = await App.api.replyContext(messageId, mode);
      ctx.title = mode === "forward" ? "Forward" : (mode === "replyall" ? "Reply All" : "Reply");
      ctx.archiveTicket = ticket;
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

  // What the window says once the server has the message. "Sent ✓" is a small
  // lie even without a delay — the agent has yet to relay it — but with one it
  // is the wrong lie: the mail is deliberately still here, catchable, and the
  // composer is the last place the author looks before they stop thinking about
  // it. So it says how long they have, and where.
  function word(res) {
    const at = res && res.send_at ? App.utcDate(res.send_at) : null;
    if (!at) return "Sent ✓";
    const s = Math.max(1, Math.round((at.getTime() - Date.now()) / 1000));
    const when = s < 90 ? `${s}s` : `${Math.round(s / 60)}m`;
    return `Sending in ${when} — in the Outbox until then`;
  }

  async function send(after) {
    const status = $("#compose-status");
    const to = parseAddrs($("#compose-to").value);
    if (!to.length) { status.textContent = "Add at least one recipient."; return false; }
    status.textContent = "Sending…";
    let sentWord = "Sent ✓";
    busy(true);
    try {
      const from = identities[Number($("#compose-from").value)] || identities[0] || {};
      // The editor only decorates; the markdown source the user typed is what
      // goes into body_text, and with the toggle off that is the whole message,
      // sent as text/plain exactly as it always has been. A draft with nothing
      // in it has nothing to render, so it stays plain either way.
      const text = body.getText();
      const res = await App.api.sendMail({
        account_id: from.account_id,
        from_address: from.address,
        to, cc: parseAddrs($("#compose-cc").value), bcc: parseAddrs($("#compose-bcc").value),
        subject: $("#compose-subject").value,
        body_text: text,
        body_html: htmlMode && text.trim() ? App.markdown.toMail(text) : "",
        in_reply_to: replyTo, references,
        attachments: staged.map((a) => a.id),
      });
      // The server baked these files into the queued MIME and removed them.
      // They are no longer part of a draft, even if a follow-up action fails.
      staged = [];
      renderAttachments();
      // The message is queued, not gone: the agent still has to relay it. The
      // sidebar's outbox count is the honest version of the "Sent ✓" below, so
      // read it back now rather than at the next poll.
      App.status.refresh();
      sentWord = word(res);
    } catch (e) {
      status.textContent = e.message || "Send failed";
      busy(false);
      return false;
    }
    status.textContent = sentWord;
    if (after) {
      try {
        // Handed the word the send earned, so a follow-up's own line does not
        // quietly promote a delayed message back to "Sent ✓".
        await after(sentWord);
      } catch (e) {
        // The window stays open on purpose: the follow-up is the only thing
        // left to retry or do by hand, and closing would hide why.
        status.textContent = `${sentWord} — ${e.message || "the follow-up failed"}`;
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
  function sendDefault() { return archiveTicket ? sendAndArchive() : sendNow(); }

  function sendAndArchive() {
    return send(async (sent) => {
      $("#compose-status").textContent = "Archiving…";
      // The conversation this draft was opened off, not whatever the reader
      // has moved on to since — those are rarely the same by the time a reply
      // is sent, let alone one that sat minimized.
      await App.reader.archiveTicketed(archiveTicket);
      $("#compose-status").textContent = `${sent} · archived`;
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
    return send(async (sent) => {
      $("#compose-status").textContent = "Creating task…";
      const res = await App.api.createTask({
        title, text,
        bucket_id: choice.bucket_id,
        status: "open",                  // Meerato's Backlog
        schedule_date: choice.date,      // …until it moves itself to Now
      });
      $("#compose-status").textContent = `${sent} · ${res.title} filed`;
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
    const archivable = !!archiveTicket;
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
    $("#compose-attach").innerHTML = App.icon("paperclip", 18);
    $("#compose-close").addEventListener("click", close);
    $("#compose-minimize").addEventListener("click", minimize);
    $("#compose-send").addEventListener("click", sendNow);
    $("#compose-send-archive").addEventListener("click", sendAndArchive);
    $("#compose-send-ticket").addEventListener("click", sendAndTicket);
    $("#compose-attach").addEventListener("click", () => $("#compose-file").click());
    $("#compose-html").addEventListener("click", () => setHtmlMode(!htmlMode));
    setHtmlMode(htmlDefault());
    $("#compose-cc-toggle").addEventListener("click", () => toggleExtra("cc"));
    $("#compose-bcc-toggle").addEventListener("click", () => toggleExtra("bcc"));
    $("#compose-file").addEventListener("change", (e) => onFiles(e.target.files));
    ["#compose-to", "#compose-cc", "#compose-bcc"].forEach((s) => {
      App.autocomplete.attach($(s));
      $(s).addEventListener("input", queueRecipientLookups);
      $(s).addEventListener("focus", () => { lastField = s; });
    });
    // Hiding a Cc/Bcc row clears it, which changes who the message is going to.
    ["#compose-cc-toggle", "#compose-bcc-toggle"].forEach((s) =>
      $(s).addEventListener("click", queueRecipientLookups));
    $("#compose-from").addEventListener("change", () => {
      fromPinned = true;                  // an explicit choice ends the guessing
      setFromNote("");
      swapFooter(currentAccountId());
    });
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
    minimize, cycle,
    htmlDefault, setHtmlDefault,     // the settings modal owns the checkbox, not the state
    isOpen: () => !$("#compose-modal").hidden,
    refreshAccounts: async () => {
      try { accounts = await App.api.accounts(); } catch (_) { accounts = []; }
      buildIdentities();
    },
  };
})();
