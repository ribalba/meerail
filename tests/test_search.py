"""Integration tests for /api/search (regex + keyword, quoted phrases, scope).

Requires the running server; uses a throwaway account so results are isolated.
"""

import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode

import dbfixture
from helpers import api, make_message

T0 = datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)


def _ingest(email, messages):
    """messages: list of (uid, raw_bytes). Ingests them the way the agent does."""
    for uid, raw in messages:
        dbfixture.ingest_raw_message(email, raw, uid=uid)


def _search(account_id, q, **kw):
    params = {"q": q, "account_id": account_id, **kw}
    return api("GET", "/api/search?" + urlencode(params))


def test_regex_keyword_and_case(account):
    email, aid = account["email"], account["id"]
    z = f"z-{uuid.uuid4().hex}@t"
    g = f"g-{uuid.uuid4().hex}@t"
    _ingest(email, [
        (1, make_message(f"<{z}>", "Zoo update", "x@y.com", email, "the ZEBRAWORD escaped today", T0)),
        (2, make_message(f"<{g}>", "Safari log", "x@y.com", email, "a GIRAFFE appeared", T0)),
    ])

    # keyword substring
    code, r = _search(aid, "ZEBRAWORD")
    assert code == 200 and r["total"] == 1

    # regex alternation across both messages
    code, r = _search(aid, r"ZEBRA\w+|GIRAFFE", mode="regex")
    assert code == 200 and r["total"] == 2

    # both modes ignore case, in either direction
    _, r = _search(aid, "zebraword", mode="regex")
    assert r["total"] == 1
    _, r = _search(aid, "the ZEBRAWORD")
    assert r["total"] == 1

    # keyword AND semantics: both terms must appear (they don't in one message)
    _, r = _search(aid, "ZEBRAWORD GIRAFFE")
    assert r["total"] == 0

    # invalid regex -> 400 with a helpful message
    code, r = _search(aid, "(", mode="regex")
    assert code == 400
    assert "regex" in (r.get("detail", "") if isinstance(r, dict) else "").lower()


def test_quoted_phrase_is_exact(account):
    """A quoted run matches as a phrase; the same words unquoted are just ANDed."""
    email, aid = account["email"], account["id"]
    tag = uuid.uuid4().hex[:8]
    ordered = f"<ord-{tag}@t>"
    scattered = f"<sca-{tag}@t>"
    _ingest(email, [
        (1, make_message(ordered, f"doc {tag}", "x@y.com", email,
                         f"{tag} read how to build the thing", T0)),
        (2, make_message(scattered, f"notes {tag}", "x@y.com", email,
                         f"{tag} how we ship: what to do, then build", T0)),
    ])

    # Unquoted: three independent substrings, so both mails qualify.
    _, r = _search(aid, f"{tag} how to build")
    assert r["total"] == 2

    # Quoted: only the mail with the words adjacent and in order.
    _, r = _search(aid, f'{tag} "how to build"')
    assert r["total"] == 1
    assert r["rows"][0]["subject"] == f"doc {tag}"

    # Case still ignored inside a phrase.
    _, r = _search(aid, f'{tag} "HOW TO BUILD"')
    assert r["total"] == 1

    # Half-typed quote (search fires per keystroke) reads as an open phrase
    # rather than blowing up or matching the quote character itself.
    _, r = _search(aid, f'{tag} "how to bui')
    assert r["total"] == 1

    # % and _ are literals in a term, not LIKE wildcards.
    _, r = _search(aid, f'{tag} "how%build"')
    assert r["total"] == 0


def test_time_window_excludes_old(account):
    email, aid = account["email"], account["id"]
    old = datetime(2000, 1, 1, tzinfo=timezone.utc)
    mid = f"old-{uuid.uuid4().hex}@t"
    _ingest(email, [(1, make_message(f"<{mid}>", "Ancient", "x@y.com", email, "PALEOTOKEN here", old))])

    _, r = _search(aid, "PALEOTOKEN")
    assert r["total"] == 1                          # all-time finds it
    _, r = _search(aid, "PALEOTOKEN", years=2)
    assert r["total"] == 0                          # last 2 years excludes a 2000 message


