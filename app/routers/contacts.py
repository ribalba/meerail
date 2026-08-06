"""Address autocomplete backed by the materialized contacts table."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session as DBSession

from core.config import get_settings
from ..contacts import rebuild_contact_pairs, rebuild_contacts
from core.database import get_db
from ..deps import require_ui_auth
from core.models import Contact, ContactPair

router = APIRouter(prefix="/api/contacts", tags=["contacts"], dependencies=[Depends(require_ui_auth)])
settings = get_settings()

MAX_SEEDS = 10          # a composer with more recipients than this needs no help
# One received mail carrying both names is a coincidence and scores 1, so the
# floor drops it. One mail the *user* addressed to both scores 2 and survives:
# putting two people on a message is a deliberate act, and doing it once is
# already an answer to "who goes with whom".
MIN_WEIGHT = 2


@router.get("")
def suggest(q: str = "", limit: int = Query(8, ge=1, le=25),
            db: DBSession = Depends(get_db)):
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


@router.get("/related")
def related(
    address: list[str] = Query(default=[]),
    limit: int = Query(4, ge=1, le=10),
    db: DBSession = Depends(get_db),
):
    """People usually addressed together with the given recipients.

    Ranked by co-occurrence weight damped by how widely the candidate turns up
    on its own: without that, the person you mail most would be suggested
    beside everyone. sqrt() rather than a plain divide, so a genuine frequent
    collaborator still outranks a one-off who happens to be obscure.
    """
    seeds: list[str] = []
    for raw in address:
        candidate = (raw or "").strip().lower()
        if candidate and "@" in candidate and candidate not in seeds:
            seeds.append(candidate)
        if len(seeds) >= MAX_SEEDS:
            break
    if not seeds:
        return []

    weight = func.sum(ContactPair.weight).label("weight")
    last_seen = func.max(ContactPair.last_seen).label("last_seen")
    score = weight / func.sqrt(func.greatest(Contact.count, 1))

    rows = db.execute(
        select(ContactPair.address_b, Contact.name, weight, last_seen)
        .join(Contact, Contact.address == ContactPair.address_b)
        .where(ContactPair.address_a.in_(seeds), ContactPair.address_b.notin_(seeds))
        .group_by(ContactPair.address_b, Contact.name, Contact.count)
        .having(weight >= MIN_WEIGHT)
        .order_by(score.desc(), last_seen.desc().nulls_last())
        .limit(limit)
    ).all()
    return [
        {"name": r.name, "address": r.address_b, "weight": int(r.weight)}
        for r in rows
    ]


@router.post("/refresh")
def refresh(years: int | None = Query(None, ge=0, le=200),
            db: DBSession = Depends(get_db)):
    """Rebuild the contacts index. `years` overrides the configured scan window."""
    y = years if years is not None else settings.contacts_scan_years
    count = rebuild_contacts(db, y)
    pairs = rebuild_contact_pairs(db, y)
    db.commit()          # both tables together — see app.contacts
    return {"count": count, "pairs": pairs, "years": y}
