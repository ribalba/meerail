"""Integration tests for the ingest pipeline the agent owns.

These drive `core.ingest` directly against the database — the same calls the
agent's sync loop makes — and assert the results through the server's read APIs.
(Formerly test_agent_protocol.py, which drove the deleted /api/agent/* HTTP API.)
"""

import io
import sys
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import dbfixture
from core import ingest
from core.config import get_settings
from core.mail.parse import parse_email
from conftest import status_for
from helpers import api, api_bytes, make_message

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
import sync as agent_sync

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


def test_re_applying_the_same_flags_reports_nothing_changed(account):
    """The reconcile sweep pushes every UID's flags every time it runs, so on a
    mailbox nobody has touched the answer must be "nothing happened".

    It used to count the rows it *matched*, which on a quiet folder is all of
    them — one "flags" event per chunk with nothing behind it, and every one of
    those costs each connected client a full reload. A 35k-message folder did it
    ~1400 times a sweep. The count is the published/not-published decision, so
    asserting on it is asserting on the event.
    """
    email = account["email"]
    raw = make_message(f"<{uuid.uuid4().hex}@t>", "Flags", "x@y.com", email, "b", T0)
    dbfixture.ingest_raw_message(email, raw, uid=1)

    flags = {"uid": 1, "flags": {"seen": True, "keywords": ["$label1", "\\Junk"]}}
    assert dbfixture.set_flags(email, "INBOX", [flags]) == 1     # a real change
    assert dbfixture.set_flags(email, "INBOX", [flags]) == 0     # ...and then nothing
    assert _mb(email, "INBOX")["unread"] == 0

    # Keyword order is the server's business, not a change.
    reordered = {"uid": 1, "flags": {"seen": True, "keywords": ["\\Junk", "$label1"]}}
    assert dbfixture.set_flags(email, "INBOX", [reordered]) == 0

    # A UID the folder holds no row for is skipped, not counted.
    assert dbfixture.set_flags(email, "INBOX", [{"uid": 999, "flags": {"seen": True}}]) == 0

    # ...but a genuine change still lands, and still moves the unread count.
    assert dbfixture.set_flags(email, "INBOX", [{"uid": 1, "flags": {"seen": False}}]) == 1
    assert _mb(email, "INBOX")["unread"] == 1


def test_two_different_messages_sharing_a_message_id_stay_two_messages(account):
    """A Message-ID is a header the sender writes, not a fact about the message.

    Mail is stored once per Message-ID because a Proton or Gmail account hands
    the same message over once per label — but a mailer with a broken generator,
    a list that re-sends under the old id, or anybody who simply typed the header
    produces two different messages wearing one id. Believing it merged them: the
    second was never fetched, its UID was hung off the first message's row, and
    the reader showed March's invoice where April's had arrived.
    """
    email, aid = account["email"], account["id"]
    mid = f"clash-{uuid.uuid4().hex}@t"
    march = make_message(f"<{mid}>", "Invoice MARCHTOK", "billing@vendor.example", email,
                         "Amount due: 100", T0)
    april = make_message(f"<{mid}>", "Invoice APRILTOK", "billing@vendor.example", email,
                         "Amount due: 999999", T0 + timedelta(days=30))
    dbfixture.ingest_raw_message(email, march, uid=1)
    dbfixture.ingest_raw_message(email, april, uid=2)

    assert dbfixture.message_count(email) == 2
    # And each one is itself, body and all — not the other one seen twice.
    for token, amount in (("MARCHTOK", "100"), ("APRILTOK", "999999")):
        _, found = api("GET", f"/api/search?q={token}&account_id={aid}")
        assert found["rows"], token
        _, detail = api("GET", f"/api/messages/{found['rows'][0]['id']}")
        assert amount in detail["body_text"]


