"""Configuration for the whole system — web app and agent, one file.

Everything lives in a single ``meerail.toml``, and every setting in it can be
overridden by an environment variable of the same name. Precedence, highest
first::

    constructor arg  >  environment  >  .env  >  meerail.toml  >  default

The file is optional. A server handed DATABASE_URL and SECRET_KEY in its
environment runs with no file at all — that is the remote deployment, where the
compose file simply drops the bind mount. The agent needs one, because
``[[agent.account]]`` is the only place mailbox credentials are configured.

Path resolution: ``$MEERAIL_CONFIG``, else ``meerail.toml`` at the repository
root (``/app/meerail.toml`` in both images). Setting ``MEERAIL_CONFIG`` to the
empty string means "environment only" and skips both files — which is how the
test suite keeps a developer's own configuration out of a test run.

The environment wins over the file on purpose, and it is the reason a container
must not set a variable it does not mean: an ``environment:`` entry with a
``${VAR:-default}`` fallback is *always* set, so it would override the file
whether or not the operator asked for anything. Compose passes only the handful
of values that are genuinely container topology (the database address, /data).
"""

from __future__ import annotations

import os
import tomllib
from email.utils import parseaddr
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = BASE_DIR / "meerail.toml"
EXAMPLE_CONFIG_PATH = BASE_DIR / "meerail.example.toml"

# Read only by the migration command below — the loader itself never looks at it.
LEGACY_AGENT_CONFIG = BASE_DIR / "agent" / "config.toml"

SECURITY_MODES = ("starttls", "ssl", "plain")

# Account display fields this file may take over from the UI, in the order the
# Settings modal shows them. See AccountConfig.presentation.
PRESENTATION_FIELDS = ("label", "color", "footer")

# How much of each the accounts table holds; `footer` is TEXT and so unbounded.
PRESENTATION_WIDTHS = {"label": 200, "color": 32}


