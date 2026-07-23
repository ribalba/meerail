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


def test_uidvalidity_change_replaces_stale_uid_placements(account):
    email = account["email"]
    old = make_message(f"<old-{uuid.uuid4().hex}@t>", "Old UID epoch", "x@y.com", email,
                       "old body", T0)
    new = make_message(f"<new-{uuid.uuid4().hex}@t>", "New UID epoch", "x@y.com", email,
                       "new body", T0)
    dbfixture.ingest_raw_message(email, old, uid=1, uidvalidity=10)
    dbfixture.ingest_raw_message(email, new, uid=1, uidvalidity=11)

    assert dbfixture.location_count(email, "INBOX") == 1
    assert dbfixture.message_count(email) == 1
    assert _mb(email, "INBOX")["total"] == 1


def test_removed_folders_and_their_orphaned_messages_are_pruned(account):
    email = account["email"]
    raw = make_message(f"<gone-{uuid.uuid4().hex}@t>", "Gone folder", "x@y.com", email,
                       "gone body", T0)
    dbfixture.ingest_raw_message(email, raw, uid=1, folder="Old Folder")

    assert dbfixture.prune_folders(email, {"INBOX"}) == 1
    assert dbfixture.location_count(email, "Old Folder") == 0
    assert dbfixture.message_count(email) == 0


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
