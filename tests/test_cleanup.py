"""Integration tests for the Cleanup panel: grouping, its protections, filing.

The grouping itself is the easy half. Most of what is checked here is the other
half — what the panel must *never* offer — because that is where a bug costs
somebody mail rather than a feature.
"""

import uuid
from datetime import timedelta

import dbfixture
import pytest
from conftest import T0, mailbox
from core import bodysig
from core.models import Message
from helpers import api, make_message
from sqlalchemy import select


def _seed_trash(email):
    """Give the account a \\Trash to file into, as every real server has."""
    dbfixture.ingest_raw_message(email, make_message(
        f"<trash-seed-{uuid.uuid4().hex}@t>", "Seed", "x@y.com", email, "seed", T0),
        uid=1, folder="Trash", role_hint="\\Trash")


# A body long enough for core.bodysig to fingerprint (MIN_WORDS), written the
# way generated mail is: a fixed template with a couple of numbers in it.
BODY = """Hello, this is an automated notification from the monitoring system.
The scheduled job finished with status {status} after {n} seconds of work and
wrote its output to the usual place. No action is required from you unless the
status above says otherwise. Please do not reply to this message."""


def _seed(email, subjects, frm="alerts@example.com", when=T0, body=None, uid0=100):
    """Ingest one message per subject, all from one sender."""
    for i, subject in enumerate(subjects):
        raw = make_message(
            f"<clean-{uuid.uuid4().hex}@t>", subject, frm, email,
            (body or BODY).format(status="ok", n=i + 1), when + timedelta(minutes=i))
        dbfixture.ingest_raw_message(email, raw, uid=uid0 + i)


def _in_folder(account_id, mailbox_id):
    _, r = api("GET", f"/api/messages?mailbox_id={mailbox_id}&limit=200")
    return [row["id"] for row in r["rows"]]


def _clusters(account_id, **kw):
    params = "&".join(f"{k}={v}" for k, v in {"account_id": account_id, **kw}.items())
    code, body = api("GET", f"/api/cleanup/clusters?{params}")
    assert code == 200, body
    return body


def _group(payload, needle):
    return next((g for g in payload["clusters"] if needle in g["label"]), None)


def test_subject_template_groups_mail_that_differs_only_in_numbers(account):
    email, aid = account["email"], account["id"]
    _seed(email, [f"Backup of /var/www on rebel: {n} files" for n in range(1, 7)])

    g = _group(_clusters(aid), "backup of")
    assert g is not None
    # The digits are what the template masks; everything else survives it.
    assert g["label"] == "backup of /var/www on rebel: # files"
    # The key is the pair the grouping is actually by, so that two senders
    # writing the same template are two rows the panel can tell apart.
    assert g["key"] == "alerts@example.com\tbackup of /var/www on rebel: # files"
    assert g["count"] == 6
    assert g["subjects"] == 6
    assert g["from_addr"] == "alerts@example.com"
    assert _clusters(aid)["totals"]["messages"] >= 6


def test_subject_group_identity_is_not_ambiguous_across_senders(account):
    """A UI-facing cleanup identifier must distinguish every displayed row.

    The key used to be the masked subject template alone, while the browser
    stores ``confirming``, ``working`` and ``filed`` by that value and finds
    the clicked group by it. Two senders with one template therefore shared a
    button state, and the second row's Delete resolved to the first sender's
    mail. Stated as an API/UI contract rather than as an implementation: what
    the key is made of is open, that no two displayed rows share one is not.
    """
    email, aid = account["email"], account["id"]
    subjects = [f"Weekly status report {n}" for n in range(1, 7)]
    _seed(email, subjects, frm="first-sender@example.com")
    _seed(email, subjects, frm="second-sender@example.com", uid0=300)

    groups = [g for g in _clusters(aid)["clusters"]
              if g["from_addr"] in {"first-sender@example.com", "second-sender@example.com"}]
    assert len(groups) == 2
    assert len({g["key"] for g in groups}) == 2


