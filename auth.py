"""Supabase password sign-in and in-memory, opaque browser sessions."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from uuid import UUID

import httpx

import config

COOKIE = "zhixu_session"
SESSION_SECONDS = 12 * 60 * 60
_AUTH_ENV = (
    "SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY", "SUPABASE_ANON_KEY",
    "ZHIXU_AUTH_USER_IDS", "ZHIXU_MAIN_USER_ID",
)


class AuthError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def not_configured() -> AuthError:
    return AuthError(503, "auth_not_configured", "Supabase 登录尚未配置完成。")


def _unavailable() -> AuthError:
    return AuthError(503, "auth_unavailable", "暂时无法连接 Supabase 登录服务，请稍后重试。")


def _valid_url(value: str) -> bool:
    try:
        url = urlsplit(value)
        return bool(
            url.hostname and url.username is None and not url.query and not url.fragment
            and url.path in ("", "/")
            and (url.scheme == "https" or (
                url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
            ))
            and (url.port is None or url.port > 0)
        )
    except ValueError:
        return False


def _public_key(value: str) -> bool:
    if value.startswith("sb_publishable_"):
        return len(value) > len("sb_publishable_")
    try:
        _, payload, _ = value.split(".")
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return isinstance(claims, dict) and claims.get("role") == "anon"
    except (ValueError, UnicodeError):
        return False


@dataclass(frozen=True)
class User:
    id: str
    email: str
    is_primary: bool


@dataclass
class Session:
    user: User
    refresh_token: str
    refresh_at: float
    ends_at: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Auth:
    def __init__(self):
        self.required = (
            os.environ.get("ZHIXU_AUTH_REQUIRED") == "1"
            or any(key in os.environ for key in _AUTH_ENV)
            or config.legacy_backup_dir().exists()
        )
        self.url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
        self.key = (os.environ.get("SUPABASE_PUBLISHABLE_KEY", "").strip()
                    or os.environ.get("SUPABASE_ANON_KEY", "").strip())
        self.user_ids: set[str] = set()
        self.primary = ""
        try:
            ids = os.environ.get("ZHIXU_AUTH_USER_IDS", "").split(",")
            self.user_ids = {str(UUID(id_.strip())) for id_ in ids}
            self.primary = str(UUID(os.environ.get("ZHIXU_MAIN_USER_ID", "").strip()))
        except ValueError:
            pass
        self.configured = (
            len(self.user_ids) == 2 and len(ids) == 2 and self.primary in self.user_ids
            and _valid_url(self.url) and _public_key(self.key)
        )
        self.sessions: dict[str, Session] = {}

    def status(self, user: User | None = None) -> dict:
        status = {
            "mode": "supabase" if self.required else "demo",
            "configured": self.configured,
            "authenticated": user is not None,
        }
        if user is not None:
            status["user"] = {"id": user.id, "email": user.email, "is_primary": user.is_primary}
        return status

    async def _exchange(self, grant: str, payload: dict) -> tuple[User, str, float]:
        try:
            async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
                response = await client.post(
                    self.url + "/auth/v1/token", params={"grant_type": grant},
                    headers={"apikey": self.key}, json=payload,
                )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise _unavailable() from exc
        if response.status_code == 429:
            raise AuthError(429, "auth_rate_limited", "尝试次数过多，请稍后重试。")
        if response.status_code in (400, 401, 403, 422):
            raise AuthError(401, "invalid_credentials", "邮箱或密码有误，或账号暂不可用。")
        if response.status_code != 200:
            raise _unavailable()
        try:
            data = response.json()
            user = data["user"]
            user_id = str(UUID(user["id"]))
            email = user["email"]
            refresh_token = data["refresh_token"]
            expires_in = int(data["expires_in"])
            if (not isinstance(email, str) or not email or not refresh_token
                    or not isinstance(refresh_token, str) or not data["access_token"] or expires_in <= 0):
                raise ValueError("Incomplete auth token response")
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
            raise _unavailable() from exc
        if user_id not in self.user_ids or user.get("is_anonymous"):
            raise AuthError(403, "account_forbidden", "该账号未获准使用知序。")
        refresh_at = time.monotonic() + expires_in - min(30, expires_in)
        return User(user_id, email, user_id == self.primary), refresh_token, refresh_at

    async def sign_in(self, email: str, password: str, previous: str | None) -> tuple[str, User]:
        if not self.configured:
            raise not_configured()
        user, refresh_token, refresh_at = await self._exchange(
            "password", {"email": email, "password": password},
        )
        now = time.monotonic()
        for token, session in list(self.sessions.items()):
            if session.ends_at <= now:
                self.sessions.pop(token, None)
        self.sign_out(previous)
        token = secrets.token_urlsafe(32)
        self.sessions[token] = Session(user, refresh_token, refresh_at, now + SESSION_SECONDS)
        return token, user

    def sign_out(self, token: str | None) -> None:
        if token:
            self.sessions.pop(token, None)

    async def user_for(self, token: str | None) -> User | None:
        session = self.sessions.get(token) if token else None
        if session is None:
            return None
        async with session.lock:
            if self.sessions.get(token) is not session or session.ends_at <= time.monotonic():
                self.sign_out(token)
                return None
            if session.refresh_at <= time.monotonic():
                try:
                    user, refresh_token, refresh_at = await self._exchange(
                        "refresh_token", {"refresh_token": session.refresh_token},
                    )
                except AuthError as exc:
                    if exc.status in (401, 403):
                        self.sign_out(token)
                        return None
                    raise
                if (user.id != session.user.id or self.sessions.get(token) is not session
                        or session.ends_at <= time.monotonic()):
                    self.sign_out(token)
                    return None
                session.user = user
                session.refresh_token = refresh_token
                session.refresh_at = refresh_at
            return session.user
