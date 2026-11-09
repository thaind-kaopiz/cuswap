from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from cuswap.fsutil import replace_with_retry
from cuswap.models import EncodedKey, SessionBundle
from cuswap.paths import is_auth_key, sidecar_candidates, state_db_path
from cuswap.sealed import apply_plaintext_tokens


class CursorDbError(RuntimeError):
    pass


def encode_value(value: object) -> EncodedKey | None:
    import base64

    if value is None:
        return None
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError:
            return EncodedKey(
                key="",
                value=base64.b64encode(value).decode("ascii"),
                encoding="base64",
            )
        return EncodedKey(key="", value=text, encoding="text")
    return EncodedKey(key="", value=str(value), encoding="text")


def decode_value(item: EncodedKey) -> str | bytes:
    import base64

    if item.encoding == "base64":
        return base64.b64decode(item.value)
    return item.value


def _sidecar(path: Path, suffix: str) -> Path:
    return Path(f"{path}{suffix}")


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def wal_path(db_path: Path) -> Path:
    return _sidecar(db_path, "-wal")


def shm_path(db_path: Path) -> Path:
    return _sidecar(db_path, "-shm")


def backup_path(db_path: Path) -> Path:
    return Path(f"{db_path}.backup")


def corrupted_candidates(db_path: Path) -> list[Path]:
    return sorted(db_path.parent.glob(f"{db_path.name}.corrupted.*"), reverse=True)


def _is_lock_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _connect_probe(db_path: Path) -> sqlite3.Connection:
    # immutable=1 avoids WAL/SHM sidecars and Windows share locks during status checks.
    uri = f"file:{db_path.as_posix()}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True, timeout=2.0)


def _itemtable_usable(db_path: Path) -> bool:
    """True when auth keys can still be read — even if another table is damaged."""
    conn: sqlite3.Connection | None = None
    try:
        conn = _connect_probe(db_path)
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ItemTable'"
        ).fetchone()
        if not row:
            return False
        conn.execute("SELECT key FROM ItemTable LIMIT 1")
        return True
    except sqlite3.Error:
        return False
    finally:
        if conn is not None:
            conn.close()


def db_status(db_path: Path) -> str:
    """Return ok, degraded, missing, locked, or corrupt.

    Cursor's state.vscdb often fails PRAGMA integrity_check because
    ``cursorDiskKV`` has a torn btree, while ItemTable (auth keys) still
    reads and writes. That case is ``degraded``, not ``corrupt``.
    """
    if not db_path.is_file():
        return "missing"
    try:
        conn = _connect_probe(db_path)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
            if row and row[0] == "ok":
                return "ok"
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if _is_lock_error(exc):
            return "locked"
        return "degraded" if _itemtable_usable(db_path) else "corrupt"
    except sqlite3.DatabaseError as exc:
        text = str(exc).lower()
        if "not a database" in text:
            return "corrupt"
        return "degraded" if _itemtable_usable(db_path) else "corrupt"
    return "degraded" if _itemtable_usable(db_path) else "corrupt"


def wait_until_writable(db_path: Path | None = None, *, timeout: float = 20.0) -> None:
    path = db_path or state_db_path()
    deadline = time.time() + timeout
    last = "not attempted"
    while time.time() < deadline:
        try:
            conn = sqlite3.connect(str(path), timeout=0.4)
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.rollback()
            finally:
                conn.close()
            return
        except sqlite3.OperationalError as exc:
            last = str(exc)
            time.sleep(0.25)
    raise CursorDbError(f"Cursor state DB is still locked after {timeout:.0f}s: {last}")


def _connect_rw(path: Path, *, timeout: float = 20.0) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=timeout)
    conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
    return conn


def repair_state_db(db_path: Path | None = None) -> str:
    """Fix a torn WAL/SHM pair or restore a healthy backup. Cursor must be closed.

    If ItemTable is still usable, return ``degraded`` instead of failing —
    cuswap only patches auth keys and must not block on cursorDiskKV damage.
    """
    path = db_path or state_db_path()
    if not path.is_file():
        raise CursorDbError(f"Cursor state DB not found: {path}")

    status = db_status(path)
    if status in {"ok", "degraded"}:
        return status
    if status == "locked":
        raise CursorDbError("Cursor still has state.vscdb open. Quit Cursor and retry.")

    notes: list[str] = [status]
    wal = wal_path(path)
    shm = shm_path(path)
    if wal.exists() or shm.exists():
        _unlink_quiet(wal)
        _unlink_quiet(shm)
        notes.append("removed-stale-wal")
        status = db_status(path)
        if status in {"ok", "degraded"}:
            notes.append(status)
            return ",".join(notes)

    for label, candidate in _recovery_sources(path):
        if db_status(candidate) not in {"ok", "degraded"}:
            continue
        _restore_file(candidate, path)
        notes.append(f"restored-{label}")
        status = db_status(path)
        if status in {"ok", "degraded"}:
            notes.append(status)
            return ",".join(notes)

    if _itemtable_usable(path):
        notes.append("degraded")
        return ",".join(notes)

    raise CursorDbError(
        "Cursor state DB is corrupted and no healthy backup was found. "
        "Close Cursor, then run `cuswap doctor --repair`. If that fails, "
        "sign in again after deleting state.vscdb-wal and state.vscdb-shm."
    )


def _recovery_sources(path: Path) -> list[tuple[str, Path]]:
    sources: list[tuple[str, Path]] = []
    backup = backup_path(path)
    if backup.is_file():
        sources.append(("backup", backup))
    for item in corrupted_candidates(path):
        sources.append((item.name, item))
    return sources


