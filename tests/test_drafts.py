"""Composer drafts: /api/compose/drafts, and /send consuming the draft it came from.

Integration coverage against the running server. A draft is an outbound row in
state "draft" holding exactly what the composer last saved, recipients that are
not addresses yet included, and what these pin down is the contract the browser
is written against: the round trip is exact, a save based on a revision someone
else has already replaced is refused rather than silently winning, discarding
and sending both take the draft away for good, and none of it ever shows up in
the Outbox or anywhere the agent looks.

The staged files themselves live on the server's disk, which the suite cannot
see, so whether one still exists is asked the way the composer would find out:
by trying to send with it.
"""

import json
import time
import uuid

import psycopg
import pytest
from sqlalchemy import text

import dbfixture
from app import staging
from core import events as core_events
from core.database import engine, init_db
from helpers import api, build_pdf, upload_attachment

NOT_A_ROW = 987_654_321


def chip(filename: str = "notes.txt", data: bytes = b"draft attachment",
         content_type: str = "text/plain") -> dict:
    """Stage a real file and return it as the composer holds it."""
    code, up = upload_attachment(data, filename, content_type)
    assert code == 200, up
    return {"id": up["id"], "filename": up["filename"], "size": up["size"],
            "content_type": up["content_type"]}


def draft_body(account, **fields) -> dict:
    body = {
        "account_id": account["id"],
        "from_address": account["email"],
        "to": ["alice@example.com"],
        "cc": [],
        "bcc": [],
        "subject": "Half written",
        "body_text": "Dear Alice,\n\nstill thinking about it",
        "in_reply_to": None,
        "references": [],
        "attachments": [],
        "state": {},
    }
    body.update(fields)
    return body


def create(account, **fields) -> dict:
    code, draft = api("POST", "/api/compose/drafts", draft_body(account, **fields))
    assert code == 200, draft
    return draft


def listed(draft_id: int) -> dict | None:
    code, body = api("GET", "/api/compose/drafts")
    assert code == 200
    return next((d for d in body["drafts"] if d["id"] == draft_id), None)


def in_outbox(outbound_id: int) -> bool:
    code, body = api("GET", "/api/outbox")
    assert code == 200
    return any(r["id"] == outbound_id for r in body["rows"])


def queue_one(account, **fields) -> int:
    body = {"account_id": account["id"], "to": ["dest@example.com"],
            "subject": "Queued", "body_text": "on its way"}
    body.update(fields)
    code, r = api("POST", "/api/compose/send", body)
    assert code == 200 and r["state"] == "queued", r
    return r["id"]


# --- Saving and reading back -------------------------------------------------


def test_a_new_draft_comes_back_exactly_as_it_was_saved(account):
    """Every field the composer saves is what it gets back, the opaque UI state
    included: the server has no business reshaping any of it."""
    attachment = chip("report.pdf", build_pdf("DRAFTROUNDTRIP"), "application/pdf")
    sent = draft_body(
        account,
        from_address=f"alias-{uuid.uuid4().hex[:8]}@example.com",
        to=["alice@example.com", "Bob Example <bob@example.com>"],
        cc=["carol@example.com"],
        bcc=["dave@example.com"],
        subject="Re: Quarterly numbers – überarbeitet",
        body_text="Line one\n\n> quoted\n\n☃ unicode survives",
        in_reply_to="orig-123@example.com",
        references=["root-1@example.com", "orig-123@example.com"],
        attachments=[attachment],
        state={"mode": "reply", "html": True, "caret": 17, "ratio": 0.25,
               "collapsed": None, "tags": ["a", "b"], "nested": {"deep": {"x": [1, 2]}}},
    )
    code, created = api("POST", "/api/compose/drafts", sent)
    assert code == 200, created
    assert isinstance(created["id"], int)
    assert created["revision"] == 1
    for key, value in sent.items():
        assert created[key] == value, key
    assert created["missing_attachments"] == []
    assert created["created_at"] and created["updated_at"]

    assert listed(created["id"]) == created


def test_drafts_are_listed_oldest_first(account):
    first = create(account, subject="first")
    second = create(account, subject="second")

    code, body = api("GET", "/api/compose/drafts")
    ids = [d["id"] for d in body["drafts"]]
    assert ids.index(first["id"]) < ids.index(second["id"])