def test_a_collision_is_told_apart_by_its_content_not_by_its_headers(account):
    """The four headers a message carries are all written by its sender, so a
    sender who wants two different mails to look like one can have that. What
    they cannot forge is the message being the same message: the bytes are
    hashed, and two that hash differently are two.

    This is the pair that agrees on everything a header can say — id, sender,
    subject, and the second it was sent — and differs only in what it says.
    """
    email, aid = account["email"], account["id"]
    mid = f"forged-{uuid.uuid4().hex}@t"
    real = make_message(f"<{mid}>", "Your invoice FORGEDTOK", "billing@vendor.example",
                        email, "Pay 100 to account 111", T0)
    fake = make_message(f"<{mid}>", "Your invoice FORGEDTOK", "billing@vendor.example",
                        email, "Pay 100 to account 999", T0)

    dbfixture.ingest_raw_message(email, real, uid=1)
    dbfixture.ingest_raw_message(email, fake, uid=2)

    assert dbfixture.message_count(email) == 2
    bodies = set()
    _, found = api("GET", f"/api/search?q=FORGEDTOK&account_id={aid}")
    for row in found["rows"]:
        bodies.add(api("GET", f"/api/messages/{row['id']}")[1]["body_text"].strip())
    assert any("account 111" in b for b in bodies)
    assert any("account 999" in b for b in bodies)


def test_a_collision_does_not_join_the_other_message_s_conversation(account):
    """A thread_id is what "archive this conversation" acts on, and the id a
    root message threads under is its Message-ID. Two unrelated messages sharing
    one therefore became one conversation, and archiving either filed both —
    a stranger's message moved by an action aimed at yours."""
    email, aid = account["email"], account["id"]
    mid = f"threadclash-{uuid.uuid4().hex}@t"
    mine = make_message(f"<{mid}>", "Lunch on Friday THREADTOK", "friend@example.com",
                        email, "see you there", T0)
    theirs = make_message(f"<{mid}>", "Invoice overdue THREADTOK", "billing@vendor.example",
                          email, "pay up", T0 + timedelta(days=90))
    dbfixture.ingest_raw_message(email, mine, uid=1)
    dbfixture.ingest_raw_message(email, theirs, uid=2)

    _, found = api("GET", f"/api/search?q=THREADTOK&account_id={aid}")
    threads = {r["thread_id"] for r in found["rows"]}
    assert len(found["rows"]) == 2
    assert len(threads) == 2, "one conversation would move both at once"

    # And the one that is not the id: a thread action names a conversation, so
    # the collision must not be reachable through the other message's.
    _seed_folder(email, "Archive", "\\Archive")
    row = next(r for r in found["rows"] if "Lunch" in r["subject"])
    code, body = api("POST", f"/api/messages/threads/{row['thread_id']}/archive"
                             f"?account_id={aid}")
    assert code == 200 and body["moved"] == 1


def test_a_reply_naming_a_duplicated_id_does_not_merge_the_two_conversations(account):
    """The way the separation could be undone from outside.

    Threading joins a message to whatever its References name, and every thread
    it finds that way is *merged* — so one reply quoting an id that two messages
    wear used to weld their conversations back together, and a thread archive
    then filed both. An id that resolves to two conversations resolves to none:
    the reply threads on what else it has.
    """
    email, aid = account["email"], account["id"]
    mid = f"replyclash-{uuid.uuid4().hex}@t"
    dbfixture.ingest_raw_message(email, make_message(
        f"<{mid}>", "Lunch on Friday REPLYTOK", "friend@example.com", email, "see you", T0), uid=1)
    dbfixture.ingest_raw_message(email, make_message(
        f"<{mid}>", "Invoice overdue REPLYTOK", "billing@vendor.example", email, "pay up",
        T0 + timedelta(days=90)), uid=2)
    threads_before = _threads_for(aid, "REPLYTOK")
    assert len(threads_before) == 2

    # A reply to "the" message with that id — which is two messages.
    dbfixture.ingest_raw_message(email, make_message(
        f"<reply-{uuid.uuid4().hex}@t>", "Re: Lunch on Friday REPLYTOK", email,
        "friend@example.com", "yes please", T0 + timedelta(hours=2),
        in_reply_to=f"<{mid}>", refs=[f"<{mid}>"]), uid=3)

    assert len(_threads_for(aid, "REPLYTOK")) == 2, "the two roots are still two"