def test_thread_reports_which_attachment_matched(account):
    """A term that lives only in extracted attachment text comes back with the
    thread, so the reader can show why the message matched at all."""
    email, aid = account["email"], account["id"]
    token = "ATTONLY" + uuid.uuid4().hex[:6]
    mid = f"attq-{uuid.uuid4().hex}@t"
    _ingest(email, [(1, make_message(
        f"<{mid}>", "Quarterly numbers", "x@y.com", email, "nothing to see in the body", T0,
        text_attachment=f"the {token} is buried in here somewhere".encode()))])
    assert dbfixture.extract_all() >= 1

    _, sr = _search(aid, token)
    assert sr["total"] == 1, sr
    row = sr["rows"][0]

    # Without a query the thread is unannotated — no cost for normal reading.
    _, plain = api("GET", f"/api/threads/{row['thread_id']}?account_id={aid}")
    assert all("match_contexts" not in a
               for m in plain["messages"] for a in m["attachments"])

    _, hit = api("GET", f"/api/threads/{row['thread_id']}?account_id={aid}&q={token}")
    atts = [a for m in hit["messages"] for a in m["attachments"] if a.get("match_contexts")]
    assert len(atts) == 1, hit
    ctx = atts[0]["match_contexts"][0]
    assert ctx["match"] == token                      # exact span, for the <mark>
    assert "buried" in ctx["after"]                   # window either side of it

    # The term is nowhere in the mail itself — the attachment is the only reason.
    assert token not in (hit["messages"][0]["body_text"] or "")


def test_thread_annotation_survives_a_bad_regex(account):
    """A pattern Postgres rejects costs the highlights, not the conversation."""
    email, aid = account["email"], account["id"]
    mid = f"badrx-{uuid.uuid4().hex}@t"
    _ingest(email, [(1, make_message(f"<{mid}>", "Plain", "x@y.com", email, "body", T0))])
    _, sr = _search(aid, "Plain")
    tid = sr["rows"][0]["thread_id"]

    code, r = api("GET", f"/api/threads/{tid}?account_id={aid}&q=%28&mode=regex")
    assert code == 200 and len(r["messages"]) == 1


def test_a_thread_is_one_result(account):
    """A term quoted down a reply chain matched every message in it, filling the
    list with the same conversation. One row per thread; the reader shows the rest."""
    email, aid = account["email"], account["id"]
    token = "THREADTOK" + uuid.uuid4().hex[:6]
    root = f"r-{uuid.uuid4().hex}@t"
    reply = f"p-{uuid.uuid4().hex}@t"
    tail = f"q-{uuid.uuid4().hex}@t"
    lone = f"l-{uuid.uuid4().hex}@t"
    _ingest(email, [
        (1, make_message(f"<{root}>", "Budget", "a@y.com", email, f"the {token} plan", T0)),
        (2, make_message(f"<{reply}>", "Re: Budget", "b@y.com", email, f"> the {token} plan\nagreed",
                         T0.replace(hour=10), in_reply_to=f"<{root}>", refs=[f"<{root}>"])),
        (3, make_message(f"<{tail}>", "Re: Budget", "c@y.com", email, f"> the {token} plan\nship it",
                         T0.replace(hour=11), in_reply_to=f"<{reply}>", refs=[f"<{root}>", f"<{reply}>"])),
        (4, make_message(f"<{lone}>", "Unrelated", "d@y.com", email, f"{token} on its own", T0)),
    ])

    _, r = _search(aid, token)
    assert r["total"] == 2, r                       # the thread + the standalone, not 4
    assert len(r["rows"]) == 2
    assert len({row["thread_id"] for row in r["rows"]}) == 2

    # The row stands for the newest *matching* message, so its snippet is a real hit.
    thread_row = next(row for row in r["rows"] if row["subject"].startswith("Re: Budget"))
    assert thread_row["from_addr"] == "c@y.com"
    assert thread_row["thread_count"] == 3          # the badge still says how big it is

    # ...and opening it hands back the whole conversation.
    _, thread = api("GET", f"/api/threads/{thread_row['thread_id']}?account_id={aid}&q={token}")
    assert len(thread["messages"]) == 3


def test_dedup_respects_the_time_window(account):
    """Collapsing to one row per thread must not resurrect a filtered-out message
    as the representative."""
    email, aid = account["email"], account["id"]
    token = "WINDOWTOK" + uuid.uuid4().hex[:6]
    old_id = f"o-{uuid.uuid4().hex}@t"
    new_id = f"n-{uuid.uuid4().hex}@t"
    _ingest(email, [
        (1, make_message(f"<{old_id}>", "Long runner", "a@y.com", email, f"{token} first",
                         datetime(2000, 1, 1, tzinfo=timezone.utc))),
        (2, make_message(f"<{new_id}>", "Re: Long runner", "b@y.com", email, f"{token} again", T0,
                         in_reply_to=f"<{old_id}>", refs=[f"<{old_id}>"])),
    ])

    _, r = _search(aid, token)
    assert r["total"] == 1                          # all-time: one conversation
    _, r = _search(aid, token, years=2)
    assert r["total"] == 1                          # windowed: still one...
    assert r["rows"][0]["from_addr"] == "b@y.com"   # ...and it is the in-window message


