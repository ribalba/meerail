"""Agent configuration, loaded from a TOML file.

Path resolution: $MEERAIL_AGENT_CONFIG, else ./config.toml next to the agent.
Bridge credentials live here (on the host) alongside the database and Tika
endpoints the agent writes through.

``load_config`` exports the database/Tika settings into the environment, because
``core.config`` reads them from there. Call it before importing anything from
``core`` — see main.py.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent

SECURITY_MODES = ("starttls", "ssl", "plain")


@dataclass
class AccountConfig:
    """One mail account and the Bridge (or IMAP/SMTP) endpoints serving it."""

    email: str
    imap_host: str = "127.0.0.1"
    imap_port: int = 1143
    imap_security: str = "starttls"   # starttls | ssl | plain
    smtp_host: str = "127.0.0.1"
    smtp_port: int = 1025
    smtp_security: str = "starttls"
    username: str = ""
    password: str = ""
    verify_cert: bool = False          # Proton Bridge uses a self-signed cert
    # Socket timeouts for the IMAP connection, in seconds. Without these a
    # Bridge that stops answering mid-pass parks the sync thread in recv()
    # forever: no exception, so nothing is logged and no error is recorded, and
    # the UI reports the account as "offline" once last_agent_seen ages out —
    # blaming a dead agent for a process that is alive and merely deaf.
    #
    # These are per-operation, not per-command: a large fetch only trips
    # imap_read_timeout if Bridge sends *nothing* for that long, so the read
    # value bounds a stall, not a slow transfer. It still has to tolerate
    # Bridge pausing mid-fetch while it pulls and decrypts a large message.
    imap_connect_timeout: int = 10
    imap_read_timeout: int = 60
    # UIDs per fetch/ingest batch for this account, overriding the global
    # batch_size. None means "use the global". Servers differ in how large an
    # ask they will actually answer: Gmail meets a big BODY.PEEK[] fetch with a
    # partial response or an outright disconnect often enough that a backfill
    # spends its time restarting, and asking for less is what gets it finished.
    batch_size: int | None = None
    # Extra "send as" addresses this account owns (Proton aliases / additional
    # addresses). The primary `email` is always sendable and need not be listed.
    addresses: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Normalise casing before anyone compares against it. imap.py/smtp.py
        # test these strings exactly, so a config saying "STARTTLS" used to fall
        # through to an unencrypted socket and send the password in the clear —
        # silently, because Bridge accepts it. Unknown values are rejected rather
        # than defaulted, for the same reason.
        for attr in ("imap_security", "smtp_security"):
            value = (getattr(self, attr) or "").strip().lower()
            if value not in SECURITY_MODES:
                raise ValueError(
                    f"{self.email}: {attr} = {getattr(self, attr)!r} is not valid; "
                    f"use one of {', '.join(SECURITY_MODES)}"
                )
            setattr(self, attr, value)
        if self.batch_size is not None and self.batch_size < 1:
            raise ValueError(f"batch_size = {self.batch_size!r} must be at least 1")

    def send_addresses(self) -> list[str]:
        """Every address the account may send from — primary first, deduped."""
        out = [self.email]
        for a in self.addresses:
            a = a.strip()
            if a and a.lower() not in {x.lower() for x in out}:
                out.append(a)
        return out


@dataclass
class AgentConfig:
    """Top-level agent settings."""

    database_url: str = "postgresql+psycopg://meerail:meerail@127.0.0.1:5432/meerail"
    tika_url: str = "http://127.0.0.1:9998"
    poll_interval: int = 30
    # How often the sweep for flag changes and vanished mail runs. Much longer
    # than poll_interval on purpose: it is the expensive part of a pass, and new
    # mail does not wait on it.
    reconcile_interval: int = 900
    batch_size: int = 200
    # Keep each message's original RFC822 bytes in messages.raw_mime. Nothing
    # reads them yet — they are kept for future features — and they are roughly
    # half the database, so a tight disk can turn them off. Only applies to mail
    # ingested from then on; existing rows keep theirs.
    store_raw_mime: bool = True
    # Only fetch and keep the *content* of mail sent within this many months;
    # 0 keeps everything. Older messages are stored as headers alone — they
    # still list, thread and answer a search by subject or correspondent — and
    # stored mail is stripped back to headers as the window slides past it.
    content_window_months: int = 0
    accounts: list[AccountConfig] = field(default_factory=list)
    # Where this was loaded from, so --test can check its permissions.
    config_path: Path | None = None


def _as_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_int(value: object, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_config(path: str | None = None) -> AgentConfig:
    cfg_path = Path(path or os.environ.get("MEERAIL_AGENT_CONFIG", HERE / "config.toml"))
    if not cfg_path.exists():
        raise SystemExit(f"Config not found: {cfg_path}\nCopy config.example.toml to config.toml.")
    with open(cfg_path, "rb") as fh:
        data = tomllib.load(fh)

    accounts = []
    for a in data.get("account", []):
        a = dict(a)
        a.setdefault("username", a["email"])
        try:
            accounts.append(AccountConfig(**a))
        except (ValueError, TypeError) as exc:
            raise SystemExit(f"Bad [[account]] in {cfg_path}: {exc}") from exc

    cfg = AgentConfig(
        database_url=data.get("database_url", AgentConfig.database_url),
        tika_url=data.get("tika_url", AgentConfig.tika_url),
        poll_interval=int(data.get("poll_interval", 30)),
        reconcile_interval=int(data.get("reconcile_interval", 900)),
        batch_size=int(data.get("batch_size", 200)),
        # The one setting with two homes: config.toml for a native agent, and
        # $STORE_RAW_MIME for the containerised one, which docker-compose passes
        # through. The file wins where it says anything, so a container that has
        # both does not quietly ignore the config the user edited.
        store_raw_mime=_as_bool(
            data.get("store_raw_mime", os.environ.get("STORE_RAW_MIME")), True
        ),
        # Same two homes, same precedence — see store_raw_mime above.
        content_window_months=_as_int(
            data.get("content_window_months", os.environ.get("CONTENT_WINDOW_MONTHS")), 0
        ),
        accounts=accounts,
        config_path=cfg_path,
    )

    # core.config reads these from the environment; an explicit value in the
    # environment still wins, which keeps `make dev` and tests overridable.
    os.environ.setdefault("DATABASE_URL", cfg.database_url)
    os.environ.setdefault("TIKA_URL", cfg.tika_url)
    # Not setdefault: the value above already folded in whatever the environment
    # said, and core.config must see the resolved answer, not the raw input.
    os.environ["STORE_RAW_MIME"] = "true" if cfg.store_raw_mime else "false"
    os.environ["CONTENT_WINDOW_MONTHS"] = str(cfg.content_window_months)
    return cfg
