"""Async client for the Mango Power cloud API (unofficial)."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from .const import HEADERS, SUPABASE_KEY, SUPABASE_URL

_LOGGER = logging.getLogger(__name__)
TIMEOUT = aiohttp.ClientTimeout(total=20)


class MangoError(Exception):
    """Generic API error."""


class MangoAuthError(MangoError):
    """Login is invalid or expired and cannot be refreshed."""


def user_id_from_token(token: str) -> str:
    """The app sends the Supabase user id (JWT 'sub') as user_id."""
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))["sub"]


class MangoAuth:
    """Supabase email-code / password login used by the official app."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def _post(self, path: str, body: dict) -> dict:
        async with self._session.post(
            f"{SUPABASE_URL}/auth/v1/{path}",
            json=body,
            headers={**HEADERS, "apikey": SUPABASE_KEY},
            timeout=TIMEOUT,
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                msg = (data or {}).get("msg") or (data or {}).get("error_description") or resp.status
                raise MangoAuthError(str(msg))
            return data

    async def send_code(self, email: str) -> None:
        await self._post("otp", {"email": email, "create_user": False})

    async def verify_code(self, email: str, code: str) -> dict:
        return await self._post("verify", {"type": "email", "email": email, "token": code})

    async def password(self, email: str, password: str) -> dict:
        return await self._post("token?grant_type=password", {"email": email, "password": password})

    async def refresh(self, refresh_token: str) -> dict:
        return await self._post("token?grant_type=refresh_token", {"refresh_token": refresh_token})


class MangoApi:
    """Authenticated calls to /v4/mp-app with automatic token refresh."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        tokens: dict[str, Any],
        on_tokens: Callable[[dict[str, Any]], Awaitable[None] | None],
    ) -> None:
        self._session = session
        self._base = f"{base_url}/v4/mp-app"
        self._tokens = tokens  # access_token, refresh_token, expires_at
        self._on_tokens = on_tokens
        self._auth = MangoAuth(session)
        self._lock = asyncio.Lock()  # refresh tokens are single-use

    @property
    def user_id(self) -> str:
        return user_id_from_token(self._tokens["access_token"])

    async def _token(self) -> str:
        async with self._lock:
            if self._tokens.get("expires_at", 0) > time.time() + 60:
                return self._tokens["access_token"]
            try:
                sess = await self._auth.refresh(self._tokens["refresh_token"])
            except MangoAuthError as err:
                raise MangoAuthError(f"token refresh failed: {err}") from err
            self._tokens = {
                "access_token": sess["access_token"],
                "refresh_token": sess["refresh_token"],
                "expires_at": sess.get("expires_at") or time.time() + sess.get("expires_in", 3600),
            }
            res = self._on_tokens(self._tokens)
            if asyncio.iscoroutine(res):
                await res
            return self._tokens["access_token"]

    async def call(self, path: str, body: dict) -> dict:
        """POST and return 'result'/'data'. Raises MangoError on API error codes."""
        token = await self._token()
        try:
            async with self._session.post(
                self._base + path,
                json=body,
                headers={**HEADERS, "Authorization": f"Bearer {token}"},
                timeout=TIMEOUT,
            ) as resp:
                text = await resp.text()
                if resp.status == 401:
                    raise MangoAuthError("unauthorized")
                if resp.status >= 400:
                    raise MangoError(f"HTTP {resp.status}: {text[:200]}")
                data = json.loads(text)
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as err:
            raise MangoError(f"{path}: {err}") from err
        if data.get("code") not in (0, None):
            raise MangoError(f"{path}: {data.get('code')} {data.get('message')}")
        return data.get("result") or data.get("data") or {}

    # --- endpoints used by the app for the Mango Power E ---

    async def list_devices(self) -> list[dict]:
        """Devices are listed inside the user's sites."""
        res = await self.call("/site/list", {"user_id": self.user_id, "page_size": 1000})
        found: dict[str, dict] = {}

        def walk(x: Any) -> None:
            if isinstance(x, dict):
                if x.get("sn") and x.get("id"):
                    found.setdefault(str(x["id"]), x)
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)

        walk(res)
        return list(found.values())

    async def device_info(self, device_id: str) -> dict:
        return await self.call("/device/info", {"device_id": device_id})

    async def realtime(self, device_id: str, tz: str) -> dict:
        return await self.call("/device/data/realtime", {"device_id": device_id, "timezone": tz})

    async def settings(self, device_id: str, tz: str) -> dict:
        return await self.call("/device/data/setting", {"device_id": device_id, "timezone": tz})

    async def send_cmd(self, device_id: str, setting_type: str, setting_data: dict) -> dict:
        return await self.call(
            "/device/send-cmd",
            {"device_id": device_id, "setting_type": setting_type, "setting_data": setting_data},
        )