def test_a_group_can_be_trashed_whole_and_undone(account):
    email, aid = account["email"], account["id"]
    _seed_trash(email)
    _seed(email, [f"Package updates on rebel ({n})" for n in range(1, 7)])

    g = _group(_clusters(aid), "package updates")
    assert g["count"] == 6

    code, res = api("POST", "/api/cleanup/trash", {
        "mode": "subject", "from_addr": g["from_addr"], "key": g["key"], "account_id": aid,
    })
    assert code == 200, res
    assert res["moved"] == 6 and res["done"] is True
    # The op id is what the Recent actions panel hangs an Undo on; without it a
    # thousand-message delete would be the one action in meerail you cannot take
    # back.
    assert res["op_id"]

    # Gone from the panel, and gone from the folder it was in.
    assert _group(_clusters(aid), "package updates") is None
    assert len(_in_folder(aid, mailbox(email, "trash")["id"])) >= 6

    code, _ = api("POST", f"/api/actions/{res['op_id']}/undo")
    assert code == 200
    assert _group(_clusters(aid), "package updates")["count"] == 6


def test_a_conversation_is_never_offered(account):
    """Twenty replies under one subject are a thread, not a flood of notices."""
    email, aid = account["email"], account["id"]
    root = f"<thread-{uuid.uuid4().hex}@t>"
    dbfixture.ingest_raw_message(email, make_message(
        root, "The house on Gartenstraße", "solicitor@example.com", email,
        BODY.format(status="ok", n=0), T0), uid=200)
    for i in range(1, 6):
        dbfixture.ingest_raw_message(email, make_message(
            f"<reply-{uuid.uuid4().hex}@t>", "Re: The house on Gartenstraße",
            "solicitor@example.com", email, BODY.format(status="ok", n=i),
            T0 + timedelta(hours=i), in_reply_to=root, refs=[root]), uid=200 + i)

    for mode in ("subject", "body"):
        assert _group(_clusters(aid, mode=mode), "gartenstra") is None, mode


def test_flagged_and_answered_mail_is_left_out_of_the_group(account):
    email, aid = account["email"], account["id"]
    _seed_trash(email)
    _seed(email, [f"Login from a new device at {n}:00" for n in range(1, 8)])

    g = _group(_clusters(aid), "login from")
    assert g["count"] == 7

    # Flag one of them, the way the reader does.
    code, rows = api("GET", f"/api/search?q=Login%20from&account_id={aid}")
    assert code == 200
    flagged_id = rows["rows"][0]["id"]
    api("POST", f"/api/messages/{flagged_id}/flag?flagged=1")

    assert _group(_clusters(aid), "login from")["count"] == 6

    api("POST", "/api/cleanup/trash", {
        "mode": "subject", "from_addr": g["from_addr"], "key": g["key"], "account_id": aid,
    })
    # The one that was flagged is still where it was: the delete re-resolves the
    # group under the same filter that listed it, so protecting a message is
    # enough — the panel need not be redrawn first.
    code, detail = api("GET", f"/api/messages/{flagged_id}")
    assert code == 200
    assert detail["flagged"] is True


def test_mail_of_any_age_is_grouped(account):
    """There is no age floor: this week's flood is a flood too.

    This used to be the opposite assertion — nothing under a month old was
    offered. It was removed because it hid the groups people most want gone: the
    sender who has written four times this week is exactly the one being looked
    for, and a headline that quietly excluded them answered a narrower question
    than the one being asked.
    """
    from datetime import datetime, timezone

    email, aid = account["email"], account["id"]
    now = datetime.now(timezone.utc)
    _seed(email, [f"Fresh alert {n}" for n in range(1, 7)], when=now - timedelta(days=2))
    _seed(email, [f"Ancient alert {n}" for n in range(1, 7)],
          when=now - timedelta(days=3000), uid0=300)

    assert _group(_clusters(aid), "fresh alert")["count"] == 6
    assert _group(_clusters(aid), "ancient alert")["count"] == 6


def test_totals_cover_every_group_not_just_the_drawn_ones(account):
    """The headline is about the mailbox, not about the page.

    Body mode used to recompute its byte total from the groups it was about to
    draw, so the figure silently depended on ?limit — a mailbox carrying 466 MB
    of bulk mail reported 86 MB the moment the panel asked for one row.
    """
    email, aid = account["email"], account["id"]
    for i, sender in enumerate(("a@example.com", "b@example.com", "c@example.com")):
        _seed(email, [f"Report {sender} {n}" for n in range(1, 7)],
              frm=sender, uid0=800 + i * 20)

    wide = _clusters(aid, mode="body", limit=50)["totals"]
    narrow = _clusters(aid, mode="body", limit=1)["totals"]
    assert wide["groups"] >= 3
    assert narrow == wide, "totals must not move when fewer rows are drawn"

    wide_s = _clusters(aid, limit=50)["totals"]
    assert _clusters(aid, limit=1)["totals"] == wide_s


