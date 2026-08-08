"""The journal server: ordering, isolation, and who is allowed to read what.

This is the piece that runs on somebody else's machine, so the properties worth
pinning are the ones that make that safe to do: a token names exactly one space
and cannot see another's records, the sequence numbers are handed out in commit
order (which is the whole basis of the claim rule in app/journal.py), and nothing
is deleted that a snapshot has not superseded.

Driven in-process through Starlette's TestClient against a temp SQLite file —
no container, no port, no network.
"""

from __future__ import annotations

import hashlib
import importlib
import sys

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

TOKEN_A = "token-for-space-a"
TOKEN_B = "token-for-space-b"
SPACE_A = hashlib.sha256(TOKEN_A.encode()).hexdigest()
SPACE_B = hashlib.sha256(TOKEN_B.encode()).hexdigest()


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A fresh journal server on its own database.

    Reimported per test because the module reads its configuration at import
    time, which is the right shape for a service configured entirely from a
    compose file and the wrong shape for a fixture — hence the reload.
    """
    monkeypatch.setenv("JOURNAL_DATABASE_URL", f"sqlite:///{tmp_path/'journal.db'}")
    monkeypatch.setenv("JOURNAL_SPACES", f"{SPACE_A},{SPACE_B}")
    sys.modules.pop("journal.server", None)
    module = importlib.import_module("journal.server")
    with TestClient(module.app) as client:
        yield client, module
    sys.modules.pop("journal.server", None)


def append(client, token, blobs, snapshot=False):
    resp = client.post(
        "/journal",
        headers={"Authorization": f"Bearer {token}"},
        json={"records": [{"blob": b, "snapshot": snapshot} for b in blobs]},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def read(client, token, since=0):
    resp = client.get("/journal", params={"since": since},
                      headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_healthz_needs_no_token(server):
    client, _ = server
    assert client.get("/healthz").json()["ok"] is True


def test_unknown_token_is_refused(server):
    client, _ = server
    assert client.get("/journal", headers={"Authorization": "Bearer nope"}).status_code == 401
    # And a missing header is not treated as an empty token.
    assert client.get("/journal").status_code == 401


def test_sequence_numbers_are_handed_out_in_order(server):
    """The one property the claim rule depends on.

    app/journal.py decides which install fires a due reminder by comparing these
    numbers, so "later append, higher number" is not a convenience here — it is
    the tie-break all three machines trust.
    """
    client, _ = server
    first = append(client, TOKEN_A, ["one"])["seqs"]
    second = append(client, TOKEN_A, ["two", "three"])["seqs"]
    assert first[0] < second[0] < second[1]

    page = read(client, TOKEN_A)
    assert [r["blob"] for r in page["records"]] == ["one", "two", "three"]
    assert page["next"] == second[1]


def test_since_returns_only_what_is_newer(server):
    client, _ = server
    seqs = append(client, TOKEN_A, ["a", "b", "c"])["seqs"]
    page = read(client, TOKEN_A, since=seqs[0])
    assert [r["blob"] for r in page["records"]] == ["b", "c"]


def test_spaces_cannot_see_each_other(server):
    """Two passphrases on one server are two journals, not one shared one."""
    client, _ = server
    append(client, TOKEN_A, ["private to A"])
    append(client, TOKEN_B, ["private to B"])
    assert [r["blob"] for r in read(client, TOKEN_A)["records"]] == ["private to A"]
    assert [r["blob"] for r in read(client, TOKEN_B)["records"]] == ["private to B"]


def test_server_stores_only_what_it_was_given(server):
    """No decoding, no inspection: the blob comes back byte for byte.

    Worth asserting because the whole argument for hosting this anywhere is that
    the server cannot read a record. A server that parsed one would be a server
    that could.
    """
    client, module = server
    blob = '{"not":"json to this server"}'
    append(client, TOKEN_A, [blob])
    assert read(client, TOKEN_A)["records"][0]["blob"] == blob
    with module.SessionLocal() as db:
        row = db.query(module.Record).one()
        assert row.blob == blob
        # The space is a hash of the token; the token itself is nowhere.
        assert row.space == SPACE_A


def test_oversized_blob_is_refused(server):
    """A ceiling checked by the schema, so it costs nothing and cannot be
    reached by a client in a loop."""
    client, module = server
    resp = client.post(
        "/journal", headers={"Authorization": f"Bearer {TOKEN_A}"},
        json={"records": [{"blob": "x" * (module.MAX_BLOB_BYTES + 1)}]},
    )
    assert resp.status_code == 422


def test_nothing_is_pruned_without_a_snapshot(server, monkeypatch):
    """Age alone is never a reason to delete a record.

    A reminder set four months ago for a date next week is old and entirely
    current, and the server cannot tell it from a record nobody needs — it cannot
    read either. So only a snapshot licenses deletion.
    """
    client, module = server
    monkeypatch.setattr(module, "RETAIN_DAYS", 0)      # everything is "old"
    monkeypatch.setattr(module, "_PRUNE_EVERY", 0)     # consider pruning now
    module._last_prune = 0.0
    append(client, TOKEN_A, ["old one"])
    append(client, TOKEN_A, ["another"])
    assert len(read(client, TOKEN_A)["records"]) == 2


def test_a_snapshot_lets_the_old_records_go(server, monkeypatch):
    client, module = server
    monkeypatch.setattr(module, "RETAIN_DAYS", 0)
    monkeypatch.setattr(module, "_PRUNE_EVERY", 0)
    module._last_prune = 0.0
    append(client, TOKEN_A, ["old one", "another"])
    module._last_prune = 0.0
    append(client, TOKEN_A, ["the snapshot"], snapshot=True)

    remaining = [r["blob"] for r in read(client, TOKEN_A)["records"]]
    assert remaining == ["the snapshot"]


def test_a_client_that_fell_off_the_end_is_told_to_replay(server, monkeypatch):
    """``reset`` is how an install that was off too long learns not to trust a
    partial page — see app/journal.py::pull."""
    client, module = server
    monkeypatch.setattr(module, "RETAIN_DAYS", 0)
    monkeypatch.setattr(module, "_PRUNE_EVERY", 0)
    module._last_prune = 0.0
    early = append(client, TOKEN_A, ["one", "two", "three"])["seqs"]
    module._last_prune = 0.0
    append(client, TOKEN_A, ["snapshot"], snapshot=True)

    # A machine still asking from before the pruned floor.
    assert read(client, TOKEN_A, since=early[0])["reset"] is True
    # One that is up to date is not told to replay anything.
    assert read(client, TOKEN_A, since=0)["reset"] is False