def test_rebuilding_threads_keeps_collisions_apart(account):
    """The rethread tool replays the threading rules over stored rows, so it has
    to replay this one too — otherwise the repair for one threading bug quietly
    reintroduces another."""
    email, aid = account["email"], account["id"]
    mid = f"rethreadclash-{uuid.uuid4().hex}@t"
    dbfixture.ingest_raw_message(email, make_message(
        f"<{mid}>", "Dentist RETHREADTOK", "surgery@example.com", email, "2pm", T0), uid=1)
    dbfixture.ingest_raw_message(email, make_message(
        f"<{mid}>", "Renewal RETHREADTOK", "billing@vendor.example", email, "due",
        T0 + timedelta(days=200)), uid=2)
    assert len(_threads_for(aid, "RETHREADTOK")) == 2

    assert dbfixture.rethread(aid)[0] >= 2

    assert len(_threads_for(aid, "RETHREADTOK")) == 2


def _threads_for(account_id: int, token: str) -> set:
    _, found = api("GET", f"/api/search?q={token}&account_id={account_id}")
    return {r["thread_id"] for r in found["rows"]}


def _seed_folder(email, folder, role_hint):
    """Give the account a folder to file into, as a real server has."""
    dbfixture.ingest_raw_message(email, make_message(
        f"<seed-{uuid.uuid4().hex}@t>", "Seed", "x@y.com", email, "seed", T0),
        uid=1, folder=folder, role_hint=role_hint)


class _Bridge:
    """The one thing _store_chunk asks a server for: raw messages, by UID.

    Enough of Bridge to drive the agent's own ingest path against the real
    database — which is the only place the decisions being tested here are
    actually made. Calling core.ingest directly, as the rest of this file does,
    walks past the code that decides whether to call it at all.
    """

    def __init__(self, raws: dict[int, bytes]):
        self.raws = raws
        self.fetched: list[int] = []

    def fetch_raw(self, uids):
        self.fetched.extend(uids)
        return {uid: {"raw": self.raws[uid], "flags": {}} for uid in uids if uid in self.raws}

    def fetch_header_block(self, uids):
        return {uid: {"raw": self.raws[uid].split(b"\r\n\r\n")[0] + b"\r\n\r\n", "flags": {}}
                for uid in uids if uid in self.raws}


def _walk(email: str, raws: dict[int, bytes], cutoff=None, folder: str = "INBOX",
          max_bytes: int = 0) -> _Bridge:
    """One chunk of a sync pass over these UIDs, through the agent's own code."""
    bridge = _Bridge(raws)
    with dbfixture.session() as db:
        account = ingest.get_or_create_account(db, email)
        mailbox = ingest.register_folder(db, account, folder, "", 1, None)
        headers = {}
        for uid, raw in raws.items():
            parsed = parse_email(raw)
            headers[uid] = {
                "message_id": parsed.message_id,
                "flags": {},
                "date": parsed.date_sent,
                "received": parsed.date_sent,     # as INTERNALDATE would say
                "size": len(raw),
                "headers": raw.split(b"\r\n\r\n")[0] + b"\r\n\r\n",
            }
        agent_sync._store_chunk(db, bridge, account, mailbox, headers, cutoff, email,
                                max_bytes)
    return bridge


