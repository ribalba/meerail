from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Shared configuration for both the web app and the agent.

    Overridable via environment variables or a .env file.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database (PostgreSQL) — the only channel between the agent and the web app.
    database_url: str = "postgresql+psycopg://meerail:meerail@localhost:5432/meerail"

    # Secret used to encrypt server-side stored credentials and sign tokens.
    secret_key: str = "dev-insecure-secret-change-me"

    # Scratch space. Raw MIME and attachments live in the database; this now only
    # holds files staged for outgoing (compose) messages.
    data_dir: Path = BASE_DIR / "data"

    # Apache Tika endpoint for attachment text extraction (used by the agent).
    tika_url: str = "http://localhost:9998"

    # Keep the original RFC822 bytes of every incoming message in
    # messages.raw_mime. Nothing in the app reads them today — they are kept so
    # future features (export, re-parse, signature verification) have the
    # original to work from — and they are the single largest thing in the
    # database, roughly doubling its size. Set false to ingest without them.
    #
    # Only affects messages ingested from then on: rows already stored keep
    # their bytes, and turning it back on does not backfill the gap.
    store_raw_mime: bool = True

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

    # Per-attachment cap for outgoing (compose) uploads, in bytes.
    max_attachment_bytes: int = 100 * 1024 * 1024  # 100 MB

    @property
    def outbox_dir(self) -> Path:
        # Staging area for files attached to outgoing (compose) messages.
        return self.data_dir / "outbox"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    for d in (settings.data_dir, settings.outbox_dir):
        d.mkdir(parents=True, exist_ok=True)
    return settings
