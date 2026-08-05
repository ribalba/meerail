"""Integration tests for the ingest pipeline the agent owns.

These drive `core.ingest` directly against the database — the same calls the
agent's sync loop makes — and assert the results through the server's read APIs.
(Formerly test_agent_protocol.py, which drove the deleted /api/agent/* HTTP API.)
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

import dbfixture
from core import ingest
from core.config import get_settings
from conftest import status_for
from helpers import api, api_bytes, make_message

T0 = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)


def _mb(email: str, imap_name: str) -> dict:
    st = status_for(email)
    return next(m for m in st["mailboxes"] if m["imap_name"] == imap_name)


def _detail_by_subject(account_id: int, subject: str) -> dict:
    """Find a message by subject through the read API and return its detail."""
    _, listing = api("GET", f"/api/messages?scope=unified_inbox&limit=200")
    row = next(r for r in listing["rows"]
               if r["subject"] == subject and r["account_id"] == account_id)
    _, detail = api("GET", f"/api/messages/{row['id']}")
    return detail


def test_ingest_threads_dedups_flags_and_prunes(account):
    email = account["email"]
    a, b, c = (f"{p}-{uuid.uuid4().hex}@t" for p in ("a", "b", "c"))
    A = make_message(f"<{a}>", "Subject ALPHA", "x@y.com", email, "the body", T0)
    B = make_message(f"<{b}>", "Re: Subject ALPHA", "z@y.com", email, "a reply",
                     T0 + timedelta(hours=1), in_reply_to=f"<{a}>", refs=[f"<{a}>"])
    C = make_message(f"<{c}>", "Unrelated BETA", "q@y.com", email, "other", T0 + timedelta(days=2))

    for uid, raw in enumerate((A, B, C), start=1):
        dbfixture.ingest_raw_message(email, raw, uid=uid)

    inbox = _mb(email, "INBOX")
    assert inbox["total"] == 3

    # A and B are one conversation; C is its own.
    _, rows = api("GET", f"/api/messages?mailbox_id={inbox['id']}&limit=50")
    threads = {r["thread_id"] for r in rows["rows"]}
    assert len(threads) == 2, threads

    # The same Message-ID under a second Proton label is a placement, not a copy.
    assert dbfixture.record_placement(email, a, uid=101, folder="Archive2",
                                      role_hint="\\Archive") is True
    assert dbfixture.message_count(email) == 3          # no duplicate content
    assert _mb(email, "Archive2")["total"] == 1

    # Flags sync per folder.
    dbfixture.set_flags(email, "INBOX", [{"uid": 1, "flags": {"seen": True}}])
    assert _mb(email, "INBOX")["unread"] == 2

    # uid 3 vanished from INBOX -> its placement goes...
    dbfixture.set_present(email, "INBOX", [1, 2])
    assert _mb(email, "INBOX")["total"] == 2
    # ...but the copy in Archive2 keeps that message alive.
    assert _mb(email, "Archive2")["total"] == 1
    assert dbfixture.message_count(email) == 2


def test_a_placement_lost_below_the_cursor_is_still_visible_to_the_repair(account):
    """The other half of prune_vanished, and the half that was missing.

    A pass only ever reads below a folder's cursor: new mail is fetched above
    last_uid, update_flags skips a UID it holds no row for, and the sweep's one
    write is the prune. So a placement pruned during a moment when the server
    did not list it — a Bridge part way through loading the mailbox, a Proton
    label cleared and put back — stayed gone, in a folder reconciled every
    fifteen minutes that could never notice. This is the query that notices.
    """
    email = account["email"]
    mid = f"gap-{uuid.uuid4().hex}@t"
    dbfixture.ingest_raw_message(
        email, make_message(f"<{mid}>", "Subject GAP", "x@y.com", email, "body", T0), uid=1)
    # A second label on the same message, so pruning the inbox placement leaves
    # the content alive — which is exactly how this was found in the field.
    dbfixture.record_placement(email, mid, uid=99, folder="All Mail", role_hint="\\All")

    assert dbfixture.unplaced_uids(email, "INBOX", [1]) == []

    dbfixture.set_present(email, "INBOX", [])          # the server "forgets" it
    assert _mb(email, "INBOX")["total"] == 0
    assert dbfixture.message_count(email) == 1         # the All Mail copy held it

    # The server lists uid 1 again. It is below the cursor, so nothing else in a
    # pass would ever ask about it.
    assert dbfixture.unplaced_uids(email, "INBOX", [1]) == [1]

    # And once it is back, it stops being reported as missing.
    dbfixture.record_placement(email, mid, uid=1, folder="INBOX")
    assert dbfixture.unplaced_uids(email, "INBOX", [1]) == []


def test_a_move_still_landing_is_not_read_as_a_gap_to_repair(account):
    """A move the user has just made looks exactly like a lost placement: the
    source placement goes the moment the key is pressed, and the server goes on
    listing the UID until the agent applies the move and catches up. Repairing
    that would put the message straight back in the folder it was archived out
    of — the disappearance the optimistic placement exists to prevent, in
    reverse."""
    email = account["email"]
    mid = f"inflight-{uuid.uuid4().hex}@t"
    dbfixture.ingest_raw_message(
        email, make_message(f"<{mid}>", "Subject FLIGHT", "x@y.com", email, "body", T0), uid=1)
    dbfixture.create_folder(email, "Archive", role_hint="\\Archive")

    _, boxes = api("GET", "/api/mailboxes")
    mine = next(a for a in boxes["accounts"] if a["email"] == email)["mailboxes"]
    inbox = next(m for m in mine if m["role"] == "inbox")
    _, listing = api("GET", f"/api/messages?mailbox_id={inbox['id']}&limit=50")
    message_id = listing["rows"][0]["id"]

    code, body = api("POST", f"/api/messages/{message_id}/archive"
                             f"?source_mailbox_id={inbox['id']}")
    assert code == 200, body

    # The inbox placement is gone locally and the move is queued, so the UID the
    # server still lists reads as missing — and must be left alone.
    assert dbfixture.unplaced_uids(email, "INBOX", [1]) == [1]
    assert dbfixture.move_in_flight(email, mid) is True

    # Applied an hour ago and the server still lists it: the move is not coming
    # back for it, and whatever the server says now is the truth.
    dbfixture.apply_actions(email, minutes_ago=60)
    assert dbfixture.move_in_flight(email, mid) is False
    assert dbfixture.unplaced_uids(email, "INBOX", [1]) == [1]   # now safe to repair


def test_repeated_alerts_with_one_subject_do_not_become_one_thread(account):
    """Machine mail shares a subject but is not a conversation.

    Monitoring alerts carry a fresh Message-ID and no References, so they fall
    through to subject-based threading. Without a reply prefix to justify it,
    each is its own thread — otherwise years of "gmt cluster" alerts collapse
    into a single row that opens as a thousand-message thread.
    """
    email = account["email"]
    for i in range(5):
        raw = make_message(f"<alert-{i}-{uuid.uuid4().hex}@t>", "[gmt cluster] disk usage high",
                           "monitor@y.com", email, f"reading {i}", T0 + timedelta(days=20 * i))
        dbfixture.ingest_raw_message(email, raw, uid=100 + i)

    inbox = _mb(email, "INBOX")
    _, rows = api("GET", f"/api/messages?mailbox_id={inbox['id']}&limit=50")
    alerts = [r for r in rows["rows"] if r["subject"].startswith("[gmt cluster]")]
    assert len({r["thread_id"] for r in alerts}) == 5
    assert all(r["thread_count"] == 1 for r in alerts)

    # A genuine human reply still joins the alert it answers.
    reply = make_message(f"<reply-{uuid.uuid4().hex}@t>", "Re: [gmt cluster] disk usage high",
                         "ops@y.com", email, "on it", T0 + timedelta(days=1))
    dbfixture.ingest_raw_message(email, reply, uid=200)
    _, rows = api("GET", f"/api/messages?mailbox_id={inbox['id']}&limit=50")
    joined = [r for r in rows["rows"] if r["subject"].endswith("disk usage high")]
    assert len(joined) == 5
    assert sorted(r["thread_count"] for r in joined) == [1, 1, 1, 1, 2]


def test_thread_count_matches_what_the_reader_opens(account):
    """The badge counts the whole conversation, not just this folder's slice.

    The reader loads a thread across folders, so a folder-scoped count let a row
    advertising "1" open as a much larger thread.
    """
    email = account["email"]
    root = f"root-{uuid.uuid4().hex}@t"
    dbfixture.ingest_raw_message(
        email, make_message(f"<{root}>", "Cross folder chat", "x@y.com", email, "hi", T0), uid=301)
    reply_mid = f"reply-{uuid.uuid4().hex}@t"
    dbfixture.ingest_raw_message(
        email,
        make_message(f"<{reply_mid}>", "Re: Cross folder chat", email, "x@y.com", "yes",
                     T0 + timedelta(hours=2), in_reply_to=f"<{root}>", refs=[f"<{root}>"]),
        uid=302, folder="Sent", role_hint="\\Sent")

    inbox = _mb(email, "INBOX")
    _, rows = api("GET", f"/api/messages?mailbox_id={inbox['id']}&limit=50")
    row = next(r for r in rows["rows"] if r["subject"] == "Cross folder chat")
    assert row["thread_count"] == 2

    _, thread = api("GET", f"/api/threads/{row['thread_id']}?account_id={row['account_id']}")
    assert len(thread["messages"]) == row["thread_count"]


def test_rescan_is_idempotent(account):
    email = account["email"]
    mid = f"solo-{uuid.uuid4().hex}@t"
    raw = make_message(f"<{mid}>", "Solo", "x@y.com", email, "hi", T0)
    dbfixture.ingest_raw_message(email, raw, uid=1)

    # Seeing the same UID/Message-ID again recognizes existing content.
    assert dbfixture.record_placement(email, mid, uid=1, folder="INBOX") is True
    assert _mb(email, "INBOX")["total"] == 1  # no duplicate row
    assert dbfixture.message_count(email) == 1


def test_uidvalidity_change_repoints_the_uid_without_deleting_mail(account):
    """A new UID epoch re-points the placement — it does not empty the folder.

    Bridge changes UIDVALIDITY for its own reasons (a re-login, a rebuilt
    cache), and this used to wipe every message in the folder and re-fetch. The
    content stays now: the cursor is rewound instead, the pass re-walks the
    folder, and mail still on the server is matched by Message-ID and simply
    gains its new placement. Nothing is downloaded twice and nothing is missing
    in between — which is the only version of this that survives a machine that
    goes offline halfway through.
    """
    email = account["email"]
    old = make_message(f"<old-{uuid.uuid4().hex}@t>", "Old UID epoch", "x@y.com", email,
                       "old body", T0)
    new = make_message(f"<new-{uuid.uuid4().hex}@t>", "New UID epoch", "x@y.com", email,
                       "new body", T0)
    dbfixture.ingest_raw_message(email, old, uid=1, uidvalidity=10)

    # The pass that meets the new epoch rewinds the cursor, which is what makes
    # it re-walk the folder instead of picking up where the old numbering left
    # off — and the mail is all still there while it does.
    assert dbfixture.register_folder(email, "INBOX", uidvalidity=11) == 0
    assert dbfixture.location_count(email, "INBOX") == 1

    dbfixture.ingest_raw_message(email, new, uid=1, uidvalidity=11)

    # UID 1 now means the new message, and the folder shows exactly that one.
    assert dbfixture.location_count(email, "INBOX") == 1
    assert _mb(email, "INBOX")["total"] == 1
    # The old message's content is still held, ready to be re-placed by the
    # re-walk rather than re-downloaded.
    assert dbfixture.message_count(email) == 2


def test_removed_folders_and_their_orphaned_messages_are_pruned(account):
    email = account["email"]
    raw = make_message(f"<gone-{uuid.uuid4().hex}@t>", "Gone folder", "x@y.com", email,
                       "gone body", T0)
    dbfixture.ingest_raw_message(email, raw, uid=1, folder="Old Folder")

    assert dbfixture.prune_folders(email, {"INBOX"}) == 1
    assert dbfixture.location_count(email, "Old Folder") == 0
    assert dbfixture.message_count(email) == 0


def test_a_server_that_lists_nothing_prunes_nothing(account):
    """An empty LIST is not "the user deleted all their folders".

    Bridge answers LIST from whatever it has loaded, so a Bridge that is still
    starting, signed out, or on a machine that has been offline for days answers
    it with nothing. Acting on that would delete every folder for the account
    and, with the last placement of each message, the mail itself.
    """
    email = account["email"]
    raw = make_message(f"<keep-{uuid.uuid4().hex}@t>", "Still here", "x@y.com", email,
                       "body", T0)
    dbfixture.ingest_raw_message(email, raw, uid=1, folder="Archive")

    assert dbfixture.prune_folders(email, set()) == 0
    assert dbfixture.location_count(email, "Archive") == 1
    assert dbfixture.message_count(email) == 1


def test_unknown_account_is_autoregistered(require_server):
    """First contact from an agent creates the account, so it appears in the UI."""
    email = f"auto-{uuid.uuid4().hex[:10]}@example.com"
    raw = make_message(f"<auto-{uuid.uuid4().hex}@t>", "Hello", "x@y.com", email, "body", T0)
    dbfixture.ingest_raw_message(email, raw, uid=1)

    code, accounts = api("GET", "/api/accounts")
    assert code == 200
    acc = next((a for a in accounts if a["email"] == email), None)
    assert acc is not None, "ingest should have created the account"
    assert acc["label"] == email.split("@")[0]
    api("DELETE", f"/api/accounts/{acc['id']}")


def test_attachment_text_is_extracted_and_searchable(account):
    """Tika extraction runs in the agent and feeds the search index."""
    email, aid = account["email"], account["id"]
    token = "TIKATOKEN" + uuid.uuid4().hex[:6]
    mid = f"att-{uuid.uuid4().hex}@t"
    raw = make_message(f"<{mid}>", "Has attachment", "x@y.com", email, "see attached", T0,
                       text_attachment=f"{token} lives inside the attachment".encode())
    dbfixture.ingest_raw_message(email, raw, uid=1)

    assert dbfixture.extract_all() >= 1

    _, sr = api("GET", f"/api/search?q={token}&account_id={aid}")
    assert sr["total"] == 1, sr
    assert sr["rows"][0]["subject"] == "Has attachment"


def test_tika_failure_leaves_attachment_pending_for_retry(account, monkeypatch):
    email = account["email"]
    raw = make_message(f"<retry-{uuid.uuid4().hex}@t>", "Retry extraction", "x@y.com", email,
                       "body", T0, text_attachment=b"retry me")
    dbfixture.ingest_raw_message(email, raw, uid=1)
    monkeypatch.setattr(ingest.tika, "extract_text", lambda *_a, **_kw: None)

    assert dbfixture.extract_all() == 0
    attachment = next(a for a in dbfixture.attachment_rows(email)
                      if a["filename"] == "notes.txt")
    assert attachment["extract_status"] == "pending"


def test_pending_attachment_count_sizes_the_indexing_queue(account):
    """The agent's "indexing N attachment(s)" line counts real queued work."""
    email = account["email"]
    raw = make_message(f"<queued-{uuid.uuid4().hex}@t>", "Queued for indexing",
                       "x@y.com", email, "body", T0, text_attachment=b"index me")
    dbfixture.ingest_raw_message(email, raw, uid=1)

    with dbfixture.session() as db:
        assert ingest.pending_attachment_count(db, "extract") >= 1

    dbfixture.extract_all()
    dbfixture.thumb_all()

    # Drained to zero, so a steady-state pass announces nothing at all.
    with dbfixture.session() as db:
        assert ingest.pending_attachment_count(db, "extract") == 0
        assert ingest.pending_attachment_count(db, "thumb") == 0