class AccountConfig(BaseModel):
    """One mail account and the Bridge (or IMAP/SMTP) endpoints serving it."""

    # A mistyped key here used to be silently dropped, which is the whole class
    # of bug this file exists to end: reject it instead.
    model_config = ConfigDict(extra="forbid")

    email: str
    imap_host: str = "127.0.0.1"
    imap_port: int = 1143
    imap_security: str = "starttls"   # starttls | ssl | plain
    smtp_host: str = "127.0.0.1"
    smtp_port: int = 1025
    smtp_security: str = "starttls"
    username: str = ""                 # defaults to `email`
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
    # The same guarantee for the send path. smtplib's own default is no timeout,
    # which builds a blocking socket; the send drain runs on the sync thread, so
    # one silent SMTP server would stop the account syncing at all. See
    # smtp.connect.
    smtp_timeout: int = 60
    # UIDs per fetch/ingest batch for this account, overriding the global
    # batch_size. None means "use the global". Servers differ in how large an
    # ask they will actually answer: Gmail meets a big BODY.PEEK[] fetch with a
    # partial response or an outright disconnect often enough that a backfill
    # spends its time restarting, and asking for less is what gets it finished.
    batch_size: int | None = None
    # The name recipients see in front of the address — the display name on the
    # From header of everything this account sends. Empty sends the bare
    # address, which is what every account did before this existed. Per-address
    # names override it; see `addresses` below.
    name: str = ""
    # Extra "send as" addresses this account owns (Proton aliases / additional
    # addresses). The primary `email` is always sendable and need not be listed.
    #
    # An entry is either a bare address or the RFC 5322 form `Name <addr>`,
    # which gives that one address a display name of its own instead of the
    # account-wide `name`. Listing the primary here is otherwise pointless but
    # is allowed, and is how it gets a name different from its siblings'.
    addresses: list[str] = []

    # --- presentation, normally owned by the UI ------------------------------
    # The three things Settings lets you edit about an account: its name in the
    # sidebar, the colour of its dot, and the footer the composer prefills.
    # Unset (the default) is the old behaviour and the usual one — the value
    # lives in the database and Settings owns it.
    #
    # Set one here and the file takes it over: the agent writes it onto the row
    # on every pass, so an edit here wins over whatever Settings last saved, and
    # the field is shown as configured-elsewhere rather than editable. That is
    # for the installs whose accounts are provisioned from a file rather than by
    # hand. Removing a key again hands the field back to Settings, keeping the
    # value the file last gave it.
    #
    # `label` is the account's own name in the UI and never leaves it — unlike
    # `name` above, which is the display name recipients see on the From header.
    label: str | None = None
    color: str | None = None       # hex ("#1d6ff2") or a CSS colour name
    footer: str | None = None      # empty string = this account has no footer

    @field_validator("imap_security", "smtp_security")
    @classmethod
    def _known_security_mode(cls, value: str, info) -> str:
        # Normalise casing before anyone compares against it. imap.py/smtp.py
        # test these strings exactly, so a config saying "STARTTLS" used to fall
        # through to an unencrypted socket and send the password in the clear —
        # silently, because Bridge accepts it. Unknown values are rejected rather
        # than defaulted, for the same reason.
        mode = (value or "").strip().lower()
        if mode not in SECURITY_MODES:
            raise ValueError(
                f"{info.field_name} = {value!r} is not valid; "
                f"use one of {', '.join(SECURITY_MODES)}"
            )
        return mode

    @field_validator("batch_size")
    @classmethod
    def _positive_batch_size(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError(f"batch_size = {value!r} must be at least 1")
        return value

    @field_validator("addresses")
    @classmethod
    def _parseable_addresses(cls, value: list[str]) -> list[str]:
        # `Name <addr>` is accepted here, so an entry is no longer self-evidently
        # an address and a typo can no longer be assumed to be one. Anything with
        # no address in it would otherwise travel all the way to the From header
        # and go out as a mailbox nobody can reply to.
        for raw in value:
            _, addr = parseaddr(raw or "")
            if "@" not in addr:
                raise ValueError(
                    f"addresses entry {raw!r} has no email address in it; "
                    f"write either 'alias@example.com' or 'Your Name <alias@example.com>'"
                )
        return value

    @field_validator("label", "color")
    @classmethod
    def _fits_its_column(cls, value: str | None, info) -> str | None:
        # These land in fixed-width columns (see core.models.Account). Caught
        # here, the operator gets the file and the key; caught by Postgres, they
        # get a psycopg error on the agent's first pass naming neither.
        limit = PRESENTATION_WIDTHS[info.field_name]
        if value is not None and len(value) > limit:
            raise ValueError(
                f"{info.field_name} is {len(value)} characters; "
                f"the limit is {limit}"
            )
        return value

    @model_validator(mode="after")
    def _username_defaults_to_email(self) -> AccountConfig:
        if not self.username:
            self.username = self.email
        return self

    def send_identities(self) -> list[tuple[str, str]]:
        """Every mailbox the account may send from, as (display name, address).

        Primary first, then `addresses` in order, deduped case-insensitively on
        the address — the first spelling of an address is the one kept. A repeat
        is not simply dropped, though: if it carries a display name and the
        entry already held does not, the name is taken. That is what makes
        listing the primary under `addresses` a way to name it.

        Names fall back to the account-wide `name`, which may itself be empty —
        an empty name means the address goes out on its own, with no display
        name at all.
        """
        out: list[list[str]] = []
        at: dict[str, int] = {}
        for raw in [self.email, *self.addresses]:
            name, addr = parseaddr(raw or "")
            name, addr = name.strip(), addr.strip()
            if not addr:
                continue
            key = addr.lower()
            if key in at:
                if name and not out[at[key]][0]:
                    out[at[key]][0] = name
                continue
            at[key] = len(out)
            out.append([name, addr])
        return [(name or self.name.strip(), addr) for name, addr in out]

    def send_addresses(self) -> list[str]:
        """Every address the account may send from — primary first, deduped."""
        return [addr for _, addr in self.send_identities()]

    def presentation(self) -> dict[str, str]:
        """The display fields this block pins, keyed by column name.

        Only the keys actually written in the file are in here — a field left
        out is absent rather than empty, which is what keeps "no footer" (an
        empty string) distinguishable from "Settings owns the footer".
        """
        return {
            field: value
            for field in PRESENTATION_FIELDS
            if (value := getattr(self, field)) is not None
        }


# meerail.toml is grouped into sections for the reader's sake; the settings
# themselves are flat, so that one environment variable name maps to one setting
# with no delimiter convention to learn. This is that mapping — TOML key on the
# left, field (and so upper-cased environment variable) on the right.
_SECTION_KEYS: dict[str, dict[str, str]] = {
    "database": {
        "url": "database_url",
    },
    "server": {
        "secret_key": "secret_key",
        "password": "server_password",
        "session_max_age_days": "session_max_age_days",
        "default_search_years": "default_search_years",
        "contacts_scan_years": "contacts_scan_years",
        "data_dir": "data_dir",
        "max_attachment_bytes": "max_attachment_bytes",
        "update_check": "update_check",
    },
    "agent": {
        "tika_url": "tika_url",
        "poll_interval": "poll_interval",
        "reconcile_interval": "reconcile_interval",
        "batch_size": "batch_size",
        "store_raw_mime": "store_raw_mime",
        "content_window_months": "content_window_months",
        "account": "accounts",
    },
}


def env_only() -> bool:
    """True when MEERAIL_CONFIG is set but empty: read no files, only the environment.

    Both meerail.toml and .env are skipped. That is what the test suite runs
    with, so a developer's own configuration — a content window, a DATA_DIR
    pointing at a container path — cannot leak into a test run and decide what
    the assertions see.
    """
    explicit = os.environ.get("MEERAIL_CONFIG")
    return explicit is not None and not explicit.strip()


def config_file_path() -> Path | None:
    """The meerail.toml in force, or None when running on the environment alone."""
    explicit = os.environ.get("MEERAIL_CONFIG")
    if explicit is not None:
        if not explicit.strip():
            return None
        path = Path(explicit).expanduser()
        if not path.exists():
            raise SystemExit(f"MEERAIL_CONFIG points at a file that is not there: {path}")
        return path
    return DEFAULT_CONFIG_PATH if DEFAULT_CONFIG_PATH.exists() else None


def _read_config(path: Path) -> dict[str, Any]:
    if path.is_dir():
        # Docker creates a *directory* when a bind-mounted file is missing on the
        # host, so this is what a first `up` without the file looks like.
        raise SystemExit(
            f"{path} is a directory, not a file.\n"
            f"Docker creates one when a bind-mounted file is missing on the host. "
            f"Remove it, copy {EXAMPLE_CONFIG_PATH.name} to {path.name}, and bring "
            f"the stack back up."
        )
    with open(path, "rb") as fh:
        data = tomllib.load(fh)

    # An old agent/config.toml renamed rather than migrated: flat keys and
    # [[account]] at the top level instead of inside [agent].
    legacy = {"account", "store_raw_mime", "content_window_months", "poll_interval",
              "reconcile_interval", "batch_size", "database_url", "tika_url"}
    if legacy & set(data):
        raise SystemExit(
            f"{path} is in the old agent/config.toml format (top-level keys, "
            f"[[account]] rather than [[agent.account]]).\n"
            f"Convert it with:  python -m core.config migrate"
        )

    flat: dict[str, Any] = {}
    for section, values in data.items():
        if section not in _SECTION_KEYS:
            raise SystemExit(
                f"{path}: unknown section [{section}] — "
                f"expected one of {', '.join('[' + s + ']' for s in _SECTION_KEYS)}"
            )
        if not isinstance(values, dict):
            raise SystemExit(f"{path}: [{section}] must be a section, not a bare value")
        known = _SECTION_KEYS[section]
        for key, value in values.items():
            if key not in known:
                raise SystemExit(
                    f"{path}: unknown key {key!r} in [{section}] — "
                    f"valid keys are {', '.join(sorted(known))}"
                )
            flat[known[key]] = value
    return flat


class _MeerailTomlSource(PydanticBaseSettingsSource):
    """meerail.toml, flattened onto the field names."""

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        path = config_file_path()
        self._data = _read_config(path) if path else {}

    def get_field_value(self, field, field_name):  # pragma: no cover - unused
        # Required by the base class, but __call__ below supplies the whole dict
        # at once, which is what pydantic-settings actually merges.
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return self._data


class Settings(BaseSettings):
    """Every setting either process reads. See the module docstring for sources."""

    model_config = SettingsConfigDict(
        # Anchored to the repository rather than the working directory, so the
        # agent (which runs with its own directory as CWD) sees the same .env the
        # server does.
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- [database] -----------------------------------------------------------
    # The only channel between the agent and the web app.
    database_url: str = "postgresql+psycopg://meerail:meerail@localhost:5432/meerail"

    # --- [server] -------------------------------------------------------------
    # Secret used to encrypt server-side stored credentials and sign tokens.
    secret_key: str = "dev-insecure-secret-change-me"

    # Password gating the web UI / REST API. Empty = open — correct for a
    # localhost install; set it (with TLS in front) before exposing the server
    # to the internet. The browser asks once, then holds a signed session
    # cookie for session_max_age_days.
    server_password: str = ""

    # How long a browser login lasts before the password is asked again.
    session_max_age_days: int = 30

    # Default search window in years (0 = everything). The UI can override per query.
    default_search_years: int = 0

    # How many years back to scan from/to/cc/bcc addresses when building the
    # contacts autocomplete list (0 = all time).
    contacts_scan_years: int = 1

    # Scratch space. Raw MIME and attachments live in the database; this now only
    # holds files staged for outgoing (compose) messages.
    data_dir: Path = BASE_DIR / "data"

    # Per-attachment cap for outgoing (compose) uploads, in bytes.
    max_attachment_bytes: int = 100 * 1024 * 1024  # 100 MB

    # Once a day, ask github.com whether a newer meerail has been released, and
    # let the UI say so. This is the only outbound request the server makes;
    # false means it makes none. It sends nothing but the request itself — no
    # identifier, no mailbox statistics, no version — so the far end learns only
    # that some IP asked for a file. See app/updates.py.
    update_check: bool = True

    # --- [agent] --------------------------------------------------------------
    # Apache Tika endpoint for attachment text extraction.
    tika_url: str = "http://localhost:9998"

    # Seconds between IDLE cycles.
    poll_interval: int = 30

    # How often the sweep for flag changes and vanished mail runs. Much longer
    # than poll_interval on purpose: it is the expensive part of a pass, and new
    # mail does not wait on it.
    reconcile_interval: int = 900

    # UIDs per fetch/ingest batch; an [[agent.account]] may override it.
    batch_size: int = 200

    # Keep the original RFC822 bytes of every incoming message in
    # messages.raw_mime. Nothing in the app reads them today — they are kept so
    # future features (export, re-parse, signature verification) have the
    # original to work from — and they are the single largest thing in the
    # database, roughly doubling its size. Set false to ingest without them.
    #
    # Only affects messages ingested from then on: rows already stored keep
    # their bytes, and turning it back on does not backfill the gap.
    store_raw_mime: bool = True

    # Only keep the *content* of mail sent within this many months; 0 keeps
    # everything. Older messages are stored as headers alone — they still list,
    # thread and answer a search by subject or correspondent — and stored mail is
    # stripped back to headers as the window slides past it. The agent publishes
    # this number to the database each pass (core.ingest.record_content_window),
    # which is how the web app knows why a body is missing; the app does not read
    # this setting itself, and on a split deployment it does not have to.
    content_window_months: int = 0

    # One [[agent.account]] block per address. Empty is fine for a server-only
    # process; the agent refuses to start without at least one.
    accounts: list[AccountConfig] = []

    # Where the file was found, or None when running on the environment alone.
    # Set by get_settings(), not by any source — agent/preflight.py checks the
    # file's permissions, since it holds mailbox passwords in plaintext.
    config_path: Path | None = None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Highest priority first. The only addition to pydantic's own order is
        # the TOML file, slotted in below .env — so the environment overrides the
        # file, which is what the docstring promises.
        if env_only():
            return (init_settings, env_settings, file_secret_settings)
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _MeerailTomlSource(settings_cls),
            file_secret_settings,
        )

    @property
    def outbox_dir(self) -> Path:
        # Staging area for files attached to outgoing (compose) messages.
        return self.data_dir / "outbox"


@lru_cache
def get_settings() -> Settings:
    try:
        settings = Settings()
    except ValidationError as exc:
        # A pydantic traceback in front of someone who mistyped a port number is
        # noise. Name the file and the offending keys instead.
        where = config_file_path() or "the environment"
        problems = "\n".join(
            f"  {'.'.join(str(p) for p in e['loc']) or '?'}: {e['msg']}"
            for e in exc.errors()
        )
        raise SystemExit(f"Bad configuration in {where}:\n{problems}") from None
    # Not a source-provided value: this records where the file was actually read
    # from, which no environment variable should be able to misreport.
    settings.config_path = config_file_path()
    # data_dir is not created here. It is a server-only directory (compose sets
    # DATA_DIR on the `server` service alone), and creating it from the shared
    # loader meant the agent died on a path it never touches — a stale
    # DATA_DIR=/data in .env killed a native `agent/main.py` with a read-only
    # filesystem error before it read a single setting it cares about. The
    # server makes it at startup instead; see app/main.py.
    return settings


# --- migration ---------------------------------------------------------------
# `python -m core.config migrate` folds a pre-unification install (.env plus
# agent/config.toml) into one meerail.toml.

def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _coerce(key: str, raw: str) -> Any:
    """Turn a .env string into the type the TOML file should carry."""
    field = Settings.model_fields.get(key)
    annotation = field.annotation if field else str
    if annotation is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if annotation is int:
        try:
            return int(raw)
        except ValueError:
            return raw
    return raw


def _migrate() -> int:
    from dotenv import dotenv_values

    target = DEFAULT_CONFIG_PATH
    if target.exists():
        print(f"{target} already exists — nothing to do.")
        return 1

    env_path = BASE_DIR / ".env"
    env = {k: v for k, v in dotenv_values(env_path).items() if v is not None} if env_path.exists() else {}
    old: dict[str, Any] = {}
    if LEGACY_AGENT_CONFIG.exists():
        with open(LEGACY_AGENT_CONFIG, "rb") as fh:
            old = tomllib.load(fh)

    if not env and not old:
        print(f"Found neither {env_path} nor {LEGACY_AGENT_CONFIG} — "
              f"copy {EXAMPLE_CONFIG_PATH.name} to {target.name} and edit it instead.")
        return 1

    # agent/config.toml wins over .env, which is the precedence the old loader
    # applied to the two settings that lived in both. Migrating preserves the
    # values that install was actually running with.
    def pick(toml_key: str, env_key: str, field: str) -> Any:
        if toml_key in old:
            return old[toml_key]
        if env_key in env:
            return _coerce(field, env[env_key])
        return None

    sections: dict[str, dict[str, Any]] = {"database": {}, "server": {}, "agent": {}}
    plan = [
        ("database", "url", "database_url", "DATABASE_URL", "database_url"),
        ("server", "secret_key", None, "SECRET_KEY", "secret_key"),
        ("server", "password", None, "SERVER_PASSWORD", "server_password"),
        ("server", "session_max_age_days", None, "SESSION_MAX_AGE_DAYS", "session_max_age_days"),
        ("server", "default_search_years", None, "DEFAULT_SEARCH_YEARS", "default_search_years"),
        ("server", "contacts_scan_years", None, "CONTACTS_SCAN_YEARS", "contacts_scan_years"),
        ("server", "max_attachment_bytes", None, "MAX_ATTACHMENT_BYTES", "max_attachment_bytes"),
        ("agent", "tika_url", "tika_url", "TIKA_URL", "tika_url"),
        ("agent", "poll_interval", "poll_interval", None, "poll_interval"),
        ("agent", "reconcile_interval", "reconcile_interval", None, "reconcile_interval"),
        ("agent", "batch_size", "batch_size", None, "batch_size"),
        ("agent", "store_raw_mime", "store_raw_mime", "STORE_RAW_MIME", "store_raw_mime"),
        ("agent", "content_window_months", "content_window_months", "CONTENT_WINDOW_MONTHS",
         "content_window_months"),
    ]
    for section, key, toml_key, env_key, field in plan:
        value = pick(toml_key or "\0", env_key or "\0", field)
        if value is not None:
            sections[section][key] = value

    # DATA_DIR is deliberately not carried over: it is /data in every container
    # and the default beside the repository otherwise, and pinning a host path in
    # a file that now also gets mounted into containers is how it goes wrong.

    lines = [
        "# meerail configuration — one file for the server and the agent.",
        "#",
        "# Written by `python -m core.config migrate`. Every setting here can be",
        "# overridden by an environment variable of the same name in upper case",
        "# (server.password -> SERVER_PASSWORD, database.url -> DATABASE_URL).",
        "#",
        "# Holds mailbox passwords in plaintext — keep it mode 0600.",
        "",
    ]
    for section in ("database", "server", "agent"):
        if not sections[section]:
            continue
        lines.append(f"[{section}]")
        for key, value in sections[section].items():
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")

    for account in old.get("account", []):
        lines.append("[[agent.account]]")
        for key, value in account.items():
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")

    target.write_text("\n".join(lines))
    target.chmod(0o600)

    print(f"Wrote {target} (mode 0600) from:")
    if old:
        print(f"  {LEGACY_AGENT_CONFIG}  — {len(old.get('account', []))} account(s)")
    if env:
        print(f"  {env_path}")
    steps = [f"Read {target} and check it over."]
    if old:
        steps.append(f"Delete {LEGACY_AGENT_CONFIG} — it is no longer read.")
    steps.append("Trim .env to the POSTGRES_* credentials docker compose needs; "
                 "anything left in it still works, as an override of the file.")
    print()
    print("Next:")
    for i, step in enumerate(steps, 1):
        print(f"  {i}. {step}")
    return 0


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 2 and sys.argv[1] == "migrate":
        raise SystemExit(_migrate())
    print("usage: python -m core.config migrate", file=sys.stderr)
    raise SystemExit(2)
