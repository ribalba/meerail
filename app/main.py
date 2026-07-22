import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import events
from core.config import get_settings
from core.database import engine, init_db
from .routers import (
    accounts, actions, analytics, auth, compose, contacts, mailboxes, messages, search,
    stream, sync, tasks,
)
from .workers import contacts_loop

settings = get_settings()
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="meerail", version="0.2.0")


@app.on_event("startup")
async def _startup() -> None:
    init_db()
    events.set_loop(asyncio.get_running_loop())
    # Mail ingest (and its Tika extraction) runs in the agent; the app only
    # listens for the resulting change notifications.
    asyncio.create_task(events.listener_loop())
    asyncio.create_task(contacts_loop())


app.include_router(accounts.router)
app.include_router(auth.router)
app.include_router(mailboxes.router)
app.include_router(messages.router)
app.include_router(actions.router)
app.include_router(compose.router)
app.include_router(contacts.router)
app.include_router(search.router)
app.include_router(analytics.router)
app.include_router(sync.router)
app.include_router(tasks.router)
app.include_router(stream.router)


@app.get("/healthz")
def healthz() -> dict:
    # The database name (never the URL — that carries the password) lets the
    # test suite refuse to assert against a production server; see
    # tests/conftest.py::pytest_configure.
    return {"ok": True, "database": engine.url.database}


class NoCacheStatic(StaticFiles):
    """Static assets that always revalidate.

    Without a Cache-Control header browsers fall back to heuristic freshness
    (roughly a tenth of the file's age), so a long-untouched js file can be
    served from cache for hours while a freshly edited one is refetched. The
    front end is a set of modules that call into each other, so half-stale is
    worse than stale: app.keys.js calling an App.reader function its cached
    copy does not have yet is a TypeError, not a missing feature. ETags still
    make the revalidation a 304, so this costs a round trip, not the payload.
    """

    def file_response(self, *args, **kwargs) -> Response:
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


# Static assets (css/js/img).
app.mount("/static", NoCacheStatic(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    # Same reasoning as NoCacheStatic: the shell must not pin an old asset set.
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})
