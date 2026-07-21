"""Background workers started on server startup.

extraction_loop: drains attachments with extract_status='pending', runs them
through Tika, stores the text, and rebuilds the parent message's search_text so
regex search covers attachment contents.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from . import events
from .config import get_settings
from .contacts import rebuild_contacts
from .database import SessionLocal
from .mail import tika
from .mail.store import rebuild_search_text
from .models import Attachment, Message

settings = get_settings()

BATCH = 8


def _process_pending_batch() -> int:
    db = SessionLocal()
    try:
        pending = db.execute(
            select(Attachment).where(Attachment.extract_status == "pending").limit(BATCH)
        ).scalars().all()
        if not pending:
            return 0

        touched_messages: set[int] = set()
        for att in pending:
            text = ""
            if att.disk_path:
                try:
                    with open(att.disk_path, "rb") as fh:
                        payload = fh.read()
                    text = tika.extract_text(payload, att.content_type)
                except FileNotFoundError:
                    text = ""
            att.extracted_text = text or None
            att.extract_status = "done" if text else "error"
            touched_messages.add(att.message_pk)

        # autoflush is off on this session, so push the extracted_text values to
        # the DB before rebuild_search_text re-reads them, or it sees stale NULLs.
        db.flush()

        for message_pk in touched_messages:
            msg = db.get(Message, message_pk)
            if msg is None:
                continue
            rebuild_search_text(db, msg)
            still_pending = db.scalar(
                select(Attachment.id)
                .where(Attachment.message_pk == message_pk, Attachment.extract_status == "pending")
                .limit(1)
            )
            if not still_pending:
                msg.extract_status = "done"

        db.commit()
        events.publish({"type": "extract", "processed": len(pending)})
        return len(pending)
    finally:
        db.close()


async def extraction_loop() -> None:
    while True:
        try:
            n = await asyncio.to_thread(_process_pending_batch)
        except Exception:
            n = 0
        await asyncio.sleep(0.2 if n else 3.0)


def _rebuild_contacts_once() -> int:
    db = SessionLocal()
    try:
        return rebuild_contacts(db, settings.contacts_scan_years)
    finally:
        db.close()


async def contacts_loop() -> None:
    # Build once at startup (so autocomplete works immediately), then refresh
    # periodically to pick up newly-synced mail.
    while True:
        try:
            await asyncio.to_thread(_rebuild_contacts_once)
        except Exception:
            pass
        await asyncio.sleep(6 * 3600)