def test_sorting_by_count_and_by_size_disagree_on_purpose(account):
    """Two honest answers to "biggest", and the panel offers both.

    Six fat messages against twenty thin ones: by size the small group wins,
    by count the large one does. If these ever agree the fixture has stopped
    testing anything.
    """
    email, aid = account["email"], account["id"]
    _seed(email, [f"Weekly digest with pictures {n}" for n in range(1, 7)],
          frm="fat@example.com", body=BODY + "\n" + ("filler text about nothing " * 400))
    _seed(email, [f"Ping {n}" for n in range(1, 21)], frm="thin@example.com", uid0=400)

    by_size = _clusters(aid, sort="size")["clusters"]
    by_count = _clusters(aid, sort="count")["clusters"]
    assert [g["bytes"] for g in by_size] == sorted((g["bytes"] for g in by_size), reverse=True)
    assert [g["count"] for g in by_count] == sorted((g["count"] for g in by_count), reverse=True)

    first = lambda rows, addr: next(i for i, g in enumerate(rows) if g["from_addr"] == addr)
    assert first(by_size, "fat@example.com") < first(by_size, "thin@example.com")
    assert first(by_count, "thin@example.com") < first(by_count, "fat@example.com")

    # And the totals describe the mailbox, so they cannot depend on the order.
    assert _clusters(aid, sort="size")["totals"] == _clusters(aid, sort="count")["totals"]


def test_groups_come_back_biggest_first(account):
    """Ordered by the space they take, not by how many they are."""
    email, aid = account["email"], account["id"]
    # Six fat messages against twenty thin ones: the smaller group is the one
    # worth reclaiming, and count ordering would bury it.
    _seed(email, [f"Weekly digest with pictures {n}" for n in range(1, 7)],
          frm="fat@example.com", body=BODY + "\n" + ("filler text about nothing " * 400))
    _seed(email, [f"Ping {n}" for n in range(1, 21)], frm="thin@example.com", uid0=400)

    rows = _clusters(aid)["clusters"]
    sizes = [g["bytes"] for g in rows]
    assert sizes == sorted(sizes, reverse=True)

    fat = next(g for g in rows if g["from_addr"] == "fat@example.com")
    thin = next(g for g in rows if g["from_addr"] == "thin@example.com")
    assert fat["count"] < thin["count"]
    assert rows.index(fat) < rows.index(thin)


def test_body_mode_groups_mail_whose_subjects_all_differ(account):
    """The case the subject template cannot see at all."""
    email, aid = account["email"], account["id"]
    _seed(email, [f"{n} neue Angebote: {word}" for n, word in
                  enumerate(["Reihenhaus", "Doppelhaus", "Bungalow", "Villa",
                             "Loft", "Penthouse"], start=2)],
          frm="myscout@example.com")

    # Nothing in common but the numbers, so the subject grouping says nothing.
    assert _group(_clusters(aid, mode="subject"), "neue angebote") is None

    payload = _clusters(aid, mode="body")
    # Fingerprints are written at ingest, so nothing here is waiting on the
    # backfill loop — that is only for mail stored before the column existed.
    assert payload["pending"] == 0
    g = next((c for c in payload["clusters"] if c["from_addr"] == "myscout@example.com"), None)
    assert g is not None
    assert g["count"] == 6
    assert g["subjects"] == 6


def test_body_group_trashes_by_its_fingerprint(account):
    email, aid = account["email"], account["id"]
    _seed_trash(email)
    _seed(email, [f"Digest for {d}" for d in
                  ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]],
          frm="digest@example.com")

    payload = _clusters(aid, mode="body")
    g = next(c for c in payload["clusters"] if c["from_addr"] == "digest@example.com")

    code, res = api("POST", "/api/cleanup/trash", {
        "mode": "body", "from_addr": g["from_addr"], "key": g["key"], "account_id": aid,
    })
    assert code == 200, res
    assert res["moved"] == 6 and res["done"] is True

    payload = _clusters(aid, mode="body")
    assert not [c for c in payload["clusters"] if c["from_addr"] == "digest@example.com"]


