from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


# --- Agent protocol --------------------------------------------------------


class Flags(BaseModel):
    seen: bool = False
    flagged: bool = False
    answered: bool = False
    draft: bool = False
    deleted: bool = False
    keywords: list[str] = Field(default_factory=list)


class FolderInfo(BaseModel):
    imap_name: str
    role_hint: str = ""           # e.g. "\\Sent" special-use flag if IMAP reported one
    uidvalidity: int | None = None
    uidnext: int | None = None


class FolderRegister(BaseModel):
    account: EmailStr
    folders: list[FolderInfo]


class FolderCursor(BaseModel):
    id: int
    imap_name: str
    role: str
    uidvalidity: int | None
    last_uid: int


class ScanItem(BaseModel):
    uid: int
    message_id: str | None = None
    flags: Flags = Field(default_factory=Flags)


class ScanRequest(BaseModel):
    account: EmailStr
    folder: str                    # imap_name
    uidvalidity: int | None = None
    items: list[ScanItem]


class ScanResponse(BaseModel):
    matched: int
    need_raw: list[int]


class RawItem(BaseModel):
    uid: int
    flags: Flags = Field(default_factory=Flags)
    raw_b64: str


class MessagesRequest(BaseModel):
    account: EmailStr
    folder: str
    uidvalidity: int | None = None
    items: list[RawItem]


class MessagesResponse(BaseModel):
    stored: int
    created: int


class FlagItem(BaseModel):
    uid: int
    flags: Flags


class FlagsRequest(BaseModel):
    account: EmailStr
    folder: str
    items: list[FlagItem]


class PresentRequest(BaseModel):
    account: EmailStr
    folder: str
    uidvalidity: int | None = None
    uids: list[int]


class CursorRequest(BaseModel):
    account: EmailStr
    folder: str
    last_uid: int


class HeartbeatRequest(BaseModel):
    account: EmailStr
    backfill_complete: bool | None = None


class ActionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    type: str
    payload: dict
    message_pk: int | None = None


class ActionAck(BaseModel):
    ok: bool = True
    error: str | None = None


# --- Accounts --------------------------------------------------------------


class AccountCreate(BaseModel):
    email: EmailStr
    label: str = ""
    color: str = "#1d6ff2"


class AccountUpdate(BaseModel):
    label: str | None = None
    color: str | None = None
    active: bool | None = None


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    label: str
    color: str
    active: bool
    backfill_complete: bool
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
