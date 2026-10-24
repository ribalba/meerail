"""One mail in two accounts: the lists show it once and every verb reaches both.

The case is ordinary. A sender writes to one address and BCCs another, both are
accounts here, and meerail stores the mail twice, once per account. Each account
inbox is right to show it. The lists that already mix accounts (All Inboxes,
Parked, Reminders) used to show two rows side by side, and the reader opened
half of it. See app/spanning.py for the two groupings this tests: conversations
merge by thread id across accounts, and twins (the same mail delivered twice)
collapse to one card in the reader and one row in Flagged.

Every delivery here differs in its Received and Delivered-To headers, so the two
copies never share a content hash, exactly as two real deliveries do not.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from types import SimpleNamespace

import pytest

import dbfixture
from app import spanning
from conftest import T0, mailbox
from core.models import Message
from helpers import api

SENDER = "news@sender.example"


# --- seeding ------------------------------------------------------------------


@pytest.fixture
def pair(account):
    """Two accounts on the same install, the second deleted on teardown."""
    email = f"pytest-{uuid.uuid4().hex[:10]}@example.com"
    other = dbfixture.create_account(email, label="pytest two")
    yield account, other
    api("DELETE", f"/api/accounts/{other['id']}")


def _delivery(mid: str, to_addr: str, when: datetime, subject: str,
              refs: list[str] | None = None, body: str = "Hello there") -> bytes:
    """One delivery of a mail, with the trace headers the receiving server adds."""
    m = EmailMessage()
    m["Received"] = (f"from mail.sender.example by mx.{to_addr.split('@')[0]}.example "
                     f"with ESMTPS id {uuid.uuid4().hex[:12]}; {format_datetime(when)}")
    m["Delivered-To"] = to_addr
    m["Message-ID"] = f"<{mid}>"
    m["Subject"] = subject
    m["From"] = f"Sender <{SENDER}>"
    m["To"] = "arne@datafuse.example"
    m["Date"] = format_datetime(when)
    if refs:
        m["In-Reply-To"] = f"<{refs[-1]}>"
        m["References"] = " ".join(f"<{r}>" for r in refs)
    m.set_content(body)
    return m.as_bytes()


def _deliver(acc: dict, mid: str, uid: int, when: datetime = T0, subject: str = "Report",
             refs: list[str] | None = None, folder: str = "INBOX") -> int:
    dbfixture.ingest_raw_message(acc["email"], _delivery(mid, acc["email"], when, subject, refs),
                                 uid=uid, folder=folder)
    return dbfixture.message_pk_for(acc["email"], mid)


def _deliver_both(a: dict, b: dict, uid: int = 1, when: datetime = T0, subject: str = "Report",
                  refs: list[str] | None = None, mid: str | None = None):
    """The same mail into both inboxes. Returns (mid, pk in a, pk in b)."""
    mid = mid or f"xa-{uuid.uuid4().hex}@sender.example"
    return mid, _deliver(a, mid, uid, when, subject, refs), _deliver(b, mid, uid, when, subject, refs)


def _folders(acc: dict, *names: str) -> None:
    for name in names:
        dbfixture.create_folder(acc["email"], name, "\\" + name)


def _thread_of(pk: int) -> str:
    code, body = api("GET", f"/api/messages/{pk}")
    assert code == 200, body
    return body["thread_id"]


def _rows(params: str) -> list[dict]:
    code, body = api("GET", f"/api/messages?{params}&limit=1000")
    assert code == 200, body
    return body["rows"]


def _rows_for(params: str, thread_id: str) -> list[dict]:
    return [r for r in _rows(params) if r["thread_id"] == thread_id]


def _inbox(acc: dict) -> int:
    return mailbox(acc["email"], "inbox")["id"]


def _live_folders(acc: dict, pk: int) -> list[str]:
    return sorted(p["imap_name"] for p in dbfixture.placements(acc["email"], pk)
                  if not p["deleted"])


def _own(row: dict) -> dict:
    return {"account_id": row["account_id"], "thread_id": row["thread_id"], "id": row["id"],
            "mailbox_id": row["mailbox_id"], "account_color": row["account_color"]}


def _soon(**kw) -> str:
    return (datetime.now(timezone.utc) + timedelta(**kw)).isoformat()


def _thread(thread_id: str, *account_ids: int, extra: str = ""):
    query = "&".join(f"account_id={a}" for a in account_ids)
    return api("GET", f"/api/threads/{thread_id}?{query}{extra}")


# --- the list -----------------------------------------------------------------


def test_all_inboxes_shows_the_mail_once_and_each_account_inbox_still_shows_it(pair):
    a, b = pair
    _, pa, pb = _deliver_both(a, b)
    thread = _thread_of(pa)
    assert _thread_of(pb) == thread

    [row] = _rows_for("scope=unified_inbox", thread)
    assert len(row["members"]) == 2
    # Twins share their date, so the lower account speaks for the row, and its
    # own entry comes first and equals the row.
    assert row["account_id"] == min(a["id"], b["id"])
    assert row["members"][0] == _own(row)
    other = row["members"][1]
    rest = b if row["account_id"] == a["id"] else a
    assert other["account_id"] == rest["id"]
    assert other["id"] == (pb if rest is b else pa)
    assert other["thread_id"] == thread
    assert other["mailbox_id"] == _inbox(rest)
    assert other["account_color"]

    for acc, pk in ((a, pa), (b, pb)):
        [own] = _rows_for(f"mailbox_id={_inbox(acc)}", thread)
        assert own["id"] == pk
        assert own["members"] == [_own(own)]


def test_a_row_that_merged_nothing_has_one_member_equal_to_itself(pair):
    a, _ = pair
    pk = _deliver(a, f"solo-{uuid.uuid4().hex}@sender.example", uid=1)
    [row] = _rows_for("scope=unified_inbox", _thread_of(pk))
    assert row["members"] == [_own(row)]
    assert row["thread_count"] == 1


def test_the_newest_message_speaks_for_a_merged_conversation(pair):
    """Representative first, whichever account it is in, then the others by account."""
    a, b = pair
    root, ra, rb = _deliver_both(a, b, uid=1)
    later = T0 + timedelta(hours=2)
    reply = f"re-{uuid.uuid4().hex}@sender.example"
    newest = _deliver(b, reply, uid=2, when=later, subject="Re: Report", refs=[root])
    thread = _thread_of(ra)

    [row] = _rows_for("scope=unified_inbox", thread)
    assert row["id"] == newest and row["account_id"] == b["id"]
    assert row["members"][0] == _own(row)
    assert row["members"][1] == {"account_id": a["id"], "thread_id": thread, "id": ra,
                                 "mailbox_id": _inbox(a), "account_color": row["members"][1]["account_color"]}


def test_a_shared_message_id_with_a_different_date_is_not_a_twin(pair):
    """A sender that reuses a Message-ID must not hide one mail behind another."""
    a, b = pair
    mid = f"reused-{uuid.uuid4().hex}@sender.example"
    pa = _deliver(a, mid, uid=1, when=T0)
    pb = _deliver(b, mid, uid=1, when=T0 + timedelta(hours=1))
    thread = _thread_of(pa)

    code, body = _thread(thread, a["id"], b["id"])
    assert code == 200, body
    assert sorted(m["id"] for m in body["messages"]) == sorted([pa, pb])
    assert all(m["twins"] == [] for m in body["messages"])

    # Still one conversation in All Inboxes (the thread id is the same), but it
    # holds two mails.
    [row] = _rows_for("scope=unified_inbox", thread)
    assert row["thread_count"] == 2
    assert len(row["members"]) == 2

    assert api("POST", f"/api/messages/{pa}/flag?flagged=1")[0] == 200
    assert api("POST", f"/api/messages/{pb}/flag?flagged=1")[0] == 200
    assert sorted(r["id"] for r in _rows_for("scope=flagged", thread)) == sorted([pa, pb])


def test_unthreaded_messages_never_merge(pair):
    a, b = pair
    _, pa, pb = _deliver_both(a, b)
    with dbfixture.session() as db:
        for pk in (pa, pb):
            db.get(Message, pk).thread_id = None

    rows = [r for r in _rows("scope=unified_inbox") if r["id"] in (pa, pb)]
    assert sorted(r["id"] for r in rows) == sorted([pa, pb])
    for row in rows:
        assert row["thread_id"] is None
        assert row["members"] == [_own(row)]


def test_thread_count_counts_a_twin_once(pair):
    a, b = pair
    root, ra, rb = _deliver_both(a, b, uid=1)
    _deliver_both(a, b, uid=2, when=T0 + timedelta(hours=1), subject="Re: Report", refs=[root])
    thread = _thread_of(ra)

    [row] = _rows_for("scope=unified_inbox", thread)
    assert row["thread_count"] == 2
    for acc in (a, b):
        [own] = _rows_for(f"mailbox_id={_inbox(acc)}", thread)
        assert own["thread_count"] == 2

    # A reply only one account got is one more mail, counted once.
    _deliver(b, f"only-{uuid.uuid4().hex}@sender.example", uid=3,
             when=T0 + timedelta(hours=2), subject="Re: Report", refs=[root])
    [row] = _rows_for("scope=unified_inbox", thread)
    assert row["thread_count"] == 3
    assert _rows_for(f"mailbox_id={_inbox(a)}", thread)[0]["thread_count"] == 2
    assert _rows_for(f"mailbox_id={_inbox(b)}", thread)[0]["thread_count"] == 3


def _badges(a: dict, b: dict) -> tuple[int, int, int]:
    """All Inboxes' unread badge, and each account's own Inbox badge."""
    code, body = api("GET", "/api/mailboxes")
    assert code == 200, body
    inbox = {acc["id"]: next(mb["unread"] for mb in acc["mailboxes"] if mb["role"] == "inbox")
             for acc in body["accounts"] if acc["id"] in (a["id"], b["id"])}
    return body["smart"]["unified_inbox_unread"], inbox[a["id"]], inbox[b["id"]]


def test_the_all_inboxes_badge_counts_a_twin_once(pair):
    # Deltas, because the badge spans every account on the install and other
    # tests leave mail behind in theirs.
    a, b = pair
    _deliver(a, f"seed-{uuid.uuid4().hex}@sender.example", uid=1)
    _deliver(b, f"seed-{uuid.uuid4().hex}@sender.example", uid=1)
    unified, in_a, in_b = _badges(a, b)

    _, pa, pb = _deliver_both(a, b, uid=2, when=T0 + timedelta(hours=1))
    assert _badges(a, b) == (unified + 1, in_a + 1, in_b + 1)

    # Still unread in one inbox, so still one unread mail in All Inboxes.
    assert api("POST", f"/api/messages/{pa}/mark?seen=1")[0] == 200
    assert _badges(a, b) == (unified + 1, in_a, in_b + 1)
    assert api("POST", f"/api/messages/{pb}/mark?seen=1")[0] == 200
    assert _badges(a, b) == (unified, in_a, in_b)


# --- the reader ---------------------------------------------------------------


def test_the_reader_shows_a_twin_once_and_reads_it_as_unread_until_both_are(pair):
    a, b = pair
    _, pa, pb = _deliver_both(a, b)
    thread = _thread_of(pa)

    code, body = _thread(thread, a["id"], b["id"])
    assert code == 200, body
    [card] = body["messages"]
    assert card["id"] == pa
    assert [t["id"] for t in card["twins"]] == [pb]
    assert card["twins"][0]["account_id"] == b["id"]
    assert [loc["mailbox_id"] for loc in card["twins"][0]["locations"]] == [_inbox(b)]
    assert body["account_ids"] == [a["id"], b["id"]]
    assert body["members"] == [{"account_id": a["id"], "id": pa},
                               {"account_id": b["id"], "id": pb}]
    assert card["seen"] is False

    # Read in one account only: still unread, so opening it marks the other.
    assert api("POST", f"/api/messages/{pa}/mark?seen=1")[0] == 200
    assert _thread(thread, a["id"], b["id"])[1]["messages"][0]["seen"] is False
    assert api("POST", f"/api/messages/{pb}/mark?seen=1")[0] == 200
    assert _thread(thread, a["id"], b["id"])[1]["messages"][0]["seen"] is True

    # The account named first decides which copy is the card.
    code, body = _thread(thread, b["id"], a["id"])
    assert code == 200
    assert body["messages"][0]["id"] == pb
    assert [t["id"] for t in body["messages"][0]["twins"]] == [pa]
    assert body["account_ids"] == [b["id"], a["id"]]


def test_the_reader_still_takes_one_account_and_refuses_odd_requests(pair):
    a, b = pair
    _, pa, _ = _deliver_both(a, b)
    thread = _thread_of(pa)

    code, body = _thread(thread, a["id"])
    assert code == 200
    assert [m["id"] for m in body["messages"]] == [pa]
    assert body["messages"][0]["twins"] == []
    assert body["account_ids"] == [a["id"]]

    assert _thread(thread, 987654321)[0] == 404
    assert _thread(thread, *range(1, 22))[0] == 422
    assert api("GET", f"/api/threads/{thread}")[0] == 422


# --- flags --------------------------------------------------------------------


def test_mark_with_also_marks_both_copies_and_skips_a_gone_one(pair):
    a, b = pair
    _, pa, pb = _deliver_both(a, b)

    code, body = api("POST", f"/api/messages/{pa}/mark?seen=1&also={pb}&also=987654321")
    assert code == 200, body
    assert all(p["seen"] for p in dbfixture.placements(a["email"], pa))
    assert all(p["seen"] for p in dbfixture.placements(b["email"], pb))

    code, _ = api("POST", f"/api/messages/{pa}/mark?seen=0&also={pb}")
    assert code == 200
    assert not any(p["seen"] for p in dbfixture.placements(b["email"], pb))

    too_many = "&".join(f"also={i}" for i in range(1000, 1051))
    assert api("POST", f"/api/messages/{pa}/mark?seen=1&{too_many}")[0] == 422


def test_flag_with_also_flags_both_and_flagged_lists_the_mail_once(pair):
    a, b = pair
    _, pa, pb = _deliver_both(a, b)
    thread = _thread_of(pa)

    code, body = api("POST", f"/api/messages/{pa}/flag?flagged=1&also={pb}")
    assert code == 200, body
    assert all(p["flagged"] for p in dbfixture.placements(a["email"], pa))
    assert all(p["flagged"] for p in dbfixture.placements(b["email"], pb))

    [row] = _rows_for("scope=flagged", thread)
    assert row["members"][0] == _own(row)
    assert sorted(m["id"] for m in row["members"]) == sorted([pa, pb])
    assert row["flagged"] is True


def test_flagged_keeps_two_copies_in_one_account_apart(pair):
    """Delivered twice to one account (two aliases), once to the other. The
    first copy in each account pairs up, the second stays a row of its own:
    in its account it is a second message, and hiding it would hide a mail."""
    a, b = pair
    mid = f"twice-{uuid.uuid4().hex}@sender.example"
    for acc, uid in ((a, 1), (a, 2), (b, 1)):
        dbfixture.ingest_raw_message(acc["email"], _delivery(mid, acc["email"], T0, "Report"),
                                     uid=uid)
    with dbfixture.session() as db:
        copies = db.query(Message).filter(Message.message_id == mid).order_by(Message.id).all()
        a1, a2 = [m.id for m in copies if m.account_id == a["id"]]
        [b1] = [m.id for m in copies if m.account_id == b["id"]]
    for pk in (a1, a2, b1):
        assert api("POST", f"/api/messages/{pk}/flag?flagged=1")[0] == 200

    rows = [r for r in _rows("scope=flagged") if r["id"] in (a1, a2, b1)
            or any(m["id"] in (a1, a2, b1) for m in r["members"])]
    assert sorted(sorted(m["id"] for m in r["members"]) for r in rows) == sorted(
        [sorted([a1, b1]), [a2]])
    for row in rows:
        assert row["members"][0] == _own(row)


# --- filing a conversation ----------------------------------------------------


def test_archiving_a_merged_conversation_moves_both_under_one_op_and_undo_puts_both_back(pair):
    a, b = pair
    for acc in pair:
        _folders(acc, "Archive")
    _, pa, pb = _deliver_both(a, b)
    thread = _thread_of(pa)

    code, body = api("POST", f"/api/messages/threads/{thread}/archive"
                             f"?account_id={a['id']}&account_id={b['id']}")
    assert code == 200, body
    op = body["op_id"]
    assert body["moved"] == 2
    assert _live_folders(a, pa) == ["Archive"]
    assert _live_folders(b, pb) == ["Archive"]
    for acc in pair:
        moves = dbfixture.pending_actions(acc["email"], "move")
        assert moves and all(m["payload"]["op_id"] == op for m in moves)
    assert _rows_for("scope=unified_inbox", thread) == []

    code, body = api("POST", f"/api/actions/{op}/undo")
    assert code == 200, body
    assert body["restored"] == 2
    assert _live_folders(a, pa) == ["INBOX"]
    assert _live_folders(b, pb) == ["INBOX"]
    [row] = _rows_for("scope=unified_inbox", thread)
    assert len(row["members"]) == 2


def test_archiving_refuses_before_anything_moves_when_one_account_has_no_archive(pair):
    a, b = pair
    _folders(a, "Archive")
    _, pa, pb = _deliver_both(a, b)
    thread = _thread_of(pa)

    code, body = api("POST", f"/api/messages/threads/{thread}/archive"
                             f"?account_id={a['id']}&account_id={b['id']}")
    assert code == 400, body
    assert "Archive" in body["detail"]
    assert _live_folders(a, pa) == ["INBOX"]
    assert _live_folders(b, pb) == ["INBOX"]
    assert dbfixture.pending_actions(a["email"]) == []


def test_a_thread_route_skips_accounts_without_the_thread_and_bounds_the_list(pair):
    a, b = pair
    _folders(a, "Trash")
    pa = _deliver(a, f"one-{uuid.uuid4().hex}@sender.example", uid=1)
    thread = _thread_of(pa)

    code, body = api("POST", f"/api/messages/threads/{thread}/trash"
                             f"?account_id={b['id']}&account_id={a['id']}")
    assert code == 200, body
    assert _live_folders(a, pa) == ["Trash"]
    assert api("POST", f"/api/messages/threads/{thread}/trash?account_id={b['id']}")[0] == 404
    many = "&".join(f"account_id={i}" for i in range(1, 22))
    assert api("POST", f"/api/messages/threads/{thread}/trash?{many}")[0] == 422


# --- filing one message -------------------------------------------------------


@pytest.mark.parametrize("verb,folder", [("archive", "Archive"), ("trash", "Trash")])
def test_a_message_verb_with_also_files_the_copy_out_of_every_folder(pair, verb, folder):
    a, b = pair
    for acc in pair:
        _folders(acc, folder)
    mid, pa, pb = _deliver_both(a, b)
    # The copy in the other account is also under a label of its own.
    dbfixture.record_placement(b["email"], mid, uid=7, folder="Work")
    assert _live_folders(b, pb) == ["INBOX", "Work"]

    code, body = api("POST", f"/api/messages/{pa}/{verb}"
                             f"?source_mailbox_id={_inbox(a)}&also={pb}&also=987654321")
    assert code == 200, body
    op = body["op_id"]
    assert _live_folders(a, pa) == [folder]
    assert _live_folders(b, pb) == [folder]
    actions = (dbfixture.pending_actions(a["email"], "move")
               + dbfixture.pending_actions(b["email"], "move"))
    assert len(actions) == 3
    assert {m["payload"]["op_id"] for m in actions} == {op}
    # Each copy went to its own account's folder, never across.
    assert {m["payload"]["to_folder"] for m in actions} == {folder}

    # Nothing left to move anywhere is the one refusal.
    code, _ = api("POST", f"/api/messages/{pa}/{verb}?also={pb}")
    assert code == 409


def test_message_archive_with_also_refuses_when_the_copy_has_no_archive(pair):
    a, b = pair
    _folders(a, "Archive")
    _, pa, pb = _deliver_both(a, b)
    code, body = api("POST", f"/api/messages/{pa}/archive?also={pb}")
    assert code == 400, body
    assert _live_folders(a, pa) == ["INBOX"]
    assert _live_folders(b, pb) == ["INBOX"]


# --- parking ------------------------------------------------------------------


def test_park_with_also_parks_both_conversations(pair):
    a, b = pair
    _, pa, pb = _deliver_both(a, b)
    thread = _thread_of(pa)

    code, body = api("POST", f"/api/messages/{pa}/park?also={pb}")
    assert code == 200, body
    assert body["moved"] == 2
    [row] = _rows_for("scope=parked", thread)
    assert sorted(m["account_id"] for m in row["members"]) == sorted([a["id"], b["id"]])
    assert _rows_for("scope=unified_inbox", thread) == []

    code, _ = api("POST", f"/api/messages/{pa}/park?also={pb}")
    assert code == 409

    code, body = api("POST", f"/api/actions/{body['op_id']}/undo")
    assert code == 200, body
    assert body["restored"] == 2
    [row] = _rows_for("scope=unified_inbox", thread)
    assert len(row["members"]) == 2


def test_parking_one_account_only_leaves_the_other_in_all_inboxes(pair):
    a, b = pair
    _, pa, pb = _deliver_both(a, b)
    thread = _thread_of(pa)

    assert api("POST", f"/api/messages/{pa}/park")[0] == 200
    [inbox_row] = _rows_for("scope=unified_inbox", thread)
    assert [m["id"] for m in inbox_row["members"]] == [pb]
    [parked_row] = _rows_for("scope=parked", thread)
    assert [m["id"] for m in parked_row["members"]] == [pa]


# --- reminders ----------------------------------------------------------------


def test_remind_with_also_sets_one_reminder_per_account_and_clears_both(pair):
    a, b = pair
    for acc in pair:
        _folders(acc, "Archive")
    _, pa, pb = _deliver_both(a, b)
    thread = _thread_of(pa)

    code, body = api("POST", f"/api/messages/{pa}/remind",
                     {"due_at": _soon(days=3), "also": [pb]})
    assert code == 200, body
    assert body["op_id"] and body["account_id"] == a["id"]
    [ra] = dbfixture.reminder_rows(a["email"])
    [rb] = dbfixture.reminder_rows(b["email"])
    assert ra["state"] == rb["state"] == "pending"
    assert ra["due_at"] == rb["due_at"]
    assert _live_folders(a, pa) == ["Archive"] and _live_folders(b, pb) == ["Archive"]
    moves = (dbfixture.pending_actions(a["email"], "move")
             + dbfixture.pending_actions(b["email"], "move"))
    assert {m["payload"]["op_id"] for m in moves} == {body["op_id"]}

    [row] = _rows_for("scope=reminders", thread)
    assert len(row["members"]) == 2
    assert row["remind_at"]

    code, thread_body = _thread(thread, a["id"], b["id"])
    assert thread_body["reminder"]["account_id"] == a["id"]

    code, body = api("DELETE", f"/api/messages/{pa}/remind?restore=0&also={pb}")
    assert code == 200, body
    assert [r["state"] for r in dbfixture.reminder_rows(a["email"])] == ["cancelled"]
    assert [r["state"] for r in dbfixture.reminder_rows(b["email"])] == ["cancelled"]
    assert _rows_for("scope=reminders", thread) == []

    code, _ = api("DELETE", f"/api/messages/{pa}/remind?also={pb}")
    assert code == 404


def test_undoing_a_shared_reminder_takes_back_every_promise(pair):
    a, b = pair
    for acc in pair:
        _folders(acc, "Archive")
    _, pa, pb = _deliver_both(a, b)

    code, body = api("POST", f"/api/messages/{pa}/remind",
                     {"due_at": _soon(days=3), "also": [pb]})
    assert code == 200, body

    code, undone = api("POST", f"/api/actions/{body['op_id']}/undo")
    assert code == 200, undone
    assert [r["state"] for r in dbfixture.reminder_rows(a["email"])] == ["cancelled"]
    assert [r["state"] for r in dbfixture.reminder_rows(b["email"])] == ["cancelled"]
    assert _live_folders(a, pa) == ["INBOX"] and _live_folders(b, pb) == ["INBOX"]


def test_a_reminder_on_one_account_lists_only_that_account(pair):
    """The other account's copy is in its inbox with nothing put off."""
    a, b = pair
    _folders(a, "Archive")
    _, pa, pb = _deliver_both(a, b)
    thread = _thread_of(pa)

    assert api("POST", f"/api/messages/{pa}/remind", {"due_at": _soon(days=1)})[0] == 200
    [row] = _rows_for("scope=reminders", thread)
    assert [m["id"] for m in row["members"]] == [pa]
    [inbox_row] = _rows_for("scope=unified_inbox", thread)
    assert [m["id"] for m in inbox_row["members"]] == [pb]
    assert inbox_row["remind_at"] is None


