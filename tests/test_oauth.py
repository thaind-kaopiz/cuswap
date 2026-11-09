from urllib.parse import parse_qs, urlparse

from cuswap.oauth import start_session
from cuswap.service import bundle_from_tokens


def test_start_session_url_shape():
    session = start_session()
    parsed = urlparse(session.login_url)
    assert parsed.netloc == "cursor.com"
    assert parsed.path == "/loginDeepControl"
    query = parse_qs(parsed.query)
    assert query["mode"] == ["login"]
    assert query["redirectTarget"] == ["cli"]
    assert query["challenge"] == [session.challenge]
    assert query["uuid"] == [session.uuid]
    assert session.verifier
    assert session.challenge != session.verifier


def test_bundle_from_tokens_sets_core_keys(monkeypatch):
    from cuswap import usage

    def fake_me(_token):
        return {"email": "alt@example.com", "name": "Alt", "id": 9}

    def fake_summary(_token):
        return {"membershipType": "pro"}

    monkeypatch.setattr(usage, "fetch_me", fake_me)
    monkeypatch.setattr(usage, "fetch_usage_summary", fake_summary)
    bundle = bundle_from_tokens("access-token", "refresh-token")
    assert bundle.get("cursorAuth/accessToken") == "access-token"
    assert bundle.get("cursorAuth/refreshToken") == "refresh-token"
    assert bundle.get("cursorAuth/cachedEmail") == "alt@example.com"
    assert bundle.get("cursorAuth/stripeMembershipType") == "pro"
