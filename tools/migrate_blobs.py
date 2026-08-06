#!/usr/bin/env python3
"""Bring the files of the on-disk era into the database.

The first version of meerail kept message bodies as `.eml` files and attachment
payloads as files beside them, with the paths in `messages.raw_path` and
`attachments.disk_path`. Everything since keeps those bytes in Postgres, so that
the ingesting agent and the serving web app need no shared filesystem.

The upgrade between the two used to add the new columns and drop the old ones in
the same breath, copying nothing: the files stayed on disk with no row pointing
at them, and every attachment in the mailbox became "not stored". init_db no
longer drops a path column while it still holds anything, which leaves the
pointers intact and this script the way to follow them.

Run it on the machine that can see those paths — on a split deployment that is
not necessarily the one running the server. Dry run by default:

  tools/migrate_blobs.py                      # what it would copy
  tools/migrate_blobs.py --apply
  tools/migrate_blobs.py --apply --batch 200

Nothing on disk is deleted. Once every row has been copied the path column is
empty, and the next start of the server drops it; the files themselves are then
yours to remove when you are satisfied the mail is there.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy import text                                          # noqa: E402

from core.database import engine                                     # noqa: E402

# (table, path column, blob column, what one row is, in the words of the report)
TARGETS = (
    ("messages", "raw_path", "raw_mime", "message"),
    ("attachments", "disk_path", "content", "attachment"),
)


def _has_column(conn, table: str, column: str) -> bool:
    return conn.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = :table AND column_name = :column"
    ), {"table": table, "column": column}).scalar() is not None


def _pending(conn, table: str, column: str) -> int:
    return conn.execute(text(
        f"SELECT count(*) FROM {table} WHERE {column} IS NOT NULL"
    )).scalar() or 0


def migrate(table: str, path_column: str, blob_column: str, noun: str,
            apply: bool, batch: int) -> tuple[int, int]:
    """Copy one table's files in. Returns (copied, unreadable).

    Batched and committed as it goes, because a mailbox of any size is more bytes
    than one transaction wants to hold, and a run interrupted halfway should
    leave everything it managed rather than nothing.

    A file that cannot be read keeps its path. That row is the only remaining
    record that the content ever existed and where it was, so it is reported and
    left alone — the file may be on a disk that is not mounted yet, and dropping
    the pointer would turn "come back with the volume attached" into "gone".
    """
    with engine.connect() as conn:
        if not _has_column(conn, table, path_column):
            return 0, 0
        total = _pending(conn, table, path_column)
    if not total:
        return 0, 0

    print(f"{table}: {total} {noun}(s) still on disk")
    copied = unreadable = 0
    seen: set[int] = set()
    while True:
        with engine.connect() as conn:
            rows = conn.execute(text(
                f"SELECT id, {path_column} FROM {table} "
                f"WHERE {path_column} IS NOT NULL AND id <> ALL(:seen) "
                f"ORDER BY id LIMIT :limit"
            ), {"seen": list(seen) or [0], "limit": batch}).all()
        if not rows:
            break

        for row_id, path in rows:
            seen.add(row_id)
            try:
                data = Path(path).read_bytes()
            except OSError as exc:
                unreadable += 1
                print(f"  ! {table} {row_id}: {path} — {exc.strerror or exc}")
                continue
            copied += 1
            if not apply:
                continue
            with engine.begin() as conn:
                # The blob column wins only where it is still empty: a row that
                # has been re-fetched since carries the current bytes, and the
                # file is the older copy of the two.
                conn.execute(text(
                    f"UPDATE {table} SET {blob_column} = COALESCE({blob_column}, :data), "
                    f"{path_column} = NULL WHERE id = :id"
                ), {"data": data, "id": row_id})
    return copied, unreadable


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="actually copy; without it nothing is written")
    ap.add_argument("--batch", type=int, default=100,
                    help="rows per pass (default 100) — lower it if memory is tight")
    args = ap.parse_args()

    copied = unreadable = 0
    for table, path_column, blob_column, noun in TARGETS:
        got, missed = migrate(table, path_column, blob_column, noun, args.apply, args.batch)
        copied += got
        unreadable += missed

    if not copied and not unreadable:
        print("Nothing to do: no rows point at files on disk.")
        return 0
    if args.apply:
        print(f"\nCopied {copied} file(s) into the database.")
    else:
        print(f"\nWould copy {copied} file(s). Nothing was written — re-run with --apply.")
    if unreadable:
        print(f"{unreadable} file(s) could not be read; those rows keep their path so the "
              f"run can be repeated once the files are reachable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
