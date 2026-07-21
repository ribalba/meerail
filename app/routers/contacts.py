"""Address autocomplete backed by the materialized contacts table."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session as DBSession

from ..config import get_settings
from ..contacts import rebuild_contacts
from ..database import get_db
from ..deps import require_ui_auth
from ..models import Contact

router = APIRouter(prefix="/api/contacts", tags=["contacts"], dependencies=[Depends(require_ui_auth)])
settings = get_settings()


@router.get("")
def suggest(q: str = "", limit: int = Query(8, le=25), db: DBSession = Depends(get_db)):
    q = q.strip().replace("%", "").replace("_", "")
    if not q:
        return []
    rows = db.execute(
        select(Contact)
        .where(or_(Contact.address.ilike(f"%{q}%"), Contact.name.ilike(f"%{q}%")))
        .order_by(Contact.count.desc(), Contact.last_seen.desc().nulls_last())
        .limit(limit)
    ).scalars().all()
    return [{"name": c.name, "address": c.address, "count": c.count} for c in rows]


@router.post("/refresh")
def refresh(years: int | None = None, db: DBSession = Depends(get_db)):
    """Rebuild the contacts index. `years` overrides the configured scan window."""
    y = years if years is not None else settings.contacts_scan_years
    count = rebuild_contacts(db, y)
    return {"count": count, "years": y}
