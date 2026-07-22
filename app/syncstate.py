"""How an account's agent is classified for the status panel.

Split out of `routers/sync.py` rather than living beside the endpoint because
this is pure logic over a row — no request, no session — and the router pulls in
FastAPI, which the test venv deliberately does not have (the server runs in
Docker; the suite talks to it over HTTP). Keeping the classification importable
on its own is what lets it be unit-tested without a stack behind it.
"""

from datetime import datetime, timezone

# How long an account may go without a sign of life before the UI calls the
# agent offline. Two limits, because "quiet" means different things:
#
#   HEALTHY — a working agent stamps last_agent_seen once per pass, i.e. every
#     poll_interval (30s by default), so three minutes of silence is already
#     well outside normal.
#   BACKING OFF — a failing agent retries on a backoff that tops out at 300s
#     (agent/sync.py), so it can legitimately be quiet for five minutes. Judging
#     it by the healthy limit would report "offline" for an agent that is very
#     much running and telling us exactly what is wrong.
STALE_AFTER_HEALTHY = 180
STALE_AFTER_FAILING = 600


def account_state(acc, last_ingest, now) -> tuple[str, str]:
    """Classify one account's agent into a state + a sentence explaining it.

    Decided here rather than in the browser so every consumer agrees, and so the
    thresholds sit next to the agent behaviour they are derived from.
    """
    if acc.last_agent_seen is None:
        return "never", "No agent has ever connected for this account."

    # A pass that is mid-flight stores messages without re-stamping
    # last_agent_seen (that happens once, at pass start). During the initial
    # backfill a single pass can run for many minutes, so mail landing in the
    # database counts as a sign of life in its own right — otherwise a busy
    # agent would be reported offline precisely while it works hardest.
    seen = max([t for t in (acc.last_agent_seen, last_ingest) if t is not None])
    quiet = (now - seen).total_seconds()
    limit = STALE_AFTER_FAILING if acc.last_error else STALE_AFTER_HEALTHY

    if quiet > limit:
        return "offline", (
            f"No sign of the agent for {human(quiet)}. It is probably not running."
        )
    # A pass that is open and still moving outranks last_error, which describes a
    # pass that has already ended. The agent clears the error as soon as it has
    # reconnected, so the two rarely disagree — but when they do, the live pass
    # is the newer fact, and reporting "failing" over a bar that is visibly
    # advancing is the more confusing of the two ways to be wrong.
    if not pass_advancing(acc, now) and acc.last_error:
        return "failing", (
            "The agent is running but its last sync pass failed. It keeps retrying "
            "on a backoff."
        )
    if not acc.backfill_complete:
        return "backfilling", "First full sync is still running."
    return "ok", "Syncing normally."


def pass_advancing(acc, now) -> bool:
    """True if a sync pass is open and has written progress recently.

    Staleness is judged by the healthy limit: the agent rewrites the blob at
    every folder boundary and at every batch within a folder, so a pass that has
    gone quiet for minutes is wedged rather than working, and must not be allowed
    to suppress the error that explains it.
    """
    progress = acc.sync_progress
    if not isinstance(progress, dict) or not progress.get("active"):
        return False
    stamp = progress.get("updated_at") or progress.get("started_at")
    try:
        written = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        # The blob is advisory and agent-written; a shape this code does not
        # recognise must fall back to the error, never mask it.
        return False
    if written.tzinfo is not None:
        written = written.astimezone(timezone.utc).replace(tzinfo=None)
    return (now - written).total_seconds() <= STALE_AFTER_HEALTHY


def human(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}m"
    return f"{int(seconds // 3600)}h"