def test_body_group_delete_never_moves_an_overlapping_neighbour(account):
    """A body group's displayed membership and its delete membership must agree.

    Message 2 shares one band with the high-reach first group and another with
    the later five-message group. Greedy star clustering assigns it to the
    first group, and the delete must not find it again through the later
    group's seed: a bare one-hop predicate did, and answered "six moved" to a
    row that displayed five.
    """
    email, aid = account["email"], account["id"]
    _seed_trash(email)
    _seed(email, [f"Overlap fixture {n}" for n in range(1, 18)],
          frm="overlap@example.com")

    # Synthetic, but valid, four-band fingerprints. The first seed claims the
    # bridge (id 2) through x and its two dense neighbourhoods through a and b.
    # The later seed's y-neighbourhood is displayed without that already-claimed
    # bridge, whereas the deletion predicate finds it again through y.
    sigs = ["x a b c", "x y p q"]
    sigs += [f"m a {i} t{i}" for i in range(3, 8)]
    sigs += [f"n {i} b t{i}" for i in range(8, 13)]
    sigs += [f"v y {i} t{i}" for i in range(13, 18)]
    with dbfixture.session() as db:
        rows = db.execute(
            select(Message).where(Message.account_id == aid,
                                  Message.from_addr == "overlap@example.com")
            .order_by(Message.id)
        ).scalars().all()
        assert len(rows) == len(sigs)
        for row, sig in zip(rows, sigs):
            row.body_sig = sig

    groups = [g for g in _clusters(aid, mode="body")["clusters"]
              if g["from_addr"] == "overlap@example.com"]
    group = next(g for g in groups if g["count"] == 5)
    code, result = api("POST", "/api/cleanup/trash", {
        "mode": "body", "from_addr": group["from_addr"],
        "key": group["key"], "account_id": aid,
    })
    assert code == 200, result
    assert result["moved"] == group["count"]


def test_cleanup_moved_count_is_distinct_messages_on_a_label_server(account):
    """Cleanup's count is mail, not the number of local label placements changed."""
    email, aid = account["email"], account["id"]
    _seed_trash(email)
    _seed(email, [f"Labelled cleanup digest {n}" for n in range(1, 7)],
          frm="labels@example.com")

    with dbfixture.session() as db:
        message_ids = list(db.execute(
            select(Message.message_id).where(
                Message.account_id == aid, Message.from_addr == "labels@example.com")
            .order_by(Message.id)
        ).scalars().all())
    for i, message_id in enumerate(message_ids, start=1):
        assert dbfixture.record_placement(email, message_id, uid=800 + i,
                                          folder="All Mail", role_hint="\\All")

    group = _group(_clusters(aid), "labelled cleanup digest")
    assert group is not None and group["count"] == 6
    code, result = api("POST", "/api/cleanup/trash", {
        "mode": "subject", "from_addr": group["from_addr"],
        "key": group["key"], "account_id": aid,
    })
    assert code == 200, result
    assert result["moved"] == group["count"]


def test_trashing_a_group_that_has_already_gone_is_not_an_error(account):
    email, aid = account["email"], account["id"]
    _seed_trash(email)
    body = {"mode": "subject", "from_addr": "nobody@example.com",
            "key": "nothing like this # exists", "account_id": aid}
    code, res = api("POST", "/api/cleanup/trash", body)
    assert code == 200
    assert res == {"ok": True, "moved": 0, "done": True, "op_id": None}


# --- :similar, the search the panel hands you --------------------------------


def _search(q, account_id):
    from urllib.parse import quote
    code, body = api("GET", f"/api/search?q={quote(q)}&account_id={account_id}")
    assert code == 200, body
    return body


def test_similar_finds_a_template_across_unrelated_subjects(account):
    """The search a body group opens into: one fingerprint, every copy."""
    email, aid = account["email"], account["id"]
    subjects = ["Reihenhaus in Kiel", "Loft in Bonn", "Villa am See",
                "Bungalow in Ulm", "Penthouse in Jena", "Altbau in Trier"]
    _seed(email, subjects, frm="scout@example.com")
    # A different sender writing something else entirely, to prove the filter
    # is about the body and not about "everything of a similar size".
    _seed(email, [f"Lunch on {d}?" for d in ("Monday", "Tuesday", "Wednesday")],
          frm="colleague@example.com", uid0=500,
          body="Hi, are you free for lunch this week? I am flexible on the day and "
               "happy to come to your side of town if that is easier for you. Let me "
               "know what suits and I will book somewhere for {n} o'clock.")

    g = next(c for c in _clusters(aid, mode="body")["clusters"]
             if c["from_addr"] == "scout@example.com")

    found = {r["subject"] for r in _search(f":similar={g['key']}", aid)["rows"]}
    assert found == set(subjects), found


