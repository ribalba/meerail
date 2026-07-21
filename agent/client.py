"""HTTP client for the meerail server's /api/agent/* protocol."""

from __future__ import annotations

import httpx


class ServerClient:
    def __init__(self, base_url: str, token: str = "", timeout: float = 180.0):
        self.base = base_url.rstrip("/")
        headers = {"X-Agent-Token": token} if token else {}
        self.http = httpx.Client(base_url=self.base, headers=headers, timeout=timeout)

    def _post(self, path: str, payload: dict):
        r = self.http.post(path, json=payload)
        r.raise_for_status()
        return r.json()

    def register_folders(self, email: str, folders: list[dict]) -> list[dict]:
        return self._post("/api/agent/folders", {"account": email, "folders": folders})

    def scan(self, email: str, folder: str, uidvalidity: int | None, items: list[dict]) -> dict:
        return self._post("/api/agent/scan",
                          {"account": email, "folder": folder, "uidvalidity": uidvalidity, "items": items})

    def upload_messages(self, email: str, folder: str, uidvalidity: int | None, items: list[dict]) -> dict:
        return self._post("/api/agent/messages",
                          {"account": email, "folder": folder, "uidvalidity": uidvalidity, "items": items})

    def advance_cursor(self, email: str, folder: str, last_uid: int) -> dict:
        return self._post("/api/agent/cursor", {"account": email, "folder": folder, "last_uid": last_uid})

    def update_flags(self, email: str, folder: str, items: list[dict]) -> dict:
        return self._post("/api/agent/flags", {"account": email, "folder": folder, "items": items})

    def present(self, email: str, folder: str, uidvalidity: int | None, uids: list[int]) -> dict:
        return self._post("/api/agent/present",
                          {"account": email, "folder": folder, "uidvalidity": uidvalidity, "uids": uids})

    def heartbeat(self, email: str, backfill_complete: bool | None = None) -> dict:
        return self._post("/api/agent/heartbeat",
                          {"account": email, "backfill_complete": backfill_complete})

    def get_actions(self, email: str) -> list[dict]:
        r = self.http.get("/api/agent/actions", params={"account": email})
        r.raise_for_status()
        return r.json()

    def get_outbound(self, outbound_id: int) -> dict:
        r = self.http.get(f"/api/agent/outbound/{outbound_id}")
        r.raise_for_status()
        return r.json()

    def ack_action(self, action_id: int, ok: bool, error: str | None = None) -> dict:
        return self._post(f"/api/agent/actions/{action_id}/ack", {"ok": ok, "error": error})
