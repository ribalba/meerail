"""The content window: fetch headers only for old mail, and prune what ages out.

Two halves that have to agree — the agent decides from a message's date whether
to fetch the body at all, and the prune pass walks stored mail back to headers
as the window slides past it. Both land in the same state, which is what the
reader renders its notice from.
"""

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import pytest

import dbfixture
from core import ingest
from helpers import api, make_message

T0 = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)
# Older than anything else the suite ingests, so a global prune run against
# ANCIENT_CUTOFF can only match the mail the test in question put there.
ANCIENT = datetime(2001, 5, 1, 9, 0, tzinfo=timezone.utc)
ANCIENT_CUTOFF = datetime(2002, 1, 1, 0, 0)


def header_block(raw: bytes) -> bytes:
    """The headers plus their terminating blank line — what BODY.PEEK[HEADER]
    returns, and all the agent fetches for mail outside the window."""
    head, _, _ = raw.partition(b"\n\n")
    return head + b"\n\n"


def _detail(account_id: int, subject: str) -> dict:
    _, listing = api("GET", "/api/messages?scope=unified_inbox&limit=200")
    row = next(r for r in listing["rows"]
               if r["subject"] == subject and r["account_id"] == account_id)
    _, detail = api("GET", f"/api/messages/{row['id']}")
    return row, detail


def _search(account_id: int, q: str) -> list[str]:
    _, body = api("GET", "/api/search?" + urlencode({"q": q, "account_id": account_id}))
    return [r["subject"] for r in body["rows"]]


# --- The cutoff itself ------------------------------------------------------

def test_content_cutoff_counts_calendar_months():
    assert ingest.content_cutoff(0) is None
    assert ingest.content_cutoff(-3) is None

    now = ingest.utcnow()
    cutoff = ingest.content_cutoff(6)
    # Six calendar months back, not 180-odd days: the month lands exactly.
    months = (now.year * 12 + now.month) - (cutoff.year * 12 + cutoff.month)
    assert months == 6
    assert cutoff < now


def test_content_cutoff_clamps_to_a_shorter_month(monkeypatch):
    """31 March, one month back, is 28 February — not the 31st of nothing."""
    monkeypatch.setattr(ingest, "utcnow",
                        lambda: datetime(2026, 3, 31, 12, 0))
    assert ingest.content_cutoff(1) == datetime(2026, 2, 28, 12, 0)


# --- Headers-only ingest (mail that was old when we first saw it) -----------

def test_headers_only_ingest_lists_and_searches_without_a_body(account):
    email, aid = account["email"], account["id"]
    mid = f"old-{uuid.uuid4().hex}@t"
    needle = f"bodyword{uuid.uuid4().hex[:8]}"
    raw = make_message(f"<{mid}>", "Ancient thread", "sender@y.com", email,
                       f"a body containing {needle}", T0, text_attachment="notes")

    dbfixture.ingest_header_block(email, header_block(raw), uid=1, size_bytes=len(raw))

    row, detail = _detail(aid, "Ancient thread")
    # The envelope survived in full — this is a real row, not a stub.
    assert detail["from_addr"] == "sender@y.com"
    assert detail["recipients"]["to"][0]["address"] == email
    assert detail["date"].startswith("2026-05-01")
    assert detail["content_status"] == "skipped"
    assert detail["body_text"] == "" and detail["body_html"] == ""
    assert detail["attachments"] == []
    # The size is the server's, not the size of the headers we actually hold.
    assert row["content_status"] == "skipped"

    # Searchable by envelope, and only by envelope: the body was never here.
    assert _search(aid, "Ancient thread") == ["Ancient thread"]
    assert _search(aid, needle) == []

    assert dbfixture.stored_raw_mime(email, mid) is None


def test_a_later_full_fetch_fills_in_a_skipped_message(account):
    """Widening the window and rechecking is meant to bring the body back."""
    email, aid = account["email"], account["id"]
    mid = f"grow-{uuid.uuid4().hex}@t"
    needle = f"laterword{uuid.uuid4().hex[:8]}"
    raw = make_message(f"<{mid}>", "Filled in later", "sender@y.com", email,
                       f"the body says {needle}", T0, text_attachment="notes")

    dbfixture.ingest_header_block(email, header_block(raw), uid=2, size_bytes=len(raw))
    assert _detail(aid, "Filled in later")[1]["content_status"] == "skipped"

    # The same UID re-walked by a recheck, this time fetched in full.
    dbfixture.ingest_raw_message(email, raw, uid=2)

    _, detail = _detail(aid, "Filled in later")
    assert detail["content_status"] == "full"
    assert needle in detail["body_text"]
    assert [a["filename"] for a in detail["attachments"]] == ["notes.txt"]
    assert _search(aid, needle) == ["Filled in later"]
    # And no second copy of the message came of it.
    assert dbfixture.message_count(email) == 1


# --- Pruning (mail that has aged out since we stored it) --------------------