def test_previews_are_precomputed_for_pdfs_and_images(account):
    """The agent's thumbnail pass renders previews the read API then advertises."""
    pytest.importorskip("pymupdf", reason="preview rendering is an agent-side dep")
    pytest.importorskip("PIL", reason="preview rendering is an agent-side dep")

    email, aid = account["email"], account["id"]
    mid = f"thumb-{uuid.uuid4().hex}@t"
    raw = make_message(f"<{mid}>", "Has previewables", "x@y.com", email, "body", T0,
                       pdf_text="Quarterly report", png=True)
    dbfixture.ingest_raw_message(email, raw, uid=1)

    # Before the pass runs, the attachments are listed but have no preview.
    msg = _detail_by_subject(aid, "Has previewables")
    assert {a["filename"] for a in msg["attachments"]} == {"report.pdf", "photo.png"}
    assert all(a["has_thumb"] is False for a in msg["attachments"])

    assert dbfixture.thumb_all() >= 2

    msg = _detail_by_subject(aid, "Has previewables")
    for att in msg["attachments"]:
        assert att["has_thumb"] is True, att
        assert att["viewable"] is True, att
        code, body, headers = api_bytes(f"/api/attachments/{att['id']}/thumb")
        assert code == 200
        assert headers["Content-Type"] == "image/webp"
        # A WebP is "RIFF" + size + "WEBP"; assert the bytes really are one
        # rather than trusting the header we set ourselves.
        assert body[:4] == b"RIFF" and body[8:12] == b"WEBP"
        assert len(body) < 100_000, "a preview should be small"