def test_widening_the_content_window_brings_the_bodies_back(account):
    """The recovery the window is documented to have, through the code that
    actually performs it.

    Mail older than the window is stored as headers alone, and the promise is
    that widening the window and asking for a recheck fills the bodies in rather
    than being a one-way door. The pass never got that far: the re-walk
    recognised each headers-only message by its Message-ID, took the shortcut
    that exists so a Proton label costs no second download, and skipped the
    fetch. The code that fills a headers-only row in was unreachable from a sync.
    """
    email, aid = account["email"], account["id"]
    old = make_message(f"<window-{uuid.uuid4().hex}@t>", "Old but WINDOWTOK", "x@y.com",
                       email, "the body that was left behind", T0)
    # Naive UTC, as ingest.content_cutoff hands it to a pass, and late enough
    # that this message falls outside the window.
    narrow = (T0 + timedelta(days=365)).replace(tzinfo=None)

    _walk(email, {1: old}, cutoff=narrow)
    _, found = api("GET", f"/api/search?q=WINDOWTOK&account_id={aid}")
    assert found["rows"], "headers alone still list and search"
    detail = api("GET", f"/api/messages/{found['rows'][0]['id']}")[1]
    assert detail["body_text"] == ""       # nothing was fetched, by design

    # The window is widened and the folder re-walked: the same UID, the same
    # Message-ID, and this time the body is meant to arrive.
    bridge = _walk(email, {1: old}, cutoff=None)

    assert bridge.fetched == [1]
    detail = api("GET", f"/api/messages/{found['rows'][0]['id']}")[1]
    assert "the body that was left behind" in detail["body_text"]
    assert dbfixture.message_count(email) == 1     # filled in, not stored twice


def test_a_message_too_big_to_hold_is_stored_as_headers(account):
    """A fetch reads the whole message into memory before anything is parsed, so
    one mail carrying somebody's backup is that many bytes resident in an agent
    with a container limit — and the pass dies on the same UID every time it
    retries. Past the cap the message lands the way mail outside the content
    window does: every header, no body, still listed and searchable."""
    email, aid = account["email"], account["id"]
    raw = make_message(f"<huge-{uuid.uuid4().hex}@t>", "Subject HUGETOK", "x@y.com",
                       email, "a body nobody can hold", T0)

    bridge = _walk(email, {1: raw}, max_bytes=len(raw) - 1)

    assert bridge.fetched == []                       # the body never crossed the wire
    _, found = api("GET", f"/api/search?q=HUGETOK&account_id={aid}")
    assert found["rows"], "it still lists and searches by subject"
    detail = api("GET", f"/api/messages/{found['rows'][0]['id']}")[1]
    assert detail["body_text"] == ""

    # Raising the cap and re-walking brings it in, exactly as widening the
    # content window does.
    assert _walk(email, {1: raw}).fetched == [1]
    detail = api("GET", f"/api/messages/{found['rows'][0]['id']}")[1]
    assert "a body nobody can hold" in detail["body_text"]


def test_a_second_walk_stores_a_placement_and_not_a_second_copy(account):
    """Mail the account already holds costs a placement row and nothing else.

    It is still fetched — what a message *is* is decided from the message, not
    from a Message-ID its sender wrote — so the saving is in the database rather
    than on the wire. That is the trade this makes: a label server hands the same
    mail over once per label, and each copy after the first is downloaded again
    to prove it is the same one.
    """
    email = account["email"]
    raw = make_message(f"<again-{uuid.uuid4().hex}@t>", "Subject AGAINTOK", "x@y.com",
                       email, "body", T0)

    assert _walk(email, {1: raw}).fetched == [1]           # first time
    assert _walk(email, {9: raw}, folder="Label").fetched == [9]   # and under a label

    # One message, two placements — no duplicate content, and no second row.
    assert dbfixture.message_count(email) == 1
    assert dbfixture.location_count(email, "INBOX") == 1
    assert dbfixture.location_count(email, "Label") == 1


def test_a_collision_that_agrees_on_every_header_is_still_fetched(account):
    """The last version of this hole, closed.

    A pass used to be able to place a UID without downloading it, on the strength
    of its Message-ID, sender, subject, send time and byte count agreeing with
    something already held. Every one of those is written by whoever sent the
    message, so a pair engineered to agree on all five was taken to be one
    message and the second body was never fetched by anything — present on the
    server, absent from the mirror, with another message shown in its place.

    Narrowing it left the *first* such pair still merging, which is the pair that
    matters. So the bytes decide now, which means fetching them.
    """
    email, aid = account["email"], account["id"]
    mid = f"twinbytes-{uuid.uuid4().hex}@t"
    first = make_message(f"<{mid}>", "Statement TWINTOK", "bank@example.com", email,
                         "balance 100", T0)
    second = make_message(f"<{mid}>", "Statement TWINTOK", "bank@example.com", email,
                          "balance 999", T0)
    assert len(first) == len(second), "the point is that every number agrees too"

    assert _walk(email, {1: first}).fetched == [1]
    assert _walk(email, {2: second}, folder="Second").fetched == [2]

    assert dbfixture.message_count(email) == 2
    bodies = set()
    _, found = api("GET", f"/api/search?q=TWINTOK&account_id={aid}")
    for row in found["rows"]:
        bodies.add(api("GET", f"/api/messages/{row['id']}")[1]["body_text"].strip())
    assert any("balance 100" in b for b in bodies)
    assert any("balance 999" in b for b in bodies)


