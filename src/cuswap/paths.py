from __future__ import annotations

import os
import sys
from pathlib import Path


APPLICATION_USER_KEY = (
    "src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl"
    ".persistentStorage.applicationUser"
)

AUTH_KEY_PREFIXES = ("cursorAuth/",)
AUTH_EXACT_KEYS = frozenset(
    {
        APPLICATION_USER_KEY,
        "glass.lastSignedInAuthId",
    }
)
AUTH_OPTIONAL_PREFIXES = (
    "glass.cloudAgent",
    "glass.localAgent",
)

SIDECAR_NAMES = ("cli-config.json", "statsig-cache.json")


def data_dir() -> Path:
    override = os.environ.get("CUSWAP_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return root / "cuswap"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "cuswap"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "cuswap"
    return Path.home() / ".local" / "share" / "cuswap"


def accounts_index_path() -> Path:
    return data_dir() / "accounts.json"


def settings_path() -> Path:
    return data_dir() / "settings.json"


def usage_cache_path() -> Path:
    return data_dir() / "usage-cache.json"


def pending_switch_path() -> Path:
    return data_dir() / "pending-switch.bin"


def switch_log_path() -> Path:
    return data_dir() / "switch.log"


def credentials_dir() -> Path:
    return data_dir() / "credentials"


def credential_path(account_id: str) -> Path:
    return credentials_dir() / f"{account_id}.bin"


def cursor_user_data_dir() -> Path:
    override = os.environ.get("CUSWAP_CURSOR_USER_DATA")
    if override:
        return Path(override)
    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return root / "Cursor"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Cursor"
    return Path.home() / ".config" / "Cursor"


def state_db_path() -> Path:
    return cursor_user_data_dir() / "User" / "globalStorage" / "state.vscdb"


def cursor_home() -> Path:
    return Path.home() / ".cursor"


def cursor_cli_dir() -> Path:
    if sys.platform == "win32":
        roaming = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return roaming / "cursor"
    if sys.platform == "darwin":
        return Path.home() / ".config" / "cursor"
    return Path.home() / ".config" / "cursor"


def resolve_sidecar(name_or_path: str) -> Path:
    """Map an export sidecar name onto this machine. Absolute paths stay put."""
    path = Path(name_or_path)
    if path.is_absolute():
        return path
    name = path.name
    for candidate in sidecar_candidates():
        if candidate.name == name:
            return candidate
    return cursor_home() / name


def sidecar_candidates() -> list[Path]:
    home = cursor_home()
    cli = cursor_cli_dir()
    names = SIDECAR_NAMES + ("auth.json",)
    found: list[Path] = []
    seen: set[Path] = set()
    for folder in (home, cli):
        for name in names:
            path = folder / name
            if path not in seen:
                found.append(path)
                seen.add(path)
    return found


def default_cursor_exe() -> Path | None:
    override = os.environ.get("CUSWAP_CURSOR_EXE")
    if override:
        path = Path(override)
        return path if path.is_file() else None
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        for candidate in (
            local / "Programs" / "cursor" / "Cursor.exe",
            local / "Programs" / "Cursor" / "Cursor.exe",
        ):
            if candidate.is_file():
                return candidate
        return None
    if sys.platform == "darwin":
        mac = Path("/Applications/Cursor.app/Contents/MacOS/Cursor")
        return mac if mac.is_file() else None
    for candidate in (
        Path("/usr/share/cursor/bin/cursor"),
        Path.home() / ".local" / "bin" / "cursor",
        Path("/usr/bin/cursor"),
    ):
        if candidate.is_file():
            return candidate
    return None


def is_auth_key(key: str) -> bool:
    if key in AUTH_EXACT_KEYS:
        return True
    if any(key.startswith(prefix) for prefix in AUTH_KEY_PREFIXES):
        return True
    return any(key.startswith(prefix) for prefix in AUTH_OPTIONAL_PREFIXES)
