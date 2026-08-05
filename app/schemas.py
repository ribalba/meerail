from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


# --- Accounts --------------------------------------------------------------


class AccountUpdate(BaseModel):
    label: str | None = None
    color: str | None = None
    active: bool | None = None
    footer: str | None = None


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    label: str
    color: str
    active: bool
    backfill_complete: bool
    send_addresses: list[str] = Field(default_factory=list)
    # Address (lower-cased) -> display name sent on From. Absent means the
    # address goes out bare; see Account.send_names.
    send_names: dict[str, str] = Field(default_factory=dict)
    footer: str = ""
    # Which of label/color/footer are pinned in the agent's meerail.toml. The
    # UI shows those as set-in-the-config instead of editable, and PATCH refuses
    # them; see Account.config_fields.
    config_fields: list[str] = Field(default_factory=list)
    last_agent_seen: datetime | None = None
    last_sync_at: datetime | None = None
    created_at: datetime


class MailboxOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    account_id: int
    imap_name: str
    display_name: str
    role: str
    unread_count: int
    total_count: int
    sort_order: int