def test_read_state_filters(account):
    """`:unread` / `:read` split the corpus the same way the sidebar counts it."""
    email, aid = account["email"], account["id"]
    token = "READTOK" + uuid.uuid4().hex[:6]
    _ingest(email, [
        (1, make_message(f"<u-{uuid.uuid4().hex}@t>", "Still new", "a@y.com", email,
                         f"{token} unread body", T0)),
    ])
    dbfixture.ingest_raw_message(
        email, make_message(f"<s-{uuid.uuid4().hex}@t>", "Old news", "b@y.com", email,
                            f"{token} read body", T0),
        uid=2, flags={"seen": True})

    _, r = _search(aid, token)
    assert r["total"] == 2

    _, r = _search(aid, f"{token} :unread")
    assert r["total"] == 1 and r["rows"][0]["from_addr"] == "a@y.com"

    _, r = _search(aid, f"{token} :read")
    assert r["total"] == 1 and r["rows"][0]["from_addr"] == "b@y.com"

    # A filter is not a word: it must not also be looked for in the text.
    _, r = _search(aid, ":unread")
    assert r["total"] >= 1
    assert all(row["seen"] is False for row in r["rows"])


def test_attachment_filter(account):
    email, aid = account["email"], account["id"]
    token = "ATTACHTOK" + uuid.uuid4().hex[:6]
    _ingest(email, [
        (1, make_message(f"<p-{uuid.uuid4().hex}@t>", "Plain", "a@y.com", email,
                         f"{token} no files here", T0)),
        (2, make_message(f"<a-{uuid.uuid4().hex}@t>", "With file", "b@y.com", email,
                         f"{token} see attached", T0, text_attachment="notes")),
    ])

    _, r = _search(aid, token)
    assert r["total"] == 2
    _, r = _search(aid, f"{token} :has-attachment")
    assert r["total"] == 1 and r["rows"][0]["has_attachments"] is True
    # The plural spelling is the same filter.
    _, r = _search(aid, f"{token} :has-attachments")
    assert r["total"] == 1


def test_participant_filters(account):
    """`:from` matches the sender, `:to` any recipient — Cc and Bcc included."""
    email, aid = account["email"], account["id"]
    token = "WHOTOK" + uuid.uuid4().hex[:6]
    _ingest(email, [
        (1, make_message(f"<1-{uuid.uuid4().hex}@t>", "From Ada", "ada@acme.com", email,
                         f"{token} one", T0, cc="carol@zed.com")),
        (2, make_message(f"<2-{uuid.uuid4().hex}@t>", "From Bob", "bob@other.com", email,
                         f"{token} two", T0)),
    ])

    _, r = _search(aid, f"{token} :from ada@acme.com")
    assert r["total"] == 1 and r["rows"][0]["from_addr"] == "ada@acme.com"

    # The pattern is a regex, wherever it is anchored.
    _, r = _search(aid, f"{token} :from @acme\\.com$")
    assert r["total"] == 1
    _, r = _search(aid, f"{token} :from (ada|bob)@")
    assert r["total"] == 2

    # Cc counts as a recipient.
    _, r = _search(aid, f"{token} :to carol@zed.com")
    assert r["total"] == 1 and r["rows"][0]["from_addr"] == "ada@acme.com"
    _, r = _search(aid, f"{token} :to {email}")
    assert r["total"] == 2

    # Filters combine with each other and with the text search.
    _, r = _search(aid, f"{token} :from ada :to carol")
    assert r["total"] == 1
    _, r = _search(aid, f"NOSUCHWORD{token} :from ada")
    assert r["total"] == 0

    # A pattern the engine can't compile names the filter it came from.
    code, r = _search(aid, f"{token} :from (unclosed")
    assert code == 400 and ":from" in r["detail"]


def test_filters_apply_in_regex_mode_and_leave_the_pattern_alone(account):
    email, aid = account["email"], account["id"]
    token = "RXFILTER" + uuid.uuid4().hex[:6]
    _ingest(email, [
        (1, make_message(f"<r1-{uuid.uuid4().hex}@t>", "Unread one", "a@y.com", email,
                         f"{token} 42 apples", T0)),
    ])
    dbfixture.ingest_raw_message(
        email, make_message(f"<r2-{uuid.uuid4().hex}@t>", "Read one", "b@y.com", email,
                            f"{token} 42 apples", T0),
        uid=2, flags={"seen": True})

    _, r = _search(aid, f"{token} \\d+ apples", mode="regex")
    assert r["total"] == 2
    # Stripping the filter must not disturb the pattern around it.
    _, r = _search(aid, f":unread {token} \\d+ apples", mode="regex")
    assert r["total"] == 1
    _, r = _search(aid, f"{token} \\d+ :unread apples", mode="regex")
    assert r["total"] == 1