def _restore_file(src: Path, dest: Path) -> None:
    tmp = dest.with_name(dest.name + ".cuswap-restore")
    _unlink_quiet(tmp)
    data = src.read_bytes()
    tmp.write_bytes(data)
    _unlink_quiet(wal_path(dest))
    _unlink_quiet(shm_path(dest))
    _unlink_quiet(dest)
    replace_with_retry(tmp, dest)


def read_live_bundle(db_path: Path | None = None) -> SessionBundle:
    path = db_path or state_db_path()
    if not path.is_file():
        raise CursorDbError(f"Cursor state DB not found: {path}")
    keys: list[EncodedKey] = []
    uri = f"file:{path.as_posix()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=8.0) as conn:
            rows = conn.execute("SELECT key, value FROM ItemTable").fetchall()
    except sqlite3.DatabaseError as exc:
        raise CursorDbError(f"Could not read Cursor state DB: {exc}") from exc
    for key, value in rows:
        if not is_auth_key(str(key)):
            continue
        encoded = encode_value(value)
        if encoded is None:
            continue
        encoded.key = str(key)
        keys.append(encoded)
    if not keys:
        raise CursorDbError("No Cursor login found in the local state DB. Sign in to Cursor first.")
    sidecars: dict[str, str] = {}
    for sidecar in sidecar_candidates():
        if sidecar.is_file():
            sidecars[str(sidecar)] = sidecar.read_text(encoding="utf-8")
    bundle = SessionBundle(keys=keys, sidecars=sidecars)
    # Cursor persists auth tokens encrypted. Keep the in-memory bundle as plaintext
    # so a later sync does not store ciphertext and mark the account needs_reauth.
    apply_plaintext_tokens(bundle)
    return bundle


def live_email(bundle: SessionBundle | None = None) -> str | None:
    data = bundle or read_live_bundle()
    return data.get("cursorAuth/cachedEmail")


def live_access_token(bundle: SessionBundle | None = None) -> str | None:
    data = bundle or read_live_bundle()
    return data.get("cursorAuth/accessToken")


def _apply_bundle(conn: sqlite3.Connection, bundle: SessionBundle) -> None:
    incoming = {item.key: decode_value(item) for item in bundle.keys}
    existing = [str(key) for (key,) in conn.execute("SELECT key FROM ItemTable")]
    for key in existing:
        # Incomplete browser-login bundles must not wipe IDE identity keys.
        if key.startswith("cursorAuth/") and key not in incoming:
            conn.execute("DELETE FROM ItemTable WHERE key = ?", (key,))
    for key, value in incoming.items():
        conn.execute("DELETE FROM ItemTable WHERE key = ?", (key,))
        conn.execute("INSERT INTO ItemTable (key, value) VALUES (?, ?)", (key, value))


def _checkpoint(conn: sqlite3.Connection) -> None:
    # PASSIVE avoids growing the 1GB+ main file when the system drive is full.
    # Do not require PRAGMA integrity_check — cursorDiskKV is often damaged
    # while ItemTable writes succeeded.
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except sqlite3.DatabaseError:
        return


def write_bundle(bundle: SessionBundle, db_path: Path | None = None) -> None:
    """Patch auth keys in-place. Never copy/replace the whole vscdb or its WAL/SHM.

    Copying state.vscdb together with -wal/-shm (or leaving a stale -shm next to
    a replaced main file) is what makes Cursor rename the DB to
    state.vscdb.corrupted.* on the next launch.
    """
    path = db_path or state_db_path()
    if not path.is_file():
        raise CursorDbError(f"Cursor state DB not found: {path}")
    if not bundle.keys:
        raise CursorDbError("Refusing to write an empty auth bundle")

    status = db_status(path)
    if status not in {"ok", "degraded"}:
        repair_state_db(path)
    wait_until_writable(path, timeout=20.0)

    conn = _connect_rw(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _apply_bundle(conn, bundle)
        conn.commit()
        _checkpoint(conn)
    finally:
        conn.close()

    _write_sidecars(bundle)


def clear_auth(db_path: Path | None = None) -> None:
    path = db_path or state_db_path()
    if not path.is_file():
        raise CursorDbError(f"Cursor state DB not found: {path}")
    status = db_status(path)
    if status not in {"ok", "degraded"}:
        repair_state_db(path)
    wait_until_writable(path, timeout=20.0)
    conn = _connect_rw(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT key FROM ItemTable").fetchall()
        for (key,) in existing:
            if is_auth_key(str(key)):
                conn.execute("DELETE FROM ItemTable WHERE key = ?", (key,))
        conn.commit()
        _checkpoint(conn)
    finally:
        conn.close()


def _write_sidecars(bundle: SessionBundle) -> None:
    for sidecar_path, content in bundle.sidecars.items():
        target = Path(sidecar_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".cuswap-tmp")
        tmp.write_text(content, encoding="utf-8")
        replace_with_retry(tmp, target)


def inspect_storage(db_path: Path | None = None) -> dict[str, object]:
    path = db_path or state_db_path()
    return {
        "state_db": str(path),
        "exists": path.is_file(),
        "status": db_status(path) if path.is_file() else "missing",
        "wal_exists": wal_path(path).exists(),
        "shm_exists": shm_path(path).exists(),
        "backup_exists": backup_path(path).is_file(),
        "corrupted_files": [str(item) for item in corrupted_candidates(path)],
    }