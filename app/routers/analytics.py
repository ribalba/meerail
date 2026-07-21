"""Lightweight analytics over the mail store (enabled by keeping everything in
Postgres). More can be added later; these back a future stats view."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import String, cast, func, select
from sqlalchemy.orm import Session as DBSession

from ..database import get_db
from ..deps import require_ui_auth
from ..models import Message, Recipient

router = APIRouter(prefix="/api/analytics", tags=["analytics"], dependencies=[Depends(require_ui_auth)])


@router.get("/summary")
def summary(db: DBSession = Depends(get_db), account_id: int | None = None):
    msg_q = select(func.count()).select_from(Message)
    thr_q = select(func.count(func.distinct(Message.thread_id)))
    if account_id is not None:
        msg_q = msg_q.where(Message.account_id == account_id)
        thr_q = thr_q.where(Message.account_id == account_id)
    return {
        "messages": int(db.scalar(msg_q) or 0),
        "threads": int(db.scalar(thr_q) or 0),
    }


@router.get("/top-senders")
def top_senders(db: DBSession = Depends(get_db), account_id: int | None = None, limit: int = 10):
    q = (
        select(Message.from_addr, func.count().label("n"),
               func.max(Message.from_name).label("name"))
        .where(Message.from_addr != "")
        .group_by(Message.from_addr)
        .order_by(func.count().desc())
        .limit(limit)
    )
    if account_id is not None:
        q = q.where(Message.account_id == account_id)
    return [{"address": a, "name": name, "count": n} for a, n, name in db.execute(q).all()]


@router.get("/volume")
def volume(db: DBSession = Depends(get_db), account_id: int | None = None):
    month = func.to_char(func.date_trunc("month", Message.date_sent), "YYYY-MM")
    q = (
        select(month.label("month"), func.count().label("n"))
        .where(Message.date_sent.is_not(None))
        .group_by(month)
        .order_by(month)
    )
    if account_id is not None:
        q = q.where(Message.account_id == account_id)
    return [{"month": m, "count": n} for m, n in db.execute(q).all()]