def test_message_source_serves_the_original_bytes(account):
    """"View source" hands back exactly what was ingested, as inert text."""
    email, aid = account["email"], account["id"]
    mid = f"source-{uuid.uuid4().hex}@t"
    raw = make_message(f"<{mid}>", "Show me the source", "x@y.com", email,
                       "the body", T0, text_attachment=b"notes")
    dbfixture.ingest_raw_message(email, raw, uid=1)

    msg = _detail_by_subject(aid, "Show me the source")
    assert msg["has_source"] is True

    code, body, headers = api_bytes(f"/api/messages/{msg['id']}/source")
    assert code == 200
    assert body == raw
    # Rendered in the tab rather than downloaded, and never sniffed into
    # something the browser would execute on our own origin.
    assert headers["Content-Type"].startswith("text/plain")
    assert headers["Content-Disposition"].startswith("inline;")
    assert headers["X-Content-Type-Options"] == "nosniff"

    assert api("GET", "/api/messages/99999999/source")[0] == 404


def test_preview_pass_skips_types_it_cannot_render(account):
    """A text attachment is marked skipped, not left pending forever."""
    email, aid = account["email"], account["id"]
    mid = f"nothumb-{uuid.uuid4().hex}@t"
    raw = make_message(f"<{mid}>", "No previewables", "x@y.com", email, "body", T0,
                       text_attachment=b"just some notes")
    dbfixture.ingest_raw_message(email, raw, uid=1)
    dbfixture.thumb_all()

    msg = _detail_by_subject(aid, "No previewables")
    att = next(a for a in msg["attachments"] if a["filename"] == "notes.txt")
    assert att["has_thumb"] is False
    assert att["viewable"] is False
    assert api("GET", f"/api/attachments/{att['id']}/thumb")[0] == 404


