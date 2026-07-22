"""Account read/edit endpoints.

There is deliberately no create endpoint: accounts are provisioned by the agent,
which inserts the row on its first sync pass (`core.ingest.get_or_create_account`)
keyed on the email in its `config.toml`. What the UI owns is presentation —
`label`, `color` and `footer` — which is what PATCH exposes.
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
