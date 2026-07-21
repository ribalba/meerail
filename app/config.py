from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Server configuration, overridable via environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database (PostgreSQL). docker-compose points this at the bundled "db" service.
    database_url: str = "postgresql+psycopg2://meerail:meerail@localhost:5432/meerail"

    # Secret used to encrypt server-side stored credentials and sign tokens.
    secret_key: str = "dev-insecure-secret-change-me"

    # Where raw .eml files and attachment blobs are written.
    data_dir: Path = BASE_DIR / "data"

    # Apache Tika endpoint for attachment text extraction.
    tika_url: str = "http://localhost:9998"

    # Shared secret the agent must present on /api/agent/*. Empty disables the check.
    agent_token: str = ""

    # Optional shared secret to gate the web UI / REST API. Empty = open (localhost).
    server_auth_token: str = ""

    # Default search window in years (0 = everything). The UI can override per query.
    default_search_years: int = 0

    # How many years back to scan from/to/cc/bcc addresses when building the
    # contacts autocomplete list (0 = all time).
    contacts_scan_years: int = 1

    # Per-attachment upload cap the agent may send (bytes).
    max_attachment_bytes: int = 100 * 1024 * 1024  # 100 MB

    @property
    def eml_dir(self) -> Path:
        return self.data_dir / "eml"

    @property
    def attachments_dir(self) -> Path:
        return self.data_dir / "attachments"

    @property
    def outbox_dir(self) -> Path:
        # Staging area for files attached to outgoing (compose) messages.
        return self.data_dir / "outbox"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    for d in (settings.data_dir, settings.eml_dir, settings.attachments_dir, settings.outbox_dir):
        d.mkdir(parents=True, exist_ok=True)
    return settings