def test_inline_disposition_is_allowlisted(account):
    """?inline=1 opens a PDF in a tab, but never a type the browser would script."""
    email, aid = account["email"], account["id"]
    mid = f"dispo-{uuid.uuid4().hex}@t"
    raw = make_message(f"<{mid}>", "Dispo check", "x@y.com", email, "body", T0,
                       pdf_text="Report", text_attachment=b"notes")
    dbfixture.ingest_raw_message(email, raw, uid=1)

    msg = _detail_by_subject(aid, "Dispo check")
    pdf = next(a for a in msg["attachments"] if a["filename"] == "report.pdf")
    txt = next(a for a in msg["attachments"] if a["filename"] == "notes.txt")

    # Default stays a download for everything.
    for att in (pdf, txt):
        _, _, h = api_bytes(f"/api/attachments/{att['id']}")
        assert h["Content-Disposition"].startswith("attachment;")

    # Allowlisted: renders in the tab.
    _, _, h = api_bytes(f"/api/attachments/{pdf['id']}?inline=1")
    assert h["Content-Disposition"].startswith("inline;")
    assert h["X-Content-Type-Options"] == "nosniff"

    # Not allowlisted: the request is ignored rather than honoured. text/plain is
    # the mild case; the same branch is what stops text/html and image/svg+xml
    # executing script on our own origin.
    _, _, h = api_bytes(f"/api/attachments/{txt['id']}?inline=1")
    assert h["Content-Disposition"].startswith("attachment;")


