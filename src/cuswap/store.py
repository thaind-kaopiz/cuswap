from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from cuswap.crypto import CryptoError, read_secret_file, write_secret_file
from cuswap.models import Account, AccountIndex, SessionBundle, UsageSnapshot, isoformat, utcnow
from cuswap.sealed import apply_plaintext_tokens
from cuswap.paths import (
    accounts_index_path,
    credential_path,
    credentials_dir,
    data_dir,
    settings_path,
    usage_cache_path,
)


class StoreError(RuntimeError):
    pass


DEFAULT_SETTINGS = {
    "switch": {"restart": True},
    "autoswitch": {"threshold": 90, "strategy": "best", "interval": 60},
}


def ensure_dirs() -> None:
    data_dir().mkdir(parents=True, exist_ok=True)
    credentials_dir().mkdir(parents=True, exist_ok=True)


def load_index() -> AccountIndex:
    path = accounts_index_path()
    if not path.is_file():
        return AccountIndex()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StoreError(f"Corrupt accounts index: {path}") from exc
    return AccountIndex.from_dict(data)


def save_index(index: AccountIndex) -> None:
    from cuswap.fsutil import atomic_write_bytes

    ensure_dirs()
    for account in index.accounts:
        account.alias = None
    path = accounts_index_path()
    payload = (json.dumps(index.to_dict(), indent=2) + "\n").encode("utf-8")
    atomic_write_bytes(path, payload)


def load_settings() -> dict:
    path = settings_path()
    if not path.is_file():
        return json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return json.loads(json.dumps(DEFAULT_SETTINGS))
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))
    for section, values in data.items():
        if isinstance(values, dict) and isinstance(merged.get(section), dict):
            merged[section].update(values)
        else:
            merged[section] = values
    return merged


def save_settings(settings: dict) -> None:
    ensure_dirs()
    path = settings_path()
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def save_bundle(account_id: str, bundle: SessionBundle) -> None:
    ensure_dirs()
    payload = json.dumps(bundle.to_dict(), ensure_ascii=False).encode("utf-8")
    write_secret_file(credential_path(account_id), payload)


def load_bundle(account_id: str) -> SessionBundle:
    path = credential_path(account_id)
    if not path.is_file():
        raise StoreError(f"No stored credentials for account {account_id}")
    try:
        raw = read_secret_file(path)
    except CryptoError as exc:
        raise StoreError(str(exc)) from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StoreError("Stored credentials are unreadable") from exc
    bundle = SessionBundle.from_dict(data)
    if apply_plaintext_tokens(bundle):
        save_bundle(account_id, bundle)
    return bundle


def delete_bundle(account_id: str) -> None:
    path = credential_path(account_id)
    if path.is_file():
        path.unlink()


def load_usage_cache() -> dict[str, UsageSnapshot]:
    path = usage_cache_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    result: dict[str, UsageSnapshot] = {}
    for key, value in (data or {}).items():
        if isinstance(value, dict):
            result[key] = UsageSnapshot.from_dict(value)
    return result


def save_usage_cache(cache: dict[str, UsageSnapshot]) -> None:
    ensure_dirs()
    payload = {key: value.to_dict() for key, value in cache.items()}
    usage_cache_path().write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def upsert_account(
    index: AccountIndex,
    *,
    email: str,
    name: str = "",
    user_id: int | None = None,
    auth_sub: str | None = None,
    membership_type: str | None = None,
    slot: int | None = None,
    bundle: SessionBundle,
) -> Account:
    now = isoformat(utcnow()) or ""
    existing = index.by_email(email)
    if existing:
        existing.name = name or existing.name
        existing.user_id = user_id if user_id is not None else existing.user_id
        existing.auth_sub = auth_sub or existing.auth_sub
        existing.membership_type = membership_type or existing.membership_type
        existing.updated_at = now
        existing.alias = None
        if slot is not None and slot != existing.slot:
            occupant = index.by_slot(slot)
            if occupant and occupant.id != existing.id:
                occupant.slot = existing.slot
            existing.slot = slot
        save_bundle(existing.id, bundle)
        save_index(index)
        return existing

    account = Account(
        id=str(uuid4()),
        slot=slot or index.next_slot(),
        email=email,
        name=name,
        alias=None,
        user_id=user_id,
        auth_sub=auth_sub,
        membership_type=membership_type,
        added_at=now,
        updated_at=now,
    )
    if index.by_slot(account.slot):
        account.slot = index.next_slot()
    index.accounts.append(account)
    if index.active_id is None:
        index.active_id = account.id
    save_bundle(account.id, bundle)
    save_index(index)
    return account


def resolve_account(index: AccountIndex, target: str) -> Account:
    text = target.strip()
    if text.isdigit():
        account = index.by_slot(int(text))
        if account:
            return account
    account = index.by_email(text)
    if account:
        return account
    raise StoreError(f"No account matches {target!r}")


def purge_all() -> None:
    root = data_dir()
    if not root.exists():
        return
    for child in root.rglob("*"):
        if child.is_file():
            child.unlink()
    for child in sorted(root.rglob("*"), reverse=True):
        if child.is_dir():
            child.rmdir()
    if root.exists():
        root.rmdir()


def export_accounts(path: Path, index: AccountIndex) -> None:
    payload = {
        "version": 1,
        "accounts": [],
    }
    for account in index.sorted_accounts():
        payload["accounts"].append(
            {
                "meta": account.to_dict(),
                "bundle": load_bundle(account.id).to_dict(),
            }
        )
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