def test_the_no_fetch_shortcut_checks_more_than_the_message_id(account):
    """The same collision, in the path that never fetches at all.

    Most of a label server's walk resolves to "we already hold this, just place
    it", and that decision is made from the cheap header pass. Made on the
    Message-ID alone it hangs a UID off whatever row shares the id; the size and
    the date come free with the same pass and are what tell the two apart.
    """
    email = account["email"]
    mid = f"short-{uuid.uuid4().hex}@t"
    raw = make_message(f"<{mid}>", "Subject SHORTCUT", "x@y.com", email, "body", T0)
    dbfixture.ingest_raw_message(email, raw, uid=1)
    sent_at = T0.replace(tzinfo=None)

    # The same message under a second label: same id, same size, same instant.
    assert dbfixture.record_placement(email, mid, uid=201, folder="Label", role_hint="",
                                      size=len(raw), date=sent_at) is True
    assert dbfixture.message_count(email) == 1

    # A different message wearing the same id is not this one, and saying so is
    # what sends the agent off to fetch it properly.
    assert dbfixture.record_placement(email, mid, uid=202, folder="Label2",
                                      size=len(raw) + 4096, date=sent_at) is False
    assert dbfixture.record_placement(email, mid, uid=203, folder="Label3",
                                      size=len(raw), date=sent_at + timedelta(days=1)) is False


def test_the_shortcut_will_not_merge_two_messages_that_only_agree_on_numbers(account):
    """Same Message-ID, same second, same byte count — different mail.

    The cheap header pass used to fetch the Message-ID and the Date and nothing
    else, so those two numbers plus the size were the whole of the argument, and
    a pair agreeing on them was merged without either being read. The pass now
    asks for From and Subject in the same FETCH — free, next to a round trip —
    and the shortcut decides on what the full ingest would decide on.
    """
    email, aid = account["email"], account["id"]
    mid = f"twins-{uuid.uuid4().hex}@t"
    first = make_message(f"<{mid}>", "Subject AAA", "one@vendor.example", email,
                         "first body", T0)
    # Same length, same instant, same id, and nothing else the same.
    second = make_message(f"<{mid}>", "Subject BBB", "two@vendor.example", email,
                          "other body", T0)
    assert len(first) == len(second), "the point of the test is that the numbers agree"

    bridge = _walk(email, {1: first})
    bridge2 = _walk(email, {2: second}, folder="Other")

    assert bridge.fetched == [1] and bridge2.fetched == [2]   # neither was skipped
    assert dbfixture.message_count(email) == 2
    for token, body in (("AAA", "first body"), ("BBB", "other body")):
        _, found = api("GET", f"/api/search?q=Subject+{token}&account_id={aid}")
        assert len(found["rows"]) == 1, token
        assert body in api("GET", f"/api/messages/{found['rows'][0]['id']}")[1]["body_text"]


