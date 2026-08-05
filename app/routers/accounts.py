"""Account read/edit endpoints.

There is deliberately no create endpoint: accounts are provisioned by the agent,
which inserts the row on its first sync pass (`core.ingest.get_or_create_account`)
keyed on the email in its `config.toml`. What the UI owns is presentation —
`label`, `color` and `footer` — which is what PATCH exposes.

Any of those three may instead be pinned in the agent's meerail.toml, which
takes ownership away from here: the agent rewrites the value on every pass and
lists the field in `config_fields`, and PATCH refuses it rather than accepting a
change the next sync would silently undo.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session as DBSession

from core.database import get_db
from ..deps import require_ui_auth
from core.models import Account
from ..schemas import AccountOut, AccountUpdate

router = APIRouter(prefix="/api/accounts", tags=["accounts"], dependencies=[Depends(require_ui_auth)])


@router.get("", response_model=list[AccountOut])
def list_accounts(db: DBSession = Depends(get_db)):
    return db.query(Account).order_by(Account.created_at).all()


@router.get("/{account_id}", response_model=AccountOut)
def get_account(account_id: int, db: DBSession = Depends(get_db)):
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


@router.patch("/{account_id}", response_model=AccountOut)
def update_account(account_id: int, payload: AccountUpdate, db: DBSession = Depends(get_db)):
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    fields = payload.model_dump(exclude_unset=True)
    # Refuse rather than accept-and-lose: the agent rewrites these on its next
    # pass, so a silent 200 here would show the new value until the sync that
    # replaced it, and leave the user to guess why it went back.
    pinned = [f for f in fields if f in (account.config_fields or [])]
    if pinned:
        raise HTTPException(
            status_code=409,
            detail=f"{', '.join(pinned)} {'is' if len(pinned) == 1 else 'are'} set for this "
                   f"account in meerail.toml — change it there, or remove it from the file "
                   f"to edit it here.",
        )
    for field, value in fields.items():
        setattr(account, field, value)
    # Saving a footer — including clearing it — opts the account out of the
    # default-footer backfill for good.
    if "footer" in fields:
        account.footer_customized = True
    db.commit()
    db.refresh(account)
    return account


@router.delete("/{account_id}")
def delete_account(account_id: int, db: DBSession = Depends(get_db)):
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    db.delete(account)
    db.commit()
    return {"ok": True}