def test_prune_strips_stored_content_but_keeps_the_message(account):
    email, aid = account["email"], account["id"]
    needle = f"prunedword{uuid.uuid4().hex[:8]}"
    # Deliberately ancient, with a cutoff to match: prune_expired_content is
    # global (the agent runs it over every account at once), so a cutoff inside
    # the range the rest of the suite dates its fixtures from would reach across
    # tests and strip their mail too.
    old = make_message(f"<{uuid.uuid4().hex}@t>", "Aged out", "sender@y.com", email,
                       f"a body with {needle}", ANCIENT, text_attachment="notes")
    recent = make_message(f"<{uuid.uuid4().hex}@t>", "Still fresh", "sender@y.com", email,
                          f"also mentions {needle}", T0)
    dbfixture.ingest_raw_message(email, old, uid=10)
    dbfixture.ingest_raw_message(email, recent, uid=11)

    pruned = dbfixture.prune_content(ANCIENT_CUTOFF)
    assert pruned == 1

    _, gone = _detail(aid, "Aged out")
    assert gone["content_status"] == "pruned"
    assert gone["body_text"] == "" and gone["body_html"] == ""
    # The attachment is still named and sized, but is no longer downloadable.
    assert [a["filename"] for a in gone["attachments"]] == ["notes.txt"]
    assert gone["attachments"][0]["stored"] is False
    code, _ = api("GET", f"/api/attachments/{gone['attachments'][0]['id']}")
    assert code == 404

    # Only the pruned message left the search corpus.
    assert _search(aid, needle) == ["Still fresh"]
    assert _search(aid, "Aged out") == ["Aged out"]

    _, kept = _detail(aid, "Still fresh")
    assert kept["content_status"] == "full" and needle in kept["body_text"]

    # Idempotent: a second pass has nothing left to do.
    assert dbfixture.prune_content(ANCIENT_CUTOFF) == 0


def test_prune_leaves_undated_mail_alone(account):
    """No Date header means no age — and no grounds for throwing content away."""
    email, aid = account["email"], account["id"]
    raw = make_message(f"<{uuid.uuid4().hex}@t>", "No date at all", "sender@y.com", email,
                       "still has a body", ANCIENT)
    raw = b"\n".join(l for l in raw.split(b"\n") if not l.startswith(b"Date:"))
    dated = make_message(f"<{uuid.uuid4().hex}@t>", "Dated and ancient", "sender@y.com", email,
                         "this one goes", ANCIENT)
    dbfixture.ingest_raw_message(email, raw, uid=12)
    dbfixture.ingest_raw_message(email, dated, uid=13)

    # The same pass that strips its dated neighbour leaves this one whole.
    assert dbfixture.prune_content(ANCIENT_CUTOFF) == 1
    _, detail = _detail(aid, "No date at all")
    assert detail["content_status"] == "full"
    assert "still has a body" in detail["body_text"]
    assert _detail(aid, "Dated and ancient")[1]["content_status"] == "pruned"


def test_the_window_the_agent_applies_reaches_the_reader(account):
    """The app cannot read agent/config.toml, so the agent publishes the number."""
    email, aid = account["email"], account["id"]
    raw = make_message(f"<{uuid.uuid4().hex}@t>", "Explain yourself", "sender@y.com", email,
                       "body", T0)
    dbfixture.ingest_header_block(email, header_block(raw), uid=13, size_bytes=len(raw))
    dbfixture.set_content_window(18)

    _, detail = _detail(aid, "Explain yourself")
    assert detail["content_window_months"] == 18

    # Full messages carry no window: there is nothing to explain, and looking it
    # up per message would be a settings read on every mail anyone opens.
    dbfixture.ingest_raw_message(email, make_message(
        f"<{uuid.uuid4().hex}@t>", "Nothing to explain", "sender@y.com", email, "body", T0), uid=14)
    _, full = _detail(aid, "Nothing to explain")
    assert full["content_status"] == "full"
    assert full["content_window_months"] == 0


# --- The agent's side of the decision ---------------------------------------

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))


@pytest.mark.parametrize("header, internal, expected", [
    # The Date header wins: it is what the rest of meerail sorts on.
    (b"Date: Tue, 5 May 2026 09:00:00 +0000\r\n\r\n", datetime(2020, 1, 1),
     datetime(2026, 5, 5, 9, 0)),
    # ...normalised to naive UTC, so it compares against a cutoff.
    (b"Date: Tue, 5 May 2026 11:00:00 +0200\r\n\r\n", None,
     datetime(2026, 5, 5, 9, 0)),
    # No Date, or one nothing can parse: fall back to the server's timestamp.
    (b"Subject: no date here\r\n\r\n", datetime(2026, 5, 5, 9, 0),
     datetime(2026, 5, 5, 9, 0)),
    (b"Date: not a date at all\r\n\r\n", datetime(2026, 5, 5, 9, 0),
     datetime(2026, 5, 5, 9, 0)),
    # Neither: unknown age, which the caller reads as "fetch it in full".
    (b"Subject: nothing to go on\r\n\r\n", None, None),
])
def test_sent_date_prefers_the_date_header(header, internal, expected):
    import imap  # noqa: PLC0415 — needs the sys.path line above

    assert imap._sent_date(header, internal) == expected