def test_a_draft_holds_what_a_send_would_refuse(account):
    """Autosave runs mid-keystroke, so recipients are whatever has been typed
    so far, and the From is not checked against an account whose aliases can
    change while the draft sits. Both are /send's to check, when it matters."""
    draft = create(account, to=["bob@", "Name <a"], cc=["half"], bcc=["@"],
                   from_address="stranger@evil.example")
    assert draft["to"] == ["bob@", "Name <a"]
    assert draft["cc"] == ["half"] and draft["bcc"] == ["@"]
    assert draft["from_address"] == "stranger@evil.example"
    assert listed(draft["id"])["to"] == ["bob@", "Name <a"]


def test_a_draft_for_an_account_that_does_not_exist_is_a_404(account):
    code, _ = api("POST", "/api/compose/drafts", draft_body(account, account_id=NOT_A_ROW))
    assert code == 404

    draft = create(account)
    code, _ = api("PUT", f"/api/compose/drafts/{draft['id']}",
                  draft_body(account, account_id=NOT_A_ROW, base_revision=1))
    assert code == 404
    assert listed(draft["id"])["revision"] == 1


def test_an_attachment_id_outside_the_staging_area_is_refused(account):
    """A draft's ids are turned back into paths by the code that deletes its
    files, so one that could point anywhere else must never be stored."""
    for bad in ("../../etc/passwd", "sub/dir.txt", "..", ""):
        body = draft_body(account, attachments=[{"id": bad, "filename": "x"}])
        code, _ = api("POST", "/api/compose/drafts", body)
        assert code == 400, bad

    draft = create(account)
    code, _ = api("PUT", f"/api/compose/drafts/{draft['id']}",
                  draft_body(account, base_revision=1,
                             attachments=[{"id": "../outside", "filename": "x"}]))
    assert code == 400
    assert listed(draft["id"])["attachments"] == []


# --- Revisions ---------------------------------------------------------------


def test_saving_on_the_current_revision_replaces_the_draft(account):
    """A save is the whole composer, so whatever it leaves out is cleared."""
    attachment = chip()
    draft = create(account, cc=["carol@example.com"], attachments=[attachment],
                   state={"step": 1})

    changed = draft_body(account, to=["erin@example.com"], cc=[], subject="Rewritten",
                         body_text="new body", attachments=[], state={"step": 2},
                         from_address=None, base_revision=1)
    code, saved = api("PUT", f"/api/compose/drafts/{draft['id']}", changed)
    assert code == 200, saved
    assert saved["id"] == draft["id"]
    assert saved["revision"] == 2
    assert saved["to"] == ["erin@example.com"] and saved["cc"] == []
    assert saved["subject"] == "Rewritten" and saved["body_text"] == "new body"
    assert saved["attachments"] == [] and saved["state"] == {"step": 2}
    assert saved["from_address"] is None
    assert saved["created_at"] == draft["created_at"]

    assert listed(draft["id"]) == saved


def test_a_save_based_on_an_old_revision_is_refused(account):
    """Two tabs on one draft: the second to save finds out it would overwrite
    something it never saw, and is told which revision to reload."""
    draft = create(account, subject="original")
    url = f"/api/compose/drafts/{draft['id']}"

    code, first = api("PUT", url, draft_body(account, subject="tab one", base_revision=1))
    assert code == 200 and first["revision"] == 2

    code, body = api("PUT", url, draft_body(account, subject="tab two", base_revision=1))
    assert code == 409
    assert body["detail"]["revision"] == 2
    assert body["detail"]["message"]

    kept = listed(draft["id"])
    assert kept["subject"] == "tab one" and kept["revision"] == 2


def test_saving_without_a_base_revision_is_refused(account):
    draft = create(account, subject="original")
    code, _ = api("PUT", f"/api/compose/drafts/{draft['id']}",
                  draft_body(account, subject="blind overwrite"))
    assert code == 422
    kept = listed(draft["id"])
    assert kept["subject"] == "original" and kept["revision"] == 1


def test_saving_a_discarded_draft_is_a_404(account):
    """Gone means sent or discarded somewhere else, and a save must not bring
    it back."""
    draft = create(account)
    assert api("DELETE", f"/api/compose/drafts/{draft['id']}")[0] == 204

    code, _ = api("PUT", f"/api/compose/drafts/{draft['id']}",
                  draft_body(account, base_revision=1))
    assert code == 404
    assert listed(draft["id"]) is None