def test_content_the_window_pruned_comes_back_the_same_way(account):
    """A body stripped as the window slid past it is in the same position as one
    that was never fetched, and comes back by the same route — with its
    attachment rows replaced rather than doubled, which is the one difference
    between the two cases."""
    email, aid = account["email"], account["id"]
    raw = make_message(f"<pruned-{uuid.uuid4().hex}@t>", "Subject PRUNEDTOK", "x@y.com",
                       email, "body worth keeping", T0)
    _walk(email, {1: raw})
    # The window is measured from when the mail arrived, which _walk reports as
    # the message's own date — see dbfixture.ingest_raw_message.
    assert dbfixture.prune_content(cutoff=(T0 + timedelta(days=1)).replace(tzinfo=None)) >= 1

    _, found = api("GET", f"/api/search?q=PRUNEDTOK&account_id={aid}")
    message_id = found["rows"][0]["id"]
    assert api("GET", f"/api/messages/{message_id}")[1]["body_text"] == ""

    bridge = _walk(email, {1: raw})          # the window has been widened again

    assert bridge.fetched == [1]
    assert "body worth keeping" in api("GET", f"/api/messages/{message_id}")[1]["body_text"]


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


def test_a_move_of_mail_with_no_message_id_is_not_undone_by_the_repair(account):
    """The same question, about mail that cannot be asked by name.

    A Message-ID is optional, and mail without one really does arrive. "No id"
    was read as "never seen before, so never moved", so the repair put the
    message straight back into the folder it had just been archived out of, and
    it would not stay filed. There is more to go on than the id: the queue is
    short, and everything in it can be recognised by the headers this pass has
    already fetched.
    """
    email = account["email"]
    raw = make_message(None, "Subject NOIDTOK", "x@y.com", email, "body", T0)
    assert b"Message-ID" not in raw
    dbfixture.ingest_raw_message(email, raw, uid=1)
    dbfixture.create_folder(email, "Archive", role_hint="\\Archive")
    headers = raw.split(b"\r\n\r\n")[0] + b"\r\n\r\n"

    _, boxes = api("GET", "/api/mailboxes")
    mine = next(a for a in boxes["accounts"] if a["email"] == email)["mailboxes"]
    inbox = next(m for m in mine if m["role"] == "inbox")
    _, listing = api("GET", f"/api/messages?mailbox_id={inbox['id']}&limit=50")
    message_id = next(r["id"] for r in listing["rows"] if r["subject"] == "Subject NOIDTOK")
    assert api("POST", f"/api/messages/{message_id}/archive"
                       f"?source_mailbox_id={inbox['id']}")[0] == 200

    assert dbfixture.unplaced_uids(email, "INBOX", [1]) == [1]      # the server still lists it
    assert dbfixture.move_in_flight(email, None, headers=headers) is True

    # And once the move has landed and settled, it stops holding the repair off —
    # the same end state as for mail that has an id.
    dbfixture.apply_actions(email, minutes_ago=60)
    assert dbfixture.move_in_flight(email, None, headers=headers) is False


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


def test_mail_left_behind_by_a_reused_uid_is_collected_at_the_end_of_a_pass(account):
    """The other side of the same trade.

    Keeping the old message through the re-walk is what makes a UIDVALIDITY
    change cost nothing — the mail that is still on the server gets its new
    placement instead of being downloaded again. But the walk finishes, and
    whatever it did not re-place is a message no folder points at: not listed,
    not counted, not reachable, and still holding every byte it arrived with. On
    a mailbox that has been through a few Bridge re-logins that is a leak of
    whole messages.

    So it is collected — at the end of a pass that completed, over messages that
    were already unplaced well before that pass began. Both halves matter: a
    message is legitimately between folders mid-pass, and answering that question
    too early turns a gap of seconds into mail nobody can get back.
    """
    email = account["email"]
    kept = make_message(f"<kept-{uuid.uuid4().hex}@t>", "Still placed", "x@y.com", email,
                        "body", T0)
    dropped = make_message(f"<dropped-{uuid.uuid4().hex}@t>", "Orphaned", "x@y.com", email,
                           "body", T0)
    dbfixture.ingest_raw_message(email, dropped, uid=1, uidvalidity=10)
    dbfixture.register_folder(email, "INBOX", uidvalidity=11)
    dbfixture.ingest_raw_message(email, kept, uid=1, uidvalidity=11)   # uid 1 now means this one
    assert dbfixture.message_count(email) == 2

    # Not on the way past: too soon to know that no folder is going to hold it.
    assert dbfixture.collect_orphans(email) == 0
    assert dbfixture.message_count(email) == 2

    assert dbfixture.collect_orphans(email, after_hours=12) == 1
    assert dbfixture.message_count(email) == 1
    # And the one a folder still holds is untouched, which is the whole point of
    # asking about placements rather than about age.
    _, found = api("GET", f"/api/search?q=Still+placed&account_id={account['id']}")
    assert len(found["rows"]) == 1


