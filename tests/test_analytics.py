"""Analytics over the mail store — /api/analytics/overview.

Every figure this endpoint returns is derived rather than stored, and three of
the derivations are easy to get wrong in ways that look plausible on screen:

  * **Direction.** There is no "sent" column. It is inferred from the account's
    own addresses *or* the Sent folder's role, and each signal alone misses a
    real case — an alias filed outside Sent, or a Sent folder whose sender is an
    address the agent has not reported yet. Both are seeded here.
  * **Folder fan-out.** One message can hold several `message_locations` rows
    (Proton exposes labels as folders). A join instead of an EXISTS counts such
    a message once per label, which silently inflates every total.
  * **Reply latency.** A reply is the next message from the other side in the
    same thread. The window-function implementation must agree with the obvious
    correlated-subquery reading, so the fixture pins exact gaps (2h and 1h).

The response rate additionally excludes mail too recent to have been answered,
so a message you simply have not got to yet is not scored as one you ignored;
that is asserted separately because it is invisible in the totals.

Ingest goes through `core.ingest` (the agent's path) rather than direct model
inserts, so the test exercises the same rows a real sync would produce.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

import dbfixture
from helpers import api, make_message

NOW = datetime.now(timezone.utc)


def at(**kw) -> datetime:
    """A moment in the past, e.g. at(days=20)."""
    return NOW - timedelta(**kw)


@pytest.fixture(scope="module")
def mailbox(require_server):
    """A small mailbox with a hand-checkable answer for every panel.

    Module-scoped: the endpoint is read-only, so one seeding serves every
    assertion below and each ingest is a real parse + store.
    """
    email = f"analytics-{uuid.uuid4().hex[:8]}@example.com"
    alias = f"alias-{uuid.uuid4().hex[:8]}@example.com"
    acc = dbfixture.create_account(email, label="pytest analytics")
    # The alias only counts as "ours" once a sync has reported it.
    dbfixture.report_sync(email, addresses=[alias])

    uid = iter(range(1, 500))

    def send(mid, subject, frm, to, when, folder, in_reply_to=None, pdf=None, cc=None):
        raw = make_message(f"<{mid}>", subject, frm, to, "body", when,
                           in_reply_to=in_reply_to, pdf_text=pdf, cc=cc)
        dbfixture.ingest_raw_message(email, raw, uid=next(uid), folder=folder)
        return mid

    # A conversation they start and we answer two hours later.
    m1 = send("a1@t", "Contract draft", "alice@acme.com", email, at(days=20), "INBOX")
    send("a2@t", "Re: Contract draft", email, "alice@acme.com",
         at(days=20) + timedelta(hours=2), "Sent", in_reply_to=f"<{m1}>")

    # They wrote, we never answered.
    send("b1@t", "Invoice 0418", "bob@corp.com", email, at(days=19), "INBOX")

    # We write from an ALIAS into a folder that is not Sent, and they answer an
    # hour later. Direction here rests on the address list alone.
    m4 = send("c1@t", "Renewal terms", alias, "carol@corp.com", at(days=18), "Archive")
    send("c2@t", "Re: Renewal terms", "carol@corp.com", alias,
         at(days=18) + timedelta(hours=1), "INBOX", in_reply_to=f"<{m4}>")

    # Alice again, carrying a real attachment. Filed under two labels below.
    m6 = send("a3@t", "Board pack", "alice@acme.com", email, at(days=15), "INBOX",
              pdf="quarterly numbers")
    dbfixture.record_placement(email, f"<{m6}>", uid=next(uid), folder="Archive")

    # Sent from an address the account does not claim, but sitting in Sent.
    # Direction here rests on the folder role alone.
    send("d1@t", "Travel", "former-self@example.com", "dave@corp.com",
         at(days=16), "Sent")

    # Too recent to have been answered yet: received, but outside the response
    # rate's denominator.
    send("e1@t", "Quick question", "frank@acme.com", email, at(days=2), "INBOX")

    # Neither of these is correspondence and neither may appear anywhere.
    send("f1@t", "Unsent thought", email, "nobody@example.com", at(days=10), "Drafts")
    send("g1@t", "You have won", "spam@bad.example", email, at(days=10), "Junk")

    # Outside the 30-day window, inside a year.
    send("h1@t", "Last summer", "erin@acme.com", email, at(days=200), "INBOX")

    return {"email": email, "alias": alias, "id": acc["id"]}


def overview(mailbox, rng="30d", **extra):
    params = {"account_id": mailbox["id"], "range": rng, "tz_offset": 0}
    params.update(extra)
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    code, body = api("GET", f"/api/analytics/overview?{qs}")
    assert code == 200, body
    return body


# --- Totals -----------------------------------------------------------------


def test_totals_count_each_message_once(mailbox):
    """The two-label message must not count twice, and drafts/junk not at all."""
    t = overview(mailbox)["totals"]
    assert t["messages"] == 8
    assert t["received"] == 5
    assert t["sent"] == 3


def test_direction_uses_both_signals(mailbox):
    """Three sent messages, found three different ways.

    The account address (Sent folder), an alias in a non-Sent folder, and an
    unclaimed address in the Sent folder. Dropping either half of `_sent_pred`
    turns this into 2.
    """
    assert overview(mailbox)["totals"]["sent"] == 3


def test_per_day_rates_use_the_data_span(mailbox):
    t = overview(mailbox)["totals"]
    # Rates are counts over the span actually covered, so they stay consistent
    # with the counts beside them. Absolute tolerance, not relative: the server
    # rounds to two decimals, which on a small rate is a larger error than any
    # sane relative bound allows (1/6 is served as 0.17).
    assert t["received_per_day"] == pytest.approx(t["received"] / t["span_days"], abs=0.005)
    assert t["sent_per_day"] == pytest.approx(t["sent"] / t["span_days"], abs=0.005)


# --- Correspondents ---------------------------------------------------------


def test_correspondents_merge_both_directions(mailbox):
    rows = {c["address"]: c for c in overview(mailbox)["correspondents"]}
    # Alice wrote twice and was written to once; Carol one each way.
    assert (rows["alice@acme.com"]["received"], rows["alice@acme.com"]["sent"]) == (2, 1)
    assert (rows["carol@corp.com"]["received"], rows["carol@corp.com"]["sent"]) == (1, 1)
    # Dave only ever received from us, and must still appear.
    assert (rows["dave@corp.com"]["received"], rows["dave@corp.com"]["sent"]) == (0, 1)


def test_own_addresses_are_not_correspondents(mailbox):
    """Mail we sent must not make us our own top contact."""
    rows = {c["address"] for c in overview(mailbox)["correspondents"]}
    assert mailbox["email"] not in rows
    assert mailbox["alias"] not in rows


def test_junk_and_drafts_stay_out_of_every_panel(mailbox):
    d = overview(mailbox)
    assert "spam@bad.example" not in {c["address"] for c in d["correspondents"]}
    assert "bad.example" not in {x["domain"] for x in d["domains"]}
    assert "nobody@example.com" not in {c["address"] for c in d["correspondents"]}


def test_domains_group_inbound_senders(mailbox):
    dom = {x["domain"]: x["count"] for x in overview(mailbox)["domains"]}
    assert dom["acme.com"] == 3      # alice x2 + frank
    assert dom["corp.com"] == 2      # bob + carol


# --- Reply latency ----------------------------------------------------------


def test_reply_latency_both_directions(mailbox):
    """The seeded gaps are exactly 2h our way and 1h theirs."""
    lat = overview(mailbox)["latency"]
    assert lat["mine"] == 7200.0
    assert lat["theirs"] == 3600.0


def test_response_rate_excludes_mail_too_recent_to_answer(mailbox):
    """Five inbound messages, but the two-day-old one cannot count against us.

    Without the maturity cut the rate would be 1/5 rather than 1/4, and would
    drift with how recently the fixture ran.
    """
    lat = overview(mailbox)["latency"]
    assert lat["inbound"] == 5
    assert lat["rate_basis"] == 4
    assert lat["answered"] == 1
    assert lat["response_rate"] == 0.25


def test_latency_histogram_agrees_with_the_median(mailbox):
    """Bars and headline are the same population, so they cannot disagree."""
    lat = overview(mailbox)["latency"]
    buckets = {b["label"]: b["count"] for b in lat["buckets"]}
    assert sum(buckets.values()) == lat["answered"]
    assert buckets["1–4 h"] == 1     # the single 2h reply


# --- Conversations and attachments ------------------------------------------


def test_threads_and_attachments(mailbox):
    d = overview(mailbox)
    # Two conversations have a reply; the rest are single messages.
    assert d["threads"]["max"] == 2
    assert d["threads"]["multi"] == 2
    # One real attachment. Inline parts (signature images) must not be counted.
    assert d["attachments"]["count"] == 1


# --- Largest emails ---------------------------------------------------------


def test_largest_ranks_by_whole_message_size(mailbox):
    """The one message carrying a PDF is the heaviest thing in the window.

    And the figure is the RFC822 size, not the attachment's: a base64'd PDF plus
    its headers and body is necessarily larger than the payload `_attachments`
    counts, so the two panels are answering different questions on purpose.
    """
    d = overview(mailbox)
    rows = d["largest"]
    assert rows, "every message in the fixture has a size, so none may be dropped"
    assert rows[0]["subject"] == "Board pack"
    assert rows[0]["attachments"] is True
    assert [r["bytes"] for r in rows] == sorted((r["bytes"] for r in rows), reverse=True)
    assert rows[0]["bytes"] > d["attachments"]["bytes"]


def test_largest_carries_identity_for_each_row(mailbox):
    """The panel exists to be clicked, and a link needs all three ids."""
    for r in overview(mailbox)["largest"]:
        assert r["id"]
        assert r["account_id"] == mailbox["id"]
        assert r["thread_id"]


def test_largest_labels_direction_and_drops_the_excluded_folders(mailbox):
    rows = {r["subject"]: r for r in overview(mailbox)["largest"]}
    # Small mailbox, short list: everything countable is here, and nothing else.
    assert "You have won" not in rows
    assert "Unsent thought" not in rows
    assert rows["Re: Contract draft"]["sent"] is True
    assert rows["Invoice 0418"]["sent"] is False


def test_totals_carry_the_stored_size(mailbox):
    """The denominator the panel quotes, over the same base filter."""
    d = overview(mailbox)
    assert d["totals"]["bytes"] >= sum(r["bytes"] for r in d["largest"]) > 0


def test_volume_and_heatmap_sum_to_the_totals(mailbox):
    """Every message lands in exactly one bucket of each breakdown."""
    d = overview(mailbox)
    t = d["totals"]
    assert sum(v["received"] for v in d["volume"]) == t["received"]
    assert sum(v["sent"] for v in d["volume"]) == t["sent"]
    assert sum(h["received"] for h in d["heatmap"]) == t["received"]
    assert sum(h["sent"] for h in d["heatmap"]) == t["sent"]


# --- Read times -------------------------------------------------------------
#
# The read grid is the one panel not derived from mail itself: IMAP records
# *whether* a message is read and never *when*, so `messages.read_at` is stamped
# on an observed transition and is NULL for everything else. Everything that can
# go wrong here is a false stamp — mail that arrived already read, mail we sent
# that the server flagged \Seen on the way out, a second read of the same
# message — each of which would show up as a plausible-looking cell nobody can
# tell is wrong. So each is pinned below.


def _read_account(account, folder="INBOX", flags=None) -> tuple[int, str]:
    """One recent message in `folder`. Returns (message id, search token)."""
    email, aid = account["email"], account["id"]
    token = "READTOK" + uuid.uuid4().hex[:8]
    raw = make_message(f"<{uuid.uuid4().hex}@t>", f"Read test {token}",
                       "reader@acme.com", email, f"{token} body", at(hours=2))
    dbfixture.ingest_raw_message(email, raw, uid=1, folder=folder, flags=flags)
    _, r = api("GET", f"/api/search?q={token}&account_id={aid}")
    return r["rows"][0]["id"], token


def _reads(account, rng="30d") -> dict:
    params = f"account_id={account['id']}&range={rng}&tz_offset=0"
    code, body = api("GET", f"/api/analytics/overview?{params}")
    assert code == 200, body
    return {"reads": body["reads"], "total": sum(h["read"] for h in body["heatmap"]),
            "cells": [h for h in body["heatmap"] if h["read"]]}


def _slot(dt: datetime) -> tuple[int, int]:
    """(dow, hour) as Postgres extract() gives them: Sunday is 0."""
    return ((dt.weekday() + 1) % 7, dt.hour)


def test_nothing_read_yet_reads_as_nothing_recorded(account):
    """A fresh mailbox must say "no reads recorded", not "you read nothing".

    `first_at` is what tells those apart on screen, so it stays None until a
    read is actually seen.
    """
    _read_account(account)
    d = _reads(account)
    assert d["reads"] == {"count": 0, "first_at": None}
    assert d["total"] == 0


def test_marking_read_lands_in_the_local_hour(account):
    mid, _ = _read_account(account)
    before = datetime.now(timezone.utc)
    assert api("POST", f"/api/messages/{mid}/mark?seen=1")[0] == 200
    after = datetime.now(timezone.utc)

    d = _reads(account)
    assert d["reads"]["count"] == 1
    assert d["reads"]["first_at"] is not None
    assert d["total"] == 1
    # tz_offset=0, so the cell is the UTC hour the mark happened in. Both ends
    # of the call are allowed rather than one, so a mark landing either side of
    # an hour boundary is not a flake.
    (cell,) = d["cells"]
    assert (cell["dow"], cell["hour"]) in {_slot(before), _slot(after)}


def test_a_second_read_does_not_move_the_first(account):
    """Read is a moment, not a counter. Re-opening mail must not restamp it."""
    mid, _ = _read_account(account)
    api("POST", f"/api/messages/{mid}/mark?seen=1")
    first = _reads(account)["reads"]["first_at"]
    api("POST", f"/api/messages/{mid}/mark?seen=0")
    api("POST", f"/api/messages/{mid}/mark?seen=1")
    d = _reads(account)
    assert d["reads"]["first_at"] == first
    assert d["total"] == 1


def test_mail_that_arrived_already_read_is_never_stamped(account):
    """The backfill case, and the one that would ruin the panel.

    Marking read a message that is already \\Seen everywhere changes nothing, so
    it is not a read that happened now — it is a read that happened at a time
    nothing records. Stamping it would fill the grid with the hours the mailbox
    was first synced.
    """
    mid, _ = _read_account(account, flags={"seen": True})
    api("POST", f"/api/messages/{mid}/mark?seen=1")
    d = _reads(account)
    assert d["reads"] == {"count": 0, "first_at": None}
    assert d["total"] == 0


def test_a_read_in_another_app_is_recorded_at_the_next_sync(account):
    """Read on the phone: the flag sweep sees unread -> read and stamps it."""
    _read_account(account)
    assert dbfixture.set_flags(account["email"], "INBOX", [{"uid": 1, "flags": {"seen": True}}]) == 1
    d = _reads(account)
    assert d["reads"]["count"] == 1
    assert d["total"] == 1


def test_sent_mail_going_seen_is_not_a_read(account):
    """Servers mark outgoing mail \\Seen as it lands in Sent. Nobody read it."""
    _read_account(account, folder="Sent")
    dbfixture.set_flags(account["email"], "Sent", [{"uid": 1, "flags": {"seen": True}}])
    assert _reads(account)["total"] == 0


def test_read_grid_windows_on_when_reading_happened(account):
    """Old mail read today belongs in today's grid.

    The rest of the page windows on `date_sent`; this panel cannot, or a message
    from last year that you got to this morning would be missing from every
    range short enough to be worth looking at.
    """
    email, aid = account["email"], account["id"]
    # Recent mail as well, left unread: with nothing at all in the window the
    # whole overview short-circuits to "no mail in this window", which is a
    # different (and correct) answer than the one under test here.
    _read_account(account)
    token = "OLDREAD" + uuid.uuid4().hex[:8]
    raw = make_message(f"<{uuid.uuid4().hex}@t>", f"Old mail {token}",
                       "reader@acme.com", email, f"{token} body", at(days=200))
    dbfixture.ingest_raw_message(email, raw, uid=7, folder="INBOX")
    _, r = api("GET", f"/api/search?q={token}&account_id={aid}")
    api("POST", f"/api/messages/{r['rows'][0]['id']}/mark?seen=1")

    # The message itself is outside a 30-day window; the read is inside it.
    assert _reads(account, "30d")["total"] == 1
    assert _reads(account, "1y")["total"] == 1


# --- Windowing --------------------------------------------------------------


def test_range_filters_the_window(mailbox):
    """The 200-day-old message is outside a month and inside a year."""
    assert overview(mailbox, "30d")["totals"]["received"] == 5
    assert overview(mailbox, "1y")["totals"]["received"] == 6
    # Only the two-day-old message falls inside a week.
    assert overview(mailbox, "7d")["totals"]["received"] == 1


def test_unknown_account_is_404(mailbox):
    code, _ = api("GET", "/api/analytics/overview?account_id=999999999&range=30d")
    assert code == 404


def test_bad_range_is_rejected(mailbox):
    code, _ = api("GET", f"/api/analytics/overview?account_id={mailbox['id']}&range=forever")
    assert code == 422
