"""Agent configuration, loaded from a TOML file.

Path resolution: $MEERAIL_AGENT_CONFIG, else ./config.toml next to the agent.
Bridge credentials live here (on the host), never on the server.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent


@dataclass
class AccountConfig:
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


@dataclass
class AgentConfig:
    server_url: str = "http://localhost:8000"
    agent_token: str = ""
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

    return AgentConfig(
        server_url=data.get("server_url", "http://localhost:8000").rstrip("/"),
        agent_token=data.get("agent_token", ""),
        poll_interval=int(data.get("poll_interval", 30)),
        batch_size=int(data.get("batch_size", 200)),
        accounts=accounts,
    )
