"""The two Message columns that must never ride along on a plain entity load.

`raw_mime` holds the original RFC822 bytes — attachments included, base64'd —
and `search_text` folds in every attachment's extracted text. Nothing outside
ingest reads either one, but the thread view, archive, trash and rethread all
load mail with `select(Message)`, so an eager column here is not a few wasted
bytes: one real four-message thread carries 130MB of raw_mime, which the reader
was reading, detoasting and materialising to render 14kB of HTML — a ~17 second
hang with a pooled connection held throughout.

Pure unit test: compiling the statement is enough, no database needed.
"""

from sqlalchemy import select

from core.models import Message

DEFERRED = ("raw_mime", "search_text")


def test_entity_load_omits_the_heavy_columns():
    # The emitted SQL, not `selected_columns`: the latter reports every mapped
    # column whether or not the loader will actually fetch it, so it would pass
    # with the deferral removed.
    sql = str(select(Message))
    for name in DEFERRED:
        assert name not in sql, f"{name} must stay deferred on a plain entity load"
    # Guard against the opposite mistake — deferring the body itself would make
    # every message in a thread a separate round trip.
    assert "body_html" in sql and "body_text" in sql


def test_deferred_columns_are_still_queryable():
    """Deferring changes what an entity load fetches, not what SQL can ask for.

    The search endpoints filter on `search_text` in the WHERE clause and ingest
    writes both columns; neither goes through the loaded attribute.
    """
    for name in DEFERRED:
        assert name in str(select(getattr(Message, name)))