def test_a_queued_message_cannot_be_saved_over_as_a_draft(account):
    oid = queue_one(account, subject="Already queued")
    code, _ = api("PUT", f"/api/compose/drafts/{oid}",
                  draft_body(account, subject="hijacked", base_revision=0))
    assert code == 404

    code, row = api("GET", f"/api/outbox/{oid}")
    assert code == 200 and row["subject"] == "Already queued"


# --- Discarding --------------------------------------------------------------


def test_discarding_a_draft_removes_it_and_its_files(account):
    attachment = chip("discarded.txt")
    draft = create(account, attachments=[attachment])

    assert api("DELETE", f"/api/compose/drafts/{draft['id']}")[0] == 204
    assert listed(draft["id"]) is None
    # Idempotent: a second tab discarding the same draft is not an error.
    assert api("DELETE", f"/api/compose/drafts/{draft['id']}")[0] == 204
    assert api("DELETE", f"/api/compose/drafts/{NOT_A_ROW}")[0] == 204

    # The staged file went with it.
    code, _ = api("POST", "/api/compose/send", {
        "account_id": account["id"], "to": ["dest@example.com"],
        "subject": "After discard", "body_text": "body", "attachments": [attachment["id"]]})
    assert code == 400


def test_discarding_by_the_id_of_a_queued_message_leaves_it_alone(account):
    oid = queue_one(account)
    assert api("DELETE", f"/api/compose/drafts/{oid}")[0] == 204
    assert in_outbox(oid)
    assert api("GET", f"/api/outbox/{oid}")[0] == 200


def test_an_attachment_whose_file_is_gone_is_reported_missing(account):
    """The chip stays, so the composer can say which file was lost instead of
    quietly sending without it."""
    kept, lost = chip("kept.txt"), chip("lost.txt")
    draft = create(account, attachments=[kept, lost])
    assert draft["missing_attachments"] == []

    assert api("DELETE", f"/api/compose/attachments/{lost['id']}")[0] == 204

    again = listed(draft["id"])
    assert again["attachments"] == [kept, lost]
    assert again["missing_attachments"] == [lost["id"]]


# --- Sending -----------------------------------------------------------------


def test_sending_a_draft_queues_the_mail_and_removes_the_draft(account):
    attachment = chip("sent-from-draft.pdf", build_pdf("DRAFTSEND"), "application/pdf")
    draft = create(account, subject="Ready now", attachments=[attachment])

    code, r = api("POST", "/api/compose/send", {
        "account_id": account["id"], "to": ["alice@example.com"], "subject": "Ready now",
        "body_text": "done", "attachments": [attachment["id"]], "draft_id": draft["id"]})
    assert code == 200 and r["state"] == "queued", r
    assert set(r) == {"id", "state", "send_at"}

    assert listed(draft["id"]) is None
    assert in_outbox(r["id"])
    assert "sent-from-draft.pdf" in dbfixture.outbound_mime(r["id"])
    # And the draft cannot be saved back into existence by a tab that had it open.
    code, _ = api("PUT", f"/api/compose/drafts/{draft['id']}",
                  draft_body(account, base_revision=1))
    assert code == 404


def test_sending_with_a_draft_id_that_is_not_a_draft_deletes_nothing(account):
    earlier = queue_one(account, subject="Earlier mail")

    later = queue_one(account, subject="Later mail", draft_id=earlier)
    assert in_outbox(earlier) and in_outbox(later)
    assert dbfixture.outbound_mime(earlier)

    # And an id that names nothing at all does not stop the send either.
    assert in_outbox(queue_one(account, subject="No such draft", draft_id=NOT_A_ROW))


def test_a_send_that_fails_leaves_its_draft_in_place(account):
    """Nothing was queued, so there is nothing the draft turned into: it has to
    still be there for the user to fix and try again."""
    draft = create(account)
    code, _ = api("POST", "/api/compose/send", {
        "account_id": account["id"], "to": ["dest@example.com"], "subject": "x",
        "body_text": "y", "attachments": ["0" * 32 + "__never-staged.txt"],
        "draft_id": draft["id"]})
    assert code == 400
    assert listed(draft["id"]) is not None


# --- Staying out of everything else ------------------------------------------


