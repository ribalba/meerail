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
    # Extra "send as" addresses this account owns (Proton aliases / additional
    # addresses). The primary `email` is always sendable and need not be listed.
    addresses: list[str] = field(default_factory=list)

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
    batch_size: int = 200
    accounts: list[AccountConfig] = field(default_factory=list)


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
        accounts.append(AccountConfig(**a))

    cfg = AgentConfig(
        database_url=data.get("database_url", AgentConfig.database_url),
        tika_url=data.get("tika_url", AgentConfig.tika_url),
        poll_interval=int(data.get("poll_interval", 30)),
        batch_size=int(data.get("batch_size", 200)),
        accounts=accounts,
    )

    # core.config reads these from the environment; an explicit value in the
    # environment still wins, which keeps `make dev` and tests overridable.
    os.environ.setdefault("DATABASE_URL", cfg.database_url)
    os.environ.setdefault("TIKA_URL", cfg.tika_url)
    return cfg
