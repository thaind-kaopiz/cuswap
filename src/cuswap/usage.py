from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from cuswap.jwtutil import session_cookie
from cuswap.models import UsageSnapshot, isoformat, utcnow

API_ORIGIN = "https://cursor.com"
OAUTH_TOKEN_URL = "https://api2.cursor.sh/oauth/token"
CURSOR_OAUTH_CLIENT_ID = "KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB"
USER_AGENT = "cuswap/0.1"


@dataclass
class RefreshResult:
    access_token: str | None = None
    refresh_token: str | None = None
    should_logout: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.access_token) and not self.should_logout


class UsageError(RuntimeError):
    pass


def _headers(access_token: str) -> dict[str, str]:
    return {
        "Cookie": f"WorkosCursorSessionToken={session_cookie(access_token)}",
        "Origin": API_ORIGIN,
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }


def fetch_me(access_token: str, *, timeout: float = 20.0) -> dict[str, Any]:
    try:
        response = httpx.get(
            f"{API_ORIGIN}/api/auth/me",
            headers=_headers(access_token),
            timeout=timeout,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise UsageError(f"auth/me request failed: {exc}") from exc
    if response.status_code == 401:
        raise UsageError("token_expired")
    if response.status_code >= 400:
        raise UsageError(f"auth/me HTTP {response.status_code}")
    data = response.json()
    if not isinstance(data, dict):
        raise UsageError("auth/me returned unexpected payload")
    return data


def fetch_usage_summary(access_token: str, *, timeout: float = 20.0) -> dict[str, Any]:
    try:
        response = httpx.get(
            f"{API_ORIGIN}/api/usage-summary",
            headers=_headers(access_token),
            timeout=timeout,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise UsageError(f"usage-summary request failed: {exc}") from exc
    if response.status_code == 401:
        raise UsageError("token_expired")
    if response.status_code >= 400:
        raise UsageError(f"usage-summary HTTP {response.status_code}")
    data = response.json()
    if not isinstance(data, dict):
        raise UsageError("usage-summary returned unexpected payload")
    return data


def parse_usage(summary: dict[str, Any]) -> UsageSnapshot:
    plan = ((summary.get("individualUsage") or {}).get("plan") or {})
    on_demand = ((summary.get("individualUsage") or {}).get("onDemand") or {})
    used = _num(plan.get("used"))
    limit = _num(plan.get("limit"))
    remaining = _num(plan.get("remaining"))
    auto_percent = _num(plan.get("autoPercentUsed"))
    api_percent = _num(plan.get("apiPercentUsed"))
    total_percent = _num(plan.get("totalPercentUsed"))
    # Match Cursor Plan & Usage: Cursor models = auto, Other models = api.
    percent = auto_percent if auto_percent is not None else total_percent
    return UsageSnapshot(
        membership_type=summary.get("membershipType"),
        is_unlimited=bool(summary.get("isUnlimited")),
        used=used,
        limit=limit,
        remaining=remaining,
        percent=percent,
        auto_percent=auto_percent,
        api_percent=api_percent,
        on_demand_enabled=bool(on_demand.get("enabled")),
        on_demand_used=_num(on_demand.get("used")),
        billing_cycle_start=summary.get("billingCycleStart"),
        billing_cycle_end=summary.get("billingCycleEnd"),
        fetched_at=isoformat(utcnow()) or "",
    )


def snapshot_for_token(access_token: str) -> UsageSnapshot:
    try:
        return parse_usage(fetch_usage_summary(access_token))
    except UsageError as exc:
        return UsageSnapshot(fetched_at=isoformat(utcnow()) or "", error=str(exc))


def parse_refresh_response(data: dict[str, Any]) -> RefreshResult:
    access = data.get("access_token") or data.get("accessToken") or ""
    refresh = data.get("refresh_token") or data.get("refreshToken") or ""
    should_logout = bool(data.get("shouldLogout"))
    if should_logout or not access:
        return RefreshResult(should_logout=True, error="needs_reauth")
    return RefreshResult(
        access_token=str(access),
        refresh_token=str(refresh) if refresh else None,
    )


def refresh_session(refresh_token: str, *, timeout: float = 20.0) -> RefreshResult:
    try:
        response = httpx.post(
            OAUTH_TOKEN_URL,
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
            json={
                "grant_type": "refresh_token",
                "client_id": CURSOR_OAUTH_CLIENT_ID,
                "refresh_token": refresh_token,
            },
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        return RefreshResult(error=f"refresh failed: {exc}")
    try:
        data = response.json()
    except ValueError:
        return RefreshResult(error=f"refresh HTTP {response.status_code}")
    if not isinstance(data, dict):
        return RefreshResult(error="refresh returned unexpected payload")
    if response.status_code >= 400 and "access_token" not in data and "accessToken" not in data:
        return RefreshResult(error=f"refresh HTTP {response.status_code}")
    return parse_refresh_response(data)


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