def test_a_message_a_queued_action_still_names_is_not_collected(account):
    """A move waiting for the agent is a message on its way somewhere, and the
    row it names has to be there when it arrives — the action points at it by
    primary key, and deleting the message would take the action with it."""
    email = account["email"]
    raw = make_message(f"<queued-{uuid.uuid4().hex}@t>", "On its way", "x@y.com", email,
                       "body", T0)
    dbfixture.ingest_raw_message(email, raw, uid=1)
    dbfixture.queue_move_for(email, "On its way")
    dbfixture.drop_placements(email, "INBOX")

    assert dbfixture.collect_orphans(email, after_hours=12) == 0
    assert dbfixture.message_count(email) == 1


def test_removed_folders_and_their_orphaned_messages_are_pruned(account):
    email = account["email"]
    raw = make_message(f"<gone-{uuid.uuid4().hex}@t>", "Gone folder", "x@y.com", email,
                       "gone body", T0)
    dbfixture.ingest_raw_message(email, raw, uid=1, folder="Old Folder")

    # Not on the first answer that leaves it out — see the test below.
    assert dbfixture.prune_folders(email, {"INBOX"}) == 0
    assert dbfixture.prune_folders(email, {"INBOX"}, after_hours=2) == 1
    assert dbfixture.location_count(email, "Old Folder") == 0
    assert dbfixture.message_count(email) == 0


def test_a_folder_missing_from_one_list_keeps_its_mail(account):
    """A partial LIST is a successful response containing a fraction of the
    mailbox, and Bridge gives one while it is still loading — three folders out
    of twelve. Nothing in the answer says which kind it is, so the folders it
    left out keep their mail and are marked instead; only staying gone removes
    them. A folder that comes back clears the mark and nothing is lost at all.
    """
    email = account["email"]
    raw = make_message(f"<partial-{uuid.uuid4().hex}@t>", "Still here", "x@y.com", email,
                       "body", T0)
    dbfixture.ingest_raw_message(email, raw, uid=1, folder="Projects")

    assert dbfixture.prune_folders(email, {"INBOX"}) == 0
    assert dbfixture.location_count(email, "Projects") == 1
    assert dbfixture.message_count(email) == 1
    # And the agent can say so: this is the state an operator needs to see while
    # it lasts, because it is also what a real deletion looks like for an hour.
    assert dbfixture.folders_held_back(email) == ["Projects"]

    # The next LIST is complete again: the mark goes, and the folder is no
    # nearer being pruned than it was before Bridge hiccupped.
    assert dbfixture.prune_folders(email, {"INBOX", "Projects"}) == 0
    assert dbfixture.folders_held_back(email) == []
    assert dbfixture.prune_folders(email, {"INBOX", "Projects"}, after_hours=2) == 0
    assert dbfixture.location_count(email, "Projects") == 1


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


