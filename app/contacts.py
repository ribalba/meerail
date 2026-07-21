"""Build the materialized contacts list from the recipients table.

Rebuilt in full (it's a small derived table — one row per distinct address you've
corresponded with) so that autocomplete stays instant even over a 10GB mailbox.
The scan window is configurable (contacts_scan_years); a 0 means all time. Your
own account addresses are excluded.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.models import utcnow


def rebuild_contacts(db: Session, years: int) -> int:
    """Repopulate the contacts table; returns the number of contacts."""
    params: dict = {"now": utcnow()}
    date_clause = ""
    if years and years > 0:
        params["cutoff"] = utcnow() - timedelta(days=365 * years)
        date_clause = "AND m.date_sent >= :cutoff"

    db.execute(text("DELETE FROM contacts"))
    db.execute(
        text(
            f"""
            INSERT INTO contacts (address, name, count, last_seen, updated_at)
            SELECT
                r.address,
                COALESCE(
                    (array_agg(r.name ORDER BY m.date_sent DESC NULLS LAST)
                        FILTER (WHERE r.name <> ''))[1], '') AS name,
                count(*) AS count,
                max(m.date_sent) AS last_seen,
                :now AS updated_at
            FROM recipients r
            JOIN messages m ON m.id = r.message_pk
            WHERE r.address <> '' {date_clause}
              AND r.address NOT IN (SELECT lower(email) FROM accounts)
            GROUP BY r.address
            """
        ),
        params,
    )
    db.commit()
    return int(db.scalar(text("SELECT count(*) FROM contacts")) or 0)
