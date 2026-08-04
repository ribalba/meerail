"""Build the materialized contacts list from the recipients table.

Rebuilt in full (it's a small derived table — one row per distinct address you've
corresponded with) so that autocomplete stays instant even over a 10GB mailbox.
The scan window is configurable (contacts_scan_years); a 0 means all time. Your
own account addresses are excluded.

The co-recipient graph (contact_pairs, "who do I write to together") is built in
the same pass and over the same window, because the two are read together and a
half-updated pair of tables would rank suggestions against stale totals.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.models import utcnow

# Above this many participants a message stops being a group of people who know
# each other and becomes a broadcast. Pairs grow with the square of that number,
# so the cap keeps the table small *and* keeps one 40-person announcement from
# making every recipient suggest every other one forever.
MAX_PARTICIPANTS = 12


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


def rebuild_contact_pairs(db: Session, years: int) -> int:
    """Repopulate contact_pairs; returns the number of ordered pairs.

    Both directions are stored (a→b and b→a) so that serving a suggestion is a
    single indexed lookup on address_a for whoever is already in the composer,
    with no OR across two columns.
    """
    params: dict = {"now": utcnow(), "max_participants": MAX_PARTICIPANTS}
    date_clause = ""
    if years and years > 0:
        params["cutoff"] = utcnow() - timedelta(days=365 * years)
        date_clause = "AND m.date_sent >= :cutoff"

    db.execute(text("DELETE FROM contact_pairs"))
    db.execute(
        text(
            f"""
            WITH parts AS (
                -- One row per (message, person). DISTINCT because someone named
                -- in both To and Cc, or in From and Reply-To, is still one
                -- participant and must not count as two.
                SELECT DISTINCT r.message_pk, r.address
                FROM recipients r
                JOIN messages m ON m.id = r.message_pk
                WHERE r.address <> '' {date_clause}
                  AND r.address NOT IN (SELECT lower(email) FROM accounts)
            ),
            kept AS (
                SELECT message_pk FROM parts
                GROUP BY message_pk
                HAVING count(*) BETWEEN 2 AND :max_participants
            ),
            sent AS (
                -- Mail the user sent: the From is one of their own addresses.
                SELECT DISTINCT r.message_pk
                FROM recipients r
                WHERE r.kind = 'from'
                  AND r.address IN (SELECT lower(email) FROM accounts)
            )
            INSERT INTO contact_pairs
                (address_a, address_b, count, weight, last_seen, updated_at)
            SELECT
                a.address,
                b.address,
                count(*) AS count,
                sum(CASE WHEN s.message_pk IS NOT NULL THEN 2 ELSE 1 END) AS weight,
                max(m.date_sent) AS last_seen,
                :now AS updated_at
            FROM parts a
            JOIN kept k ON k.message_pk = a.message_pk
            JOIN parts b ON b.message_pk = a.message_pk AND b.address <> a.address
            JOIN messages m ON m.id = a.message_pk
            LEFT JOIN sent s ON s.message_pk = a.message_pk
            GROUP BY a.address, b.address
            """
        ),
        params,
    )
    db.commit()
    return int(db.scalar(text("SELECT count(*) FROM contact_pairs")) or 0)
