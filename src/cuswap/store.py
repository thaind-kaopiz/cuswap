from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from cuswap.crypto import CryptoError, read_secret_file, write_secret_file
from cuswap.models import Account, AccountIndex, SessionBundle, UsageSnapshot, isoformat, utcnow
from cuswap.sealed import apply_plaintext_tokens, is_sealed
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


def portable_sidecars(sidecars: dict[str, str]) -> dict[str, str]:
    """Drop machine-specific directories so a sidecar name works on another PC."""
    portable: dict[str, str] = {}
    for key, content in sidecars.items():
        portable[Path(key).name] = content
    return portable


def portable_problems(bundle: SessionBundle) -> list[str]:
    """Reasons this session cannot be restored on another machine."""
    problems: list[str] = []
    access = bundle.get("cursorAuth/accessToken")
    if not access:
        problems.append("missing access token")
    elif is_sealed(access):
        problems.append("access token is sealed to this machine")
    refresh = bundle.get("cursorAuth/refreshToken")
    if refresh and is_sealed(refresh):
        problems.append("refresh token is sealed to this machine")
    return problems


def _prepare_bundle(bundle: SessionBundle) -> SessionBundle:
    apply_plaintext_tokens(bundle)
    bundle.sidecars = portable_sidecars(bundle.sidecars)
    return bundle


def export_accounts(path: Path, index: AccountIndex) -> int:
    active = index.by_id(index.active_id) if index.active_id else None
    payload: dict = {
        "version": 1,
        "exported_at": isoformat(utcnow()),
        "active_email": active.email if active else None,
        "accounts": [],
    }
    for account in index.sorted_accounts():
        bundle = _prepare_bundle(load_bundle(account.id))
        problems = portable_problems(bundle)
        if problems:
            raise StoreError(
                f"{account.email}: {'; '.join(problems)}. "
                "Sign in to that account in Cursor, then run `cuswap add` before exporting."
            )
        meta = account.to_dict()
        meta["alias"] = None
        payload["accounts"].append({"meta": meta, "bundle": bundle.to_dict()})
    from cuswap.fsutil import atomic_write_bytes

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, (json.dumps(payload, indent=2) + "\n").encode("utf-8"))
    return len(payload["accounts"])


def _load_export(path: Path) -> dict:
    if not path.is_file():
        raise StoreError(f"Import file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StoreError(f"Import file is not valid JSON: {path}") from exc
    if not isinstance(data, dict) or int(data.get("version") or 0) != 1:
        raise StoreError("Unsupported export file. Expected version 1.")
    entries = data.get("accounts")
    if not isinstance(entries, list) or not entries:
        raise StoreError("Import file has no accounts.")
    return data


def _entry_from_export(entry: object) -> tuple[dict, SessionBundle]:
    if not isinstance(entry, dict):
        raise StoreError("Import file has an invalid account entry.")
    meta = entry.get("meta") or {}
    bundle_data = entry.get("bundle")
    if not isinstance(meta, dict) or not isinstance(bundle_data, dict):
        raise StoreError("Import file has an invalid account entry.")
    email = str(meta.get("email") or "").strip()
    if not email:
        raise StoreError("Import entry is missing an email.")
    meta = dict(meta)
    meta["email"] = email
    bundle = _prepare_bundle(SessionBundle.from_dict(bundle_data))
    problems = portable_problems(bundle)
    if problems:
        raise StoreError(f"{email}: {'; '.join(problems)}")
    return meta, bundle


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _apply_imported_meta(account: Account, meta: dict, *, now: str) -> None:
    account.name = str(meta.get("name") or account.name)
    account.disabled = bool(meta.get("disabled", account.disabled))
    user_id = _optional_int(meta.get("user_id"))
    if user_id is not None:
        account.user_id = user_id
    if meta.get("auth_sub"):
        account.auth_sub = str(meta["auth_sub"])
    if meta.get("membership_type"):
        account.membership_type = str(meta["membership_type"])
    account.updated_at = now
    account.alias = None


def import_accounts(path: Path, *, replace: bool = False) -> tuple[int, int]:
    """Merge exported sessions into this machine. Does not switch Cursor.

    Matching emails get a new session and keep their local id. New emails are
    added. ``replace`` drops every account already saved on this machine.
    """
    data = _load_export(path)
    parsed = [_entry_from_export(entry) for entry in data["accounts"]]
    previous = load_index()
    previous_active = previous.active_id
    index = AccountIndex() if replace else previous
    now = isoformat(utcnow()) or ""
    added = 0
    updated = 0
    for meta, bundle in parsed:
        email = str(meta["email"])
        existing = index.by_email(email)
        if existing:
            _apply_imported_meta(existing, meta, now=now)
            save_bundle(existing.id, bundle)
            updated += 1
            continue
        requested = _optional_int(meta.get("slot"))
        if requested is None or requested < 1 or index.by_slot(requested):
            requested = index.next_slot()
        account = Account(
            id=str(uuid4()),
            slot=requested,
            email=email,
            name=str(meta.get("name") or ""),
            alias=None,
            disabled=bool(meta.get("disabled")),
            user_id=_optional_int(meta.get("user_id")),
            auth_sub=str(meta["auth_sub"]) if meta.get("auth_sub") else None,
            membership_type=str(meta["membership_type"]) if meta.get("membership_type") else None,
            added_at=str(meta.get("added_at") or now),
            updated_at=now,
        )
        index.accounts.append(account)
        save_bundle(account.id, bundle)
        added += 1

    active_email = str(data.get("active_email") or "").strip()
    if not replace and previous_active and index.by_id(previous_active):
        index.active_id = previous_active
    else:
        match = index.by_email(active_email) if active_email else None
        if match:
            index.active_id = match.id
        elif index.accounts:
            index.active_id = index.sorted_accounts()[0].id
        else:
            index.active_id = None
    if replace:
        imported_ids = {account.id for account in index.accounts}
        for account in previous.accounts:
            if account.id not in imported_ids:
                delete_bundle(account.id)
    save_index(index)
    return added, updated
