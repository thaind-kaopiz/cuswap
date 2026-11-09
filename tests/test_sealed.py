import sqlite3
from datetime import datetime, timedelta, timezone

from cuswap.cursor_db import read_live_bundle
from cuswap.models import EncodedKey, SessionBundle
from cuswap.sealed import SEALED_PREFIX, apply_plaintext_tokens, is_sealed, unseal_token
from cuswap.service import refresh_bundle, session_is_usable


def _jwt(exp: datetime) -> str:
    import base64
    import json

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    header = b64(b'{"alg":"none","typ":"JWT"}')
    body = b64(json.dumps({"sub": "auth0|user", "exp": int(exp.timestamp())}).encode())
    return f"{header}.{body}.sig"


def test_plaintext_token_passes_through():
    assert unseal_token("eyJ.session.sig") == "eyJ.session.sig"
    assert is_sealed("eyJ.session.sig") is False
    assert is_sealed(f"{SEALED_PREFIX}abc") is True


def test_apply_plaintext_tokens_replaces_sealed_values(monkeypatch):
    monkeypatch.setattr(
        "cuswap.sealed.unseal_token",
        lambda value: "eyJ.plain.sig" if is_sealed(value) else value,
    )
    bundle = SessionBundle(
        keys=[
            EncodedKey(key="cursorAuth/accessToken", value=f"{SEALED_PREFIX}blob"),
            EncodedKey(key="cursorAuth/refreshToken", value=f"{SEALED_PREFIX}other"),
            EncodedKey(key="cursorAuth/cachedEmail", value="one@example.com"),
        ]
    )
    assert apply_plaintext_tokens(bundle) is True
    assert bundle.get("cursorAuth/accessToken") == "eyJ.plain.sig"
    assert bundle.get("cursorAuth/refreshToken") == "eyJ.plain.sig"
    assert bundle.get("cursorAuth/cachedEmail") == "one@example.com"
    assert apply_plaintext_tokens(bundle) is False


def test_failed_unseal_leaves_ciphertext(monkeypatch):
    monkeypatch.setattr("cuswap.sealed.unseal_token", lambda _value: None)
    bundle = SessionBundle(
        keys=[EncodedKey(key="cursorAuth/accessToken", value=f"{SEALED_PREFIX}blob")]
    )
    assert apply_plaintext_tokens(bundle) is False
    assert bundle.get("cursorAuth/accessToken") == f"{SEALED_PREFIX}blob"


def test_read_live_bundle_unseals_before_return(tmp_path, monkeypatch):
    db = tmp_path / "state.vscdb"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO ItemTable VALUES (?, ?)",
        ("cursorAuth/accessToken", f"{SEALED_PREFIX}blob"),
    )
    conn.execute("INSERT INTO ItemTable VALUES (?, ?)", ("cursorAuth/cachedEmail", "one@example.com"))
    conn.commit()
    conn.close()

    def fake_apply(bundle: SessionBundle) -> bool:
        bundle.set("cursorAuth/accessToken", "eyJ.plain.sig")
        return True

    monkeypatch.setattr("cuswap.cursor_db.apply_plaintext_tokens", fake_apply)
    bundle = read_live_bundle(db)
    assert bundle.get("cursorAuth/accessToken") == "eyJ.plain.sig"
    assert bundle.get("cursorAuth/cachedEmail") == "one@example.com"


def test_refresh_does_not_post_sealed_token(monkeypatch):
    def boom(_token: str):
        raise AssertionError("sealed token must not be sent to the refresh endpoint")

    monkeypatch.setattr("cuswap.service.usage.refresh_session", boom)
    bundle = SessionBundle(
        keys=[EncodedKey(key="cursorAuth/refreshToken", value=f"{SEALED_PREFIX}blob")]
    )
    updated, status = refresh_bundle(bundle)
    assert status == "token_expired"
    assert updated.get("cursorAuth/refreshToken") == f"{SEALED_PREFIX}blob"


def test_session_is_usable_rejects_sealed_and_expired():
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    assert session_is_usable(_jwt(future)) is True
    assert session_is_usable(_jwt(past)) is False
    assert session_is_usable(f"{SEALED_PREFIX}blob") is False
    assert session_is_usable(None) is False
    assert session_is_usable("not-a-jwt") is False


def test_unseal_rejects_non_v10_payload(monkeypatch):
    monkeypatch.setattr("cuswap.sealed.sys.platform", "win32")
    import base64

    blob = base64.b64encode(b"nope").decode("ascii")
    assert unseal_token(f"{SEALED_PREFIX}{blob}") is None
