from __future__ import annotations

import base64
import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from cuswap.usage import USER_AGENT

LOGIN_URL = "https://cursor.com/loginDeepControl"
POLL_URL = "https://api2.cursor.sh/auth/poll"


class OAuthError(RuntimeError):
    pass


@dataclass
class OAuthSession:
    verifier: str
    challenge: str
    uuid: str
    login_url: str


@dataclass
class OAuthTokens:
    access_token: str
    refresh_token: str


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def start_session() -> OAuthSession:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("utf-8")).digest())
    login_id = str(uuid.uuid4())
    query = urlencode(
        {
            "challenge": challenge,
            "uuid": login_id,
            "mode": "login",
            "redirectTarget": "cli",
        }
    )
    return OAuthSession(
        verifier=verifier,
        challenge=challenge,
        uuid=login_id,
        login_url=f"{LOGIN_URL}?{query}",
    )


def poll_tokens(
    session: OAuthSession,
    *,
    timeout: float = 240.0,
    on_wait=None,
) -> OAuthTokens:
    deadline = time.time() + timeout
    delay = 1.0
    errors = 0
    while time.time() < deadline:
        time.sleep(delay)
        if on_wait:
            on_wait()
        try:
            response = httpx.get(
                POLL_URL,
                params={"uuid": session.uuid, "verifier": session.verifier},
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=20.0,
            )
        except httpx.HTTPError:
            errors += 1
            if errors >= 8:
                raise OAuthError("Network error while waiting for browser login") from None
            delay = min(delay * 1.2, 8.0)
            continue
        if response.status_code == 404:
            errors = 0
            delay = min(delay * 1.2, 8.0)
            continue
        if response.status_code >= 400:
            errors += 1
            if errors >= 5:
                raise OAuthError(f"Login poll failed (HTTP {response.status_code})")
            continue
        data = response.json()
        access = (data or {}).get("accessToken") or (data or {}).get("access_token")
        refresh = (data or {}).get("refreshToken") or (data or {}).get("refresh_token") or ""
        if not access:
            raise OAuthError("Login finished but no access token was returned")
        return OAuthTokens(access_token=str(access), refresh_token=str(refresh))
    raise OAuthError("Timed out waiting for browser login")
