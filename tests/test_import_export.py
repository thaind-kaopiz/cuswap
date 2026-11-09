import json

import pytest

from cuswap.models import AccountIndex, EncodedKey, SessionBundle
from cuswap.paths import resolve_sidecar
from cuswap.sealed import SEALED_PREFIX
from cuswap.store import (
    StoreError,
    export_accounts,
    import_accounts,
    load_bundle,
    load_index,
    upsert_account,
)


def _bundle(token: str, *, email: str, sidecar: str | None = None) -> SessionBundle:
    sidecars = {}
    if sidecar:
        sidecars[sidecar] = "{\"auth\":true}"
    return SessionBundle(
        keys=[
            EncodedKey(key="cursorAuth/accessToken", value=token),
            EncodedKey(key="cursorAuth/refreshToken", value=f"refresh-{token}"),
            EncodedKey(key="cursorAuth/cachedEmail", value=email),
        ],
        sidecars=sidecars,
    )


def test_export_import_roundtrip(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("CUSWAP_HOME", str(home))
    index = AccountIndex()
    first = upsert_account(
        index,
        email="a@x.com",
        name="A",
        membership_type="pro",
        bundle=_bundle("access-1", email="a@x.com", sidecar=str(tmp_path / "machine" / "auth.json")),
    )
    upsert_account(index, email="b@x.com", name="B", bundle=_bundle("access-2", email="b@x.com"))
    index.active_id = first.id
    from cuswap.store import save_index

    save_index(index)

    dest = tmp_path / "cuswap-export.json"
    assert export_accounts(dest, load_index()) == 2
    raw = json.loads(dest.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert raw["active_email"] == "a@x.com"
    assert raw["accounts"][0]["bundle"]["sidecars"] == {"auth.json": "{\"auth\":true}"}
    assert "access-1" in dest.read_text(encoding="utf-8")

    monkeypatch.setenv("CUSWAP_HOME", str(tmp_path / "other"))
    added, updated = import_accounts(dest)
    assert (added, updated) == (2, 0)
    loaded = load_index()
    assert loaded.by_email("a@x.com").name == "A"
    assert loaded.by_email("a@x.com").membership_type == "pro"
    assert loaded.by_id(loaded.active_id).email == "a@x.com"
    restored = load_bundle(loaded.by_email("a@x.com").id)
    assert restored.get("cursorAuth/accessToken") == "access-1"
    assert restored.sidecars == {"auth.json": "{\"auth\":true}"}


def test_import_updates_same_email_and_skips_taken_slot(tmp_path, monkeypatch):
    monkeypatch.setenv("CUSWAP_HOME", str(tmp_path / "home"))
    index = AccountIndex()
    kept = upsert_account(index, email="a@x.com", name="Old", bundle=_bundle("old", email="a@x.com"))
    upsert_account(index, email="local@x.com", bundle=_bundle("local", email="local@x.com"))
    payload = {
        "version": 1,
        "active_email": "a@x.com",
        "accounts": [
            {
                "meta": {"email": "a@x.com", "name": "New", "slot": 1, "membership_type": "pro"},
                "bundle": _bundle("fresh", email="a@x.com").to_dict(),
            },
            {
                "meta": {"email": "c@x.com", "name": "C", "slot": 2},
                "bundle": _bundle("access-3", email="c@x.com").to_dict(),
            },
        ],
    }
    path = tmp_path / "in.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    added, updated = import_accounts(path)
    assert (added, updated) == (1, 1)
    loaded = load_index()
    assert loaded.by_email("a@x.com").id == kept.id
    assert loaded.by_email("a@x.com").name == "New"
    assert load_bundle(kept.id).get("cursorAuth/accessToken") == "fresh"
    assert loaded.by_email("local@x.com").slot == 2
    assert loaded.by_email("c@x.com").slot == 3
    assert loaded.active_id == kept.id


def test_import_replace_drops_accounts_missing_from_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CUSWAP_HOME", str(tmp_path / "home"))
    index = AccountIndex()
    upsert_account(index, email="gone@x.com", bundle=_bundle("gone", email="gone@x.com"))
    payload = {
        "version": 1,
        "active_email": "keep@x.com",
        "accounts": [
            {
                "meta": {"email": "keep@x.com", "name": "Keep", "slot": 4},
                "bundle": _bundle("keep", email="keep@x.com").to_dict(),
            }
        ],
    }
    path = tmp_path / "in.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    added, updated = import_accounts(path, replace=True)
    assert (added, updated) == (1, 0)
    loaded = load_index()
    assert loaded.by_email("gone@x.com") is None
    assert loaded.by_email("keep@x.com").slot == 4
    assert loaded.by_id(loaded.active_id).email == "keep@x.com"


def test_export_rejects_sealed_token(tmp_path, monkeypatch):
    monkeypatch.setenv("CUSWAP_HOME", str(tmp_path / "home"))
    index = AccountIndex()
    upsert_account(
        index,
        email="a@x.com",
        bundle=SessionBundle(
            keys=[EncodedKey(key="cursorAuth/accessToken", value=f"{SEALED_PREFIX}blob")]
        ),
    )
    with pytest.raises(StoreError, match="sealed"):
        export_accounts(tmp_path / "out.json", load_index())


def test_resolve_sidecar_basename(tmp_path, monkeypatch):
    preferred = tmp_path / "cursor" / "auth.json"
    monkeypatch.setattr(
        "cuswap.paths.sidecar_candidates",
        lambda: [preferred, tmp_path / "cli" / "auth.json"],
    )
    assert resolve_sidecar("auth.json") == preferred
    absolute = tmp_path / "abs" / "auth.json"
    assert resolve_sidecar(str(absolute)) == absolute