def test_drafts_stay_out_of_the_outbox_and_its_counts(account):
    _, outbox_before = api("GET", "/api/outbox")
    _, side_before = api("GET", "/api/mailboxes")
    _, status_before = api("GET", "/api/sync/status")

    draft = create(account, attachments=[chip()])

    _, outbox_after = api("GET", "/api/outbox")
    assert outbox_after["total"] == outbox_before["total"]
    assert all(r["id"] != draft["id"] for r in outbox_after["rows"])
    assert api("GET", f"/api/outbox/{draft['id']}")[0] == 404
    _, side_after = api("GET", "/api/mailboxes")
    assert side_after["smart"]["outbox_unsent"] == side_before["smart"]["outbox_unsent"]
    _, status_after = api("GET", "/api/sync/status")
    assert status_after["outbox"]["queued"] == status_before["outbox"]["queued"]
    # And nothing was queued for the agent.
    assert dbfixture.pending_actions(account["email"], "send") == []


def test_the_sweep_spares_exactly_the_files_drafts_reference(account):
    """The keep set the startup sweep is given, read from the real table: every
    chip of every draft, and not the ids a queued message already baked in."""
    first, second = chip("one.txt"), chip("two.txt")
    create(account, attachments=[first])
    create(account, attachments=[second])
    baked = chip("baked.txt")
    queue_one(account, attachments=[baked["id"]])

    with dbfixture.session() as db:
        keep = staging.draft_staging_ids(db)
    assert {first["id"], second["id"]} <= keep
    assert baked["id"] not in keep


# --- Live updates ------------------------------------------------------------


class Listener:
    """The server's event channel, read the way app/events.py reads it."""

    def __init__(self):
        self.conn = psycopg.connect(core_events.dsn(), autocommit=True)
        self.conn.execute(f"LISTEN {core_events.CHANNEL}")

    def drafts_until(self, done, timeout: float = 10.0) -> list[dict]:
        """Every drafts event, up to and including the first one `done` accepts."""
        seen: list[dict] = []
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            for note in self.conn.notifies(timeout=left, stop_after=1):
                event = json.loads(note.payload)
                if event.get("type") != "drafts":
                    continue
                seen.append(event)
                if done(event):
                    return seen
        raise AssertionError(f"timed out; drafts events seen: {seen}")

    def close(self):
        self.conn.close()


@pytest.fixture
def listener(require_server):
    lst = Listener()
    yield lst
    lst.close()


def test_every_change_to_a_draft_is_announced(account, listener):
    draft = create(account)
    did = draft["id"]
    api("PUT", f"/api/compose/drafts/{did}", draft_body(account, base_revision=1))
    api("DELETE", f"/api/compose/drafts/{did}")
    # A second discard deletes nothing, so it must say nothing. The draft
    # created after it is the marker that proves the silence.
    api("DELETE", f"/api/compose/drafts/{did}")
    marker = create(account)

    events = listener.drafts_until(lambda e: e["id"] == marker["id"])
    assert events == [
        {"type": "drafts", "id": did, "revision": 1, "change": "saved"},
        {"type": "drafts", "id": did, "revision": 2, "change": "saved"},
        {"type": "drafts", "id": did, "revision": None, "change": "deleted"},
        {"type": "drafts", "id": marker["id"], "revision": 1, "change": "saved"},
    ]


def test_sending_a_draft_announces_that_it_is_gone(account, listener):
    draft = create(account)
    queue_one(account, draft_id=draft["id"])

    events = listener.drafts_until(lambda e: e["change"] == "deleted")
    assert events[-1] == {"type": "drafts", "id": draft["id"], "revision": None,
                          "change": "deleted"}


# --- Upgrading ---------------------------------------------------------------


def test_an_existing_outbound_table_gains_the_draft_columns(account):
    """The columns are added in place on a volume that predates them, and every
    message already there reads as what it is: not a draft, never revised."""
    oid = queue_one(account, subject="From before drafts")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE outbound DROP COLUMN draft_state"))
        conn.execute(text("ALTER TABLE outbound DROP COLUMN revision"))

    init_db()

    with engine.connect() as conn:
        columns = {row.column_name: row for row in conn.execute(text(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'outbound' AND column_name IN ('draft_state', 'revision')"
        ))}
        existing = conn.execute(text(
            "SELECT revision, draft_state FROM outbound WHERE id = :id"), {"id": oid}).one()
    assert columns["draft_state"].data_type == "jsonb"
    assert (columns["revision"].data_type, columns["revision"].is_nullable) == ("integer", "NO")
    assert (existing.revision, existing.draft_state) == (0, None)

    assert in_outbox(oid)
    assert create(account)["revision"] == 1