def test_remind_with_also_refuses_before_anything_moves_when_the_copy_has_no_archive(pair):
    a, b = pair
    _folders(a, "Archive")
    _, pa, pb = _deliver_both(a, b)

    code, body = api("POST", f"/api/messages/{pa}/remind",
                     {"due_at": _soon(days=1), "also": [pb]})
    assert code == 400, body
    assert "pytest two" in body["detail"]
    assert dbfixture.reminder_rows(a["email"]) == []
    assert _live_folders(a, pa) == ["INBOX"]

    code, _ = api("POST", f"/api/messages/{pa}/remind",
                  {"due_at": _soon(days=1), "also": list(range(1000, 1051))})
    assert code == 422


# --- the pure collapse --------------------------------------------------------


def _m(id_, account, mid="x@y", sender="A@ex.com", when=T0):
    return SimpleNamespace(id=id_, account_id=account, message_id=mid, from_addr=sender,
                           date_sent=when)


def test_collapse_pairs_copies_across_accounts_only():
    one, two, alone = _m(1, 10), _m(2, 20), _m(3, 10, mid="other@y")
    out = spanning.collapse_twins([one, two, alone], [20, 10])
    assert [(rep.id, [t.id for t in twins]) for rep, twins in out] == [(2, [1]), (3, [])]

    # Same Message-ID, same sender, a different Date: two mails.
    late = _m(4, 20, when=T0 + timedelta(seconds=1))
    assert [rep.id for rep, _ in spanning.collapse_twins([one, late], [10, 20])] == [1, 4]

    # The sender's case does not matter, the address does.
    upper = _m(5, 20, sender="a@EX.com")
    assert spanning.collapse_twins([one, upper], [10, 20]) == [(one, [upper])]

    # No Message-ID or no Date: nobody's twin.
    bare = [_m(6, 10, mid=None), _m(7, 20, mid=None)]
    assert [rep.id for rep, _ in spanning.collapse_twins(bare, [10, 20])] == [6, 7]


def test_collapse_keeps_two_copies_in_one_account_apart():
    first, second, there = _m(1, 10), _m(2, 10), _m(3, 20)
    out = spanning.collapse_twins([first, second, there], [10, 20])
    assert [(rep.id, [t.id for t in twins]) for rep, twins in out] == [(1, [3]), (2, [])]
    assert [rep.id for rep, _ in spanning.collapse_twins([first, second], [10])] == [1, 2]


def test_distinct_ids_keeps_order():
    assert spanning.distinct_ids([3, 1, 3, 2, 1]) == [3, 1, 2]
    assert spanning.distinct_ids([]) == []