def test_inline_attachments_are_not_previewed(account):
    """Signature logos and tracking pixels must not queue rendering work."""
    email = account["email"]
    mid = f"inline-{uuid.uuid4().hex}@t"
    raw = make_message(f"<{mid}>", "Inline only", "x@y.com", email, "body", T0, png=True)
    # Re-label the part as inline, the way a signature image arrives.
    raw = raw.replace(b'Content-Disposition: attachment; filename="photo.png"',
                      b'Content-Disposition: inline; filename="photo.png"')
    dbfixture.ingest_raw_message(email, raw, uid=1)

    # Assert the part really did land as inline first — otherwise "no previews
    # were rendered" would pass for the wrong reason.
    rows = dbfixture.attachment_rows(email)
    png = next(r for r in rows if r["filename"] == "photo.png")
    assert png["is_inline"] is True
    assert png["thumb_status"] == "skipped"

    assert dbfixture.thumb_all() == 0


def test_sync_marks_backfill_complete(account):
    """The agent's end-of-pass report lands on the account row the UI reads."""
    email, aid = account["email"], account["id"]
    dbfixture.report_sync(email, backfill_complete=True)

    _, accounts = api("GET", "/api/accounts")
    acc = next(a for a in accounts if a["id"] == aid)
    assert acc["backfill_complete"] is True


