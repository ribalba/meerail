from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session as DBSession

from core.database import get_db
from ..deps import require_ui_auth
from core.models import Account
from ..schemas import AccountCreate, AccountOut, AccountUpdate

router = APIRouter(prefix="/api/accounts", tags=["accounts"], dependencies=[Depends(require_ui_auth)])


@router.get("", response_model=list[AccountOut])
def list_accounts(db: DBSession = Depends(get_db)):
    return db.query(Account).order_by(Account.created_at).all()


@router.post("", response_model=AccountOut, status_code=201)
def create_account(payload: AccountCreate, db: DBSession = Depends(get_db)):
    email = payload.email.strip().lower()
    if db.query(Account).filter(Account.email == email).first():
        raise HTTPException(status_code=409, detail="An account with that email already exists")
    account = Account(
        email=email,
        label=payload.label.strip() or email.split("@")[0],
        color=payload.color,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


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
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(account, field, value)
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
