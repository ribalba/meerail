from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


# --- Accounts --------------------------------------------------------------


class AccountCreate(BaseModel):
    email: EmailStr
    label: str = ""
    color: str = "#1d6ff2"


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
    footer: str = ""
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