def test_a_placement_arriving_after_the_read_stays_read(account):
    """A label the server hands over late must not resurrect read mail.

    Servers that file one message under several labels deliver each placement
    separately, so the second can land after the message has been read — the
    reader only marks the placements that existed when it ran. Taking the
    server's flags verbatim there put the mail back in the unread list seconds
    after it was opened.
    """
    email = account["email"]
    mid = f"late-{uuid.uuid4().hex}@t"
    raw = make_message(f"<{mid}>", "Read before the label landed", "x@y.com", email, "body", T0)
    dbfixture.ingest_raw_message(email, raw, uid=1)

    detail = _detail_by_subject(account["id"], "Read before the label landed")
    api("POST", f"/api/messages/{detail['id']}/mark?seen=true")
    assert _mb(email, "INBOX")["unread"] == 0

    # The second placement turns up now, and the server still calls it unseen —
    # our \Seen write-back has not been applied upstream yet.
    assert dbfixture.record_placement(email, mid, uid=900, folder="AllMail",
                                      flags={"seen": False}) is True

    assert _mb(email, "AllMail")["unread"] == 0
    assert _mb(email, "INBOX")["unread"] == 0

    # And the server is told, so the next reconcile sweep does not undo it.
    queued = dbfixture.pending_actions(email, "setflags")
    catchup = [a for a in queued if a["payload"].get("uid") == 900]
    assert len(catchup) == 1
    assert catchup[0]["payload"]["add"] == ["\\Seen"]


def test_an_unread_placement_stays_unread(account):
    """The inheritance only escalates — it must not mark anything read."""
    email = account["email"]
    mid = f"cold-{uuid.uuid4().hex}@t"
    raw = make_message(f"<{mid}>", "Never opened", "x@y.com", email, "body", T0)
    dbfixture.ingest_raw_message(email, raw, uid=2, flags={"seen": False})

    assert dbfixture.record_placement(email, mid, uid=901, folder="AllMail2",
                                      flags={"seen": False}) is True

    assert _mb(email, "AllMail2")["unread"] == 1
    assert not [a for a in dbfixture.pending_actions(email, "setflags")
                if a["payload"].get("uid") == 901]


def test_raw_mime_is_stored_by_default(account):
    email = account["email"]
    mid = f"kept-{uuid.uuid4().hex}@t"
    raw = make_message(f"<{mid}>", "Keeps its original", "x@y.com", email, "body", T0)
    dbfixture.ingest_raw_message(email, raw, uid=3)

    assert dbfixture.stored_raw_mime(email, mid) == raw


def test_store_raw_mime_off_drops_only_the_original_bytes(account, monkeypatch):
    """Everything the app reads is derived at ingest, so turning the copy off
    costs nothing but the copy — the message still lists, reads and searches."""
    email = account["email"]
    monkeypatch.setattr(get_settings(), "store_raw_mime", False)

    mid = f"lean-{uuid.uuid4().hex}@t"
    needle = f"haystack{uuid.uuid4().hex}"
    raw = make_message(f"<{mid}>", "No original kept", "x@y.com", email,
                       f"a body with {needle} in it", T0)
    dbfixture.ingest_raw_message(email, raw, uid=4)

    assert dbfixture.stored_raw_mime(email, mid) is None

    detail = _detail_by_subject(account["id"], "No original kept")
    assert needle in detail["body_text"]
    _, found = api("GET", f"/api/search?q={needle}&account_id={account['id']}")
    assert [r["subject"] for r in found["rows"]] == ["No original kept"]