def test_all_attachments_download_as_one_zip(account):
    """"Download all" hands back every stored attachment in a single archive."""
    email, aid = account["email"], account["id"]
    mid = f"zip-{uuid.uuid4().hex}@t"
    raw = make_message(f"<{mid}>", "Zip me up", "x@y.com", email, "body", T0,
                       pdf_text="Report", text_attachment=b"just some notes", png=True)
    dbfixture.ingest_raw_message(email, raw, uid=1)

    msg = _detail_by_subject(aid, "Zip me up")
    code, body, headers = api_bytes(f"/api/messages/{msg['id']}/attachments.zip")
    assert code == 200
    assert headers["Content-Type"] == "application/zip"
    assert headers["Content-Disposition"].startswith("attachment;")
    assert headers["X-Content-Type-Options"] == "nosniff"

    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        assert sorted(zf.namelist()) == ["notes.txt", "photo.png", "report.pdf"]
        assert zf.read("notes.txt") == b"just some notes"
        # The bytes in the archive are the bytes the single-file route serves,
        # so a zip is never a second, differently-mangled copy.
        pdf = next(a for a in msg["attachments"] if a["filename"] == "report.pdf")
        assert zf.read("report.pdf") == api_bytes(f"/api/attachments/{pdf['id']}")[1]

    # Nothing attached is not an error worth a zero-entry archive.
    plain = make_message(f"<plain-{uuid.uuid4().hex}@t>", "Nothing attached", "x@y.com",
                         email, "body", T0)
    dbfixture.ingest_raw_message(email, plain, uid=2)
    bare = _detail_by_subject(aid, "Nothing attached")
    assert api("GET", f"/api/messages/{bare['id']}/attachments.zip")[0] == 404
    assert api("GET", "/api/messages/99999999/attachments.zip")[0] == 404


def test_zip_entry_names_are_defanged_and_deduped():
    """A sender names the files; the archive is unpacked on someone else's disk."""
    from app.routers.messages import _zip_name

    taken: set[str] = set()
    # Flattened to one component and never starting with a dot: whatever a
    # tolerant unzip does with this, it does it inside the folder it was told to.
    assert _zip_name("../../.ssh/authorized_keys", taken) == "_.._.ssh_authorized_keys"
    assert _zip_name("invoice.pdf", taken) == "invoice.pdf"
    assert _zip_name("invoice.pdf", taken) == "invoice (2).pdf"
    assert _zip_name("INVOICE.PDF", taken) == "INVOICE (3).PDF"   # case-insensitive filesystems
    assert _zip_name("", taken) == "attachment"
    assert _zip_name("no-extension", taken) == "no-extension"
    assert _zip_name("no-extension", taken) == "no-extension (2)"


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


def test_inline_images_are_not_queued_for_ocr(account):
    """The same logos and pixels, and the same reason: extraction OCRs images.

    Every inline image is a Tesseract round trip in the Tika container, seconds
    apiece, over a picture nobody searches for — and a mailbox holds tens of
    thousands of them. Queueing them buried real documents behind hours of
    signature logos, on a queue that is shared by every account.
    """
    email = account["email"]
    mid = f"ocr-{uuid.uuid4().hex}@t"
    # A PDF *and* a PNG, both re-labelled inline the way Apple Mail sends them.
    raw = make_message(f"<{mid}>", "Inline both", "x@y.com", email, "body", T0,
                       pdf_text="INLINEDOC", png=True)
    raw = raw.replace(b'Content-Disposition: attachment; filename="photo.png"',
                      b'Content-Disposition: inline; filename="photo.png"')
    raw = raw.replace(b'Content-Disposition: attachment; filename="report.pdf"',
                      b'Content-Disposition: inline; filename="report.pdf"')
    dbfixture.ingest_raw_message(email, raw, uid=1)

    rows = {r["filename"]: r for r in dbfixture.attachment_rows(email)}
    assert rows["photo.png"]["is_inline"] is True
    assert rows["photo.png"]["extract_status"] == "skipped"
    # The document is inline too, and stays queued: Apple Mail sends genuine
    # attachments with Content-Disposition: inline, and a PDF's text is exactly
    # what search wants. Only the OCR types are declined.
    assert rows["report.pdf"]["is_inline"] is True
    assert rows["report.pdf"]["extract_status"] == "pending"


def test_attached_images_are_still_queued_for_ocr(account):
    """The opposite case, so the rule above cannot quietly become "no images"."""
    email = account["email"]
    mid = f"ocr2-{uuid.uuid4().hex}@t"
    raw = make_message(f"<{mid}>", "Attached photo", "x@y.com", email, "body", T0,
                       png=True)
    dbfixture.ingest_raw_message(email, raw, uid=1)

    png = next(r for r in dbfixture.attachment_rows(email) if r["filename"] == "photo.png")
    assert png["is_inline"] is False
    assert png["extract_status"] == "pending"


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
