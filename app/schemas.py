from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


# --- Accounts --------------------------------------------------------------


class AccountUpdate(BaseModel):
    """A PATCH: every field optional, and none of them nullable.

    The two are not the same thing, and the type says the first. Omitting
    ``label`` means "leave it alone" — the router works from
    ``model_dump(exclude_unset=True)``, so an absent field is never written. But
    ``{"label": null}`` is a value, and it was assigned straight onto a column
    the database declares NOT NULL: the request died in the flush with a 500 and
    an integrity error, which tells whoever sent it nothing about what to send
    instead.

    So null is refused where the request is read, and the reason travels with the
    422. None stays as the *default* — that is what "unset" is spelled as in
    Python, and it never reaches a column, because exclude_unset drops it.
    """

    label: str | None = None
    color: str | None = None
    active: bool | None = None
    footer: str | None = None

    @field_validator("label", "color", "active", "footer", mode="before")
    @classmethod
    def _not_null(cls, value, info):
        # Defaults are not validated in pydantic v2, so this only ever sees a
        # value the caller actually sent.
        if value is None:
            raise ValueError(f"{info.field_name} cannot be null — omit it to leave it "
                             f"unchanged, or send a value")
        return value


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
