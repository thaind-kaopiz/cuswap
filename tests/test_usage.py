from cuswap.models import EncodedKey, SessionBundle, UsageSnapshot
from cuswap.service import merge_usage
from cuswap.usage import parse_refresh_response, parse_usage


def test_parse_uses_cursor_and_other_model_pools():
    snap = parse_usage(
        {
            "membershipType": "pro",
            "isUnlimited": False,
            "billingCycleEnd": "2026-09-10T00:00:00.000Z",
            "individualUsage": {
                "plan": {
                    "used": 2000,
                    "limit": 2000,
                    "remaining": 0,
                    "totalPercentUsed": 71.08,
                    "autoPercentUsed": 79.35,
                    "apiPercentUsed": 15.95,
                },
                "onDemand": {"enabled": False, "used": 0},
            },
        }
    )
    assert snap.membership_type == "pro"
    assert snap.auto_percent == 79.35
    assert snap.api_percent == 15.95
    assert snap.percent == 79.35
    assert snap.exhausted is False


def test_exhausted_only_when_both_pools_are_full():
    snap = parse_usage(
        {
            "individualUsage": {
                "plan": {
                    "used": 2000,
                    "limit": 2000,
                    "remaining": 0,
                    "autoPercentUsed": 100,
                    "apiPercentUsed": 100,
                }
            }
        }
    )
    assert snap.exhausted is True


def test_unlimited():
    snap = parse_usage({"isUnlimited": True, "individualUsage": {"plan": {}}})
    assert snap.is_unlimited
    assert snap.exhausted is False


def test_parse_refresh_success():
    result = parse_refresh_response(
        {"access_token": "new-access", "id_token": "id", "shouldLogout": False}
    )
    assert result.ok
    assert result.access_token == "new-access"
    assert result.should_logout is False


def test_parse_refresh_logout():
    result = parse_refresh_response(
        {"access_token": "", "id_token": "", "shouldLogout": True}
    )
    assert result.ok is False
    assert result.should_logout is True
    assert result.error == "needs_reauth"


def test_merge_keeps_last_good_usage():
    good = UsageSnapshot(used=100, limit=2000, remaining=1900, percent=5, fetched_at="old")
    failed = UsageSnapshot(error="token_expired", fetched_at="new")
    merged = merge_usage(good, failed)
    assert merged.used == 100
    assert merged.error is None


def test_bundle_set_updates_token():
    bundle = SessionBundle(keys=[EncodedKey(key="cursorAuth/accessToken", value="old")])
    bundle.set("cursorAuth/accessToken", "new")
    bundle.set("cursorAuth/refreshToken", "refresh")
    assert bundle.get("cursorAuth/accessToken") == "new"
    assert bundle.get("cursorAuth/refreshToken") == "refresh"
