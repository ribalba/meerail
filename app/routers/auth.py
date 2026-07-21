"""Browser session bootstrap for installations protected by SERVER_AUTH_TOKEN."""

import secrets

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel

from ..config import get_settings
from ..deps import UI_SESSION_COOKIE, ui_session_value

router = APIRouter(prefix="/api/auth", tags=["auth"])
settings = get_settings()


class LoginRequest(BaseModel):
    token: str


@router.get("/status")
def auth_status() -> dict:
    return {"required": bool(settings.server_auth_token)}


@router.post("/login", status_code=204)
def login(payload: LoginRequest, request: Request, response: Response) -> None:
    expected = settings.server_auth_token
    if not expected or not secrets.compare_digest(payload.token, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid server token")
    response.set_cookie(
        UI_SESSION_COOKIE, ui_session_value(), httponly=True, samesite="strict",
        secure=request.url.scheme == "https", path="/",
    )
