import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import events
from .config import get_settings
from .database import init_db
from .routers import (
    accounts, actions, agent, analytics, auth, compose, contacts, mailboxes, messages, search,
    stream, sync,
)
from .workers import contacts_loop, extraction_loop

settings = get_settings()
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="meerail", version="0.2.0")


@app.on_event("startup")
async def _startup() -> None:
    init_db()
    events.set_loop(asyncio.get_running_loop())
    asyncio.create_task(extraction_loop())
    asyncio.create_task(contacts_loop())


app.include_router(accounts.router)
app.include_router(auth.router)
app.include_router(agent.router)
app.include_router(mailboxes.router)
app.include_router(messages.router)
app.include_router(actions.router)
app.include_router(compose.router)
app.include_router(contacts.router)
app.include_router(search.router)
app.include_router(analytics.router)
app.include_router(sync.router)
app.include_router(stream.router)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


# Static assets (css/js/img).
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