# The template each copy fills in differently — a real one, not six copies of
# one string. Members of a group like this do *not* all share a fingerprint:
# they are connected through each other, which is the whole reason the group is
# a component rather than a bucket.
VARIED = """Guten Tag,

zu Ihrem Suchauftrag "Haus kaufen im Umkreis von {km} km" gibt es neue Angebote.
{kind} in {place}, {rooms} Zimmer, Kaufpreis auf Anfrage. Sehen Sie sich die
passenden Objekte jetzt an und kontaktieren Sie den Anbieter direkt.

Diese Nachricht wurde automatisch erstellt, bitte antworten Sie nicht darauf."""

PLACES = ["Gartenstrasse", "Rosenweg", "Hafenstrasse", "Bergblick", "Seestrasse",
          "Kirchplatz", "Ahornweg", "Feldstrasse", "Lindenallee", "Muehlbach",
          "Sonnenhang", "Talblick"]
KINDS = ["Reihenhaus", "Bungalow", "Stadtvilla", "Loft", "Penthouse", "Dachgeschoss"]


def test_similar_recovers_a_group_whose_bodies_all_differ(account):
    """The search a group opens must find the whole group, not a corner of it.

    This is the regression that cost the feature an afternoon. Groups were
    connected components — A resembles B, B resembles C, so all three are one
    group — and no single search can ask for that. A real 438-message group
    opened a view of 142. Groups are stars now: every member resembles one seed
    message, which is what `key` names and what `:similar` asks for, so the
    group, the view and the delete are the same set by construction.

    A group of identical bodies would pass either way, so this one varies every
    copy: the fingerprints differ, and only the one-hop guarantee holds it
    together.
    """
    email, aid = account["email"], account["id"]
    subjects = []
    for i in range(12):
        subject = f"{i + 2} neue Angebote: {KINDS[i % len(KINDS)]} in {PLACES[i]}"
        subjects.append(subject)
        dbfixture.ingest_raw_message(email, make_message(
            f"<varied-{uuid.uuid4().hex}@t>", subject, "scout@example.com", email,
            VARIED.format(km=(i % 4) * 10 + 10, kind=KINDS[i % len(KINDS)],
                          place=PLACES[i], rooms=i % 6 + 2),
            T0 + timedelta(days=i)), uid=600 + i)

    g = next(c for c in _clusters(aid, mode="body")["clusters"]
             if c["from_addr"] == "scout@example.com")
    assert g["count"] == 12
    assert g["subjects"] == 12, "the subjects must all differ or this proves nothing"

    found = {r["subject"] for r in _search(f":similar={g['key']}", aid)["rows"]}
    assert found == set(subjects), f"missing {set(subjects) - found}"


def test_similar_takes_a_message_id_as_well(account):
    """"More like this one" — the spelling that works from outside Cleanup."""
    email, aid = account["email"], account["id"]
    _seed(email, [f"Build {n} failed" for n in range(1, 7)], frm="ci@example.com")

    rows = _search("Build failed", aid)["rows"]
    mid = rows[0]["id"]

    by_id = _search(f":similar={mid}", aid)["rows"]
    assert len(by_id) == 6
    assert mid in {r["id"] for r in by_id}


def test_similar_on_a_fingerprint_nothing_carries_finds_nothing(account):
    """Not "no filter": an unknown fingerprint must not widen the search."""
    email, aid = account["email"], account["id"]
    _seed(email, [f"Build {n} failed" for n in range(1, 7)], frm="ci@example.com")

    assert _search(":similar=ffffffff", aid)["rows"] == []
    # A message whose body was too short to fingerprint is the same case.
    dbfixture.ingest_raw_message(email, make_message(
        f"<short-{uuid.uuid4().hex}@t>", "ok", "x@y.com", email, "thanks", T0), uid=700)
    short = _search("thanks", aid)["rows"][0]["id"]
    assert _search(f":similar={short}", aid)["rows"] == []


# --- Trash, and seeing what you just did -------------------------------------


def test_deleted_mail_stays_findable_but_says_it_is_in_the_trash(account):
    """A search reads every folder, so it has to say which one.

    Deleting from a search view and watching the rows sit there is
    indistinguishable from a delete that failed. The rows carry `in_trash` so
    the list can mark them, and `:no-trash` leaves them out altogether — which
    is what the Cleanup panel asks for, because that view is worked *while*
    deleting.
    """
    email, aid = account["email"], account["id"]
    _seed_trash(email)
    _seed(email, [f"Nightly backup report {n}" for n in range(1, 7)])

    before = _search(":from=alerts@example\\.com", aid)["rows"]
    assert len(before) == 6
    assert not any(r["in_trash"] for r in before)

    g = _group(_clusters(aid), "nightly backup")
    code, res = api("POST", "/api/cleanup/trash", {
        "mode": "subject", "from_addr": g["from_addr"], "key": g["key"], "account_id": aid,
    })
    assert code == 200 and res["moved"] == 6

    after = _search(":from=alerts@example\\.com", aid)["rows"]
    assert len(after) == 6, "trashed mail is still mail, and still findable"
    assert all(r["in_trash"] for r in after)

    gone = _search(":no-trash :from=alerts@example\\.com", aid)["rows"]
    assert gone == []


