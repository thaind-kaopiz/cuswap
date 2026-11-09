from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class EncodedKey:
    key: str
    value: str
    encoding: str = "text"

    def to_dict(self) -> dict[str, str]:
        return {"key": self.key, "value": self.value, "encoding": self.encoding}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EncodedKey:
        return cls(
            key=str(data["key"]),
            value=str(data["value"]),
            encoding=str(data.get("encoding") or "text"),
        )


@dataclass
class SessionBundle:
    keys: list[EncodedKey] = field(default_factory=list)
    sidecars: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "keys": [item.to_dict() for item in self.keys],
            "sidecars": self.sidecars,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionBundle:
        return cls(
            keys=[EncodedKey.from_dict(item) for item in data.get("keys") or []],
            sidecars={str(k): str(v) for k, v in (data.get("sidecars") or {}).items()},
        )

    def get(self, key: str) -> str | None:
        for item in self.keys:
            if item.key == key and item.encoding == "text":
                return item.value
        return None

    def set(self, key: str, value: str, encoding: str = "text") -> None:
        for item in self.keys:
            if item.key == key:
                item.value = value
                item.encoding = encoding
                return
        self.keys.append(EncodedKey(key=key, value=value, encoding=encoding))


@dataclass
class Account:
    id: str
    slot: int
    email: str
    name: str = ""
    alias: str | None = None
    disabled: bool = False
    user_id: int | None = None
    auth_sub: str | None = None
    membership_type: str | None = None
    added_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Account:
        allowed = {item.name for item in fields(cls)}
        payload = {key: data[key] for key in allowed if key in data}
        return cls(**payload)


@dataclass
class AccountIndex:
    version: int = 1
    active_id: str | None = None
    accounts: list[Account] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "active_id": self.active_id,
            "accounts": [account.to_dict() for account in self.accounts],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AccountIndex:
        return cls(
            version=int(data.get("version") or 1),
            active_id=data.get("active_id"),
            accounts=[Account.from_dict(item) for item in data.get("accounts") or []],
        )

    def by_id(self, account_id: str) -> Account | None:
        return next((item for item in self.accounts if item.id == account_id), None)

    def by_email(self, email: str) -> Account | None:
        needle = email.strip().lower()
        return next((item for item in self.accounts if item.email.lower() == needle), None)

    def by_slot(self, slot: int) -> Account | None:
        return next((item for item in self.accounts if item.slot == slot), None)

    def by_alias(self, alias: str) -> Account | None:
        needle = alias.strip().lower()
        return next(
            (item for item in self.accounts if item.alias and item.alias.lower() == needle),
            None,
        )

    def next_slot(self) -> int:
        used = {item.slot for item in self.accounts}
        slot = 1
        while slot in used:
            slot += 1
        return slot

    def sorted_accounts(self) -> list[Account]:
        return sorted(self.accounts, key=lambda item: item.slot)


@dataclass
class UsageSnapshot:
    membership_type: str | None = None
    is_unlimited: bool = False
    used: float | None = None
    limit: float | None = None
    remaining: float | None = None
    percent: float | None = None
    auto_percent: float | None = None
    api_percent: float | None = None
    on_demand_enabled: bool = False
    on_demand_used: float | None = None
    billing_cycle_start: str | None = None
    billing_cycle_end: str | None = None
    fetched_at: str = ""
    error: str | None = None

    @property
    def exhausted(self) -> bool:
        if self.error or self.is_unlimited:
            return False
        pools = [value for value in (self.auto_percent, self.api_percent) if value is not None]
        if pools:
            return all(value >= 100 for value in pools)
        if self.percent is not None:
            return self.percent >= 100
        return False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UsageSnapshot:
        allowed = {item.name for item in fields(cls)}
        payload = {key: data[key] for key in allowed if key in data}
        return cls(**payload)


@dataclass
class AccountRow:
    account: Account
    active: bool
    live: bool
    usage: UsageSnapshot | None = None
    token_status: str = "ok"

    @property
    def status(self) -> str:
        if self.account.disabled:
            return "disabled"
        if self.token_status not in {"ok", "usage_error"}:
            return self.token_status
        if self.usage and self.usage.error:
            return "usage_error"
        if self.usage and self.usage.exhausted:
            return "exhausted"
        if self.active:
            return "active"
        return "ok"
