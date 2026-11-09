from __future__ import annotations

import base64
import json
from datetime import datetime, timezone


class JwtError(ValueError):
    pass


def _b64url_decode(raw: str) -> bytes:
    padded = raw + "=" * (-len(raw) % 4)
    try:
        return base64.urlsafe_b64decode(padded)
    except Exception as exc:  # noqa: BLE001
        raise JwtError("invalid base64url") from exc


def decode_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) < 2:
        raise JwtError("not a JWT")
    try:
        payload = json.loads(_b64url_decode(parts[1]))
    except json.JSONDecodeError as exc:
        raise JwtError("JWT payload is not JSON") from exc
    if not isinstance(payload, dict):
        raise JwtError("JWT payload is not an object")
    return payload


def subject(token: str) -> str:
    sub = decode_payload(token).get("sub")
    if not sub:
        raise JwtError("JWT has no sub claim")
    return str(sub)


def expires_at(token: str) -> datetime | None:
    exp = decode_payload(token).get("exp")
    if exp is None:
        return None
    try:
        return datetime.fromtimestamp(int(exp), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def is_expired(token: str, *, skew_seconds: int = 30) -> bool:
    exp = expires_at(token)
    if exp is None:
        return False
    now = datetime.now(timezone.utc).timestamp()
    return now >= (exp.timestamp() - skew_seconds)


def session_cookie(access_token: str) -> str:
    return f"{subject(access_token)}::{access_token}"