def test_no_trash_keeps_mail_that_is_only_partly_deleted(account):
    """A message filed in two places, one of them Trash, is not deleted."""
    email, aid = account["email"], account["id"]
    _seed_trash(email)
    _seed(email, ["Kept in two places"], uid0=900)

    rows = _search(":from=alerts@example\\.com", aid)["rows"]
    assert len(rows) == 1 and not rows[0]["in_trash"]

    # A second placement, in Trash, alongside the one in the inbox — which is
    # what a label server does when you delete on one device.
    dbfixture.ingest_raw_message(email, make_message(
        f"<two-places-{uuid.uuid4().hex}@t>", "Kept in two places", "alerts@example.com",
        email, BODY.format(status="ok", n=1), T0), uid=950, folder="Trash",
        role_hint="\\Trash")

    still = _search(":no-trash :from=alerts@example\\.com", aid)["rows"]
    assert len(still) == 1, "the copy outside Trash is what counts"


# --- The fingerprint itself ---------------------------------------------------
# Unit tests, because the stored value's stability is a promise to every row
# already in the table: see core/bodysig.py on why it may not be re-rolled.


def test_fingerprint_ignores_the_numbers_a_template_varies():
    a = bodysig.fingerprint(BODY.format(status="ok", n=12))
    b = bodysig.fingerprint(BODY.format(status="ok", n=8177))
    assert a == b != ""


def test_fingerprint_separates_unrelated_mail():
    a = bodysig.fingerprint(BODY.format(status="ok", n=1))
    b = bodysig.fingerprint(
        "Hi Didi, are you free on Thursday for the review meeting? I have booked "
        "the small room but we can move it if the whole team wants to come along, "
        "and Friday afternoon works just as well for me either way.")
    assert not set(a.split()) & set(b.split())


def test_fingerprint_declines_a_body_too_short_to_mean_anything():
    assert bodysig.fingerprint("thanks!") == ""
    assert bodysig.fingerprint("") == ""


@pytest.mark.parametrize("changed", ["a single word swapped out near the end", ""])
def test_fingerprint_is_stable_across_calls(changed):
    text = BODY.format(status="ok", n=3) + " " + changed
    assert bodysig.fingerprint(text) == bodysig.fingerprint(text)


def test_fingerprint_survives_a_body_that_is_mostly_one_long_blob():
    """A base64 run in a text part must cost milliseconds, not minutes.

    The regression this pins: `_ADDR_RE` used to be `\\S+@\\S+\\.\\w+`, which
    backtracks quadratically over a long run of non-space characters that turns
    out not to be an address. Two hundred kilobytes of base64 took 104 seconds
    inside one `re.sub`, and `re` holds the GIL throughout — so the server
    stopped answering HTTP, the batch never reached its commit, and every
    restart began the same batch again. The wall-clock assertion is the point of
    the test; the margin is five thousand times the fixed cost, so it says
    "quadratic again" rather than "the machine is busy".
    """
    import time as _time

    blob = "ABCDEFghijkl0123456789+/" * 9000  # ~216 KB, no whitespace, no '@'
    prose = BODY.format(status="ok", n=4)
    started = _time.monotonic()
    sig = bodysig.fingerprint(prose + "\n" + blob)
    assert _time.monotonic() - started < 2.0

    # And the blob is dropped rather than shingled: what is left is the prose,
    # so the mail still groups with the same template sent without one.
    assert sig == bodysig.fingerprint(prose) != ""


def test_fingerprint_reads_only_the_front_of_a_very_long_body():
    """MAX_CHARS bounds the work; MAX_WORDS already bounded the answer."""
    prose = BODY.format(status="ok", n=4)
    quoted = " ".join(f"line {i} of a very long quoted thread" for i in range(20_000))
    body = prose + " " + quoted
    assert len(body) > bodysig.MAX_CHARS
    assert bodysig.fingerprint(body) == bodysig.fingerprint(body[:bodysig.MAX_CHARS]) != ""
