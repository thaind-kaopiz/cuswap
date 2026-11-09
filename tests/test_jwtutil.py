from datetime import datetime, timedelta, timezone

from cuswap.jwtutil import decode_payload, is_expired, session_cookie, subject


def _token(payload: dict) -> str:
    import base64
    import json

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    header = b64(b'{"alg":"none","typ":"JWT"}')
    body = b64(json.dumps(payload, separators=(",", ":")).encode())
    return f"{header}.{body}.sig"


def test_subject_and_cookie():
    token = _token({"sub": "auth0|user_01ABC", "type": "session"})
    assert subject(token) == "auth0|user_01ABC"
    assert session_cookie(token) == f"auth0|user_01ABC::{token}"
    assert decode_payload(token)["type"] == "session"


def test_expiry():
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    assert is_expired(_token({"sub": "x", "exp": int(future.timestamp())})) is False
    assert is_expired(_token({"sub": "x", "exp": int(past.timestamp())})) is True
