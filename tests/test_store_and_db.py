import sqlite3
from pathlib import Path

from cuswap.cursor_db import (
    backup_path,
    db_status,
    read_live_bundle,
    repair_state_db,
    shm_path,
    wal_path,
    write_bundle,
)
from cuswap.fsutil import atomic_write_bytes, replace_with_retry
from cuswap.models import AccountIndex, EncodedKey, SessionBundle
from cuswap.paths import is_auth_key
from cuswap.store import load_index, resolve_account, upsert_account


def test_is_auth_key():
    assert is_auth_key("cursorAuth/accessToken")
    assert is_auth_key("glass.lastSignedInAuthId")
    assert is_auth_key("glass.cloudAgentProjects.v1")
    assert not is_auth_key("glass.theme.settingsId")
    assert not is_auth_key("cursor.commands.recentlyUsed")


def _make_db(path: Path, *, wal: bool = False) -> None:
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO ItemTable VALUES (?, ?)", ("cursorAuth/cachedEmail", "one@example.com"))
    conn.execute("INSERT INTO ItemTable VALUES (?, ?)", ("cursorAuth/accessToken", "token-one"))
    conn.execute("INSERT INTO ItemTable VALUES (?, ?)", ("glass.theme.settingsId", "keep-me"))
    conn.execute(
        "INSERT INTO ItemTable VALUES (?, ?)",
        (
            "src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl.persistentStorage.applicationUser",
            "keep-user",
        ),
    )
    conn.execute("INSERT INTO ItemTable VALUES (?, ?)", ("glass.lastSignedInAuthId", "old-id"))
    conn.execute("INSERT INTO ItemTable VALUES (?, ?)", ("cursorAuth/refreshToken", "old-refresh"))
    conn.commit()
    conn.close()


def test_sqlite_roundtrip(tmp_path):
    db = tmp_path / "state.vscdb"
    _make_db(db)

    bundle = read_live_bundle(db)
    assert bundle.get("cursorAuth/cachedEmail") == "one@example.com"
    assert any(item.key == "glass.theme.settingsId" for item in bundle.keys) is False

    incoming = SessionBundle(
        keys=[
            EncodedKey(key="cursorAuth/cachedEmail", value="two@example.com"),
            EncodedKey(key="cursorAuth/accessToken", value="token-two"),
        ]
    )
    write_bundle(incoming, db)
    conn = sqlite3.connect(db)
    rows = dict(conn.execute("SELECT key, value FROM ItemTable").fetchall())
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()
    assert rows["cursorAuth/cachedEmail"] == "two@example.com"
    assert rows["cursorAuth/accessToken"] == "token-two"
    assert rows["glass.theme.settingsId"] == "keep-me"
    assert (
        rows["src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl.persistentStorage.applicationUser"]
        == "keep-user"
    )
    assert rows["glass.lastSignedInAuthId"] == "old-id"
    assert "cursorAuth/refreshToken" not in rows


def test_wal_write_stays_healthy(tmp_path):
    db = tmp_path / "state.vscdb"
    _make_db(db, wal=True)
    assert wal_path(db).exists() or db_status(db) == "ok"

    write_bundle(
        SessionBundle(
            keys=[
                EncodedKey(key="cursorAuth/cachedEmail", value="two@example.com"),
                EncodedKey(key="cursorAuth/accessToken", value="token-two"),
            ]
        ),
        db,
    )
    assert db_status(db) == "ok"
    conn = sqlite3.connect(db)
    rows = dict(conn.execute("SELECT key, value FROM ItemTable").fetchall())
    conn.close()
    assert rows["cursorAuth/cachedEmail"] == "two@example.com"
    assert rows["glass.theme.settingsId"] == "keep-me"


def test_repair_removes_stale_wal(tmp_path):
    db = tmp_path / "state.vscdb"
    _make_db(db, wal=True)
    # A leftover WAL that does not belong to this main file is the usual
    # Cursor "file storage corrupted" case after a naive copy/replace.
    wal_path(db).write_bytes(b"this is not a wal file" * 50)
    shm_path(db).write_bytes(b"\x00" * 32)
    note = repair_state_db(db)
    assert "ok" in note
    assert db_status(db) == "ok"
    assert read_live_bundle(db).get("cursorAuth/cachedEmail") == "one@example.com"


def test_repair_restores_backup(tmp_path):
    db = tmp_path / "state.vscdb"
    _make_db(db)
    backup = backup_path(db)
    backup.write_bytes(db.read_bytes())
    db.write_bytes(b"not a sqlite database")
    wal_path(db).write_bytes(b"junk")
    note = repair_state_db(db)
    assert "restored-backup" in note
    assert db_status(db) == "ok"
    assert read_live_bundle(db).get("cursorAuth/cachedEmail") == "one@example.com"


def _make_degraded_db(path: Path) -> None:
    """ItemTable is healthy; another btree is damaged like Cursor's cursorDiskKV."""
    _make_db(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA page_size=4096")
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
    for index in range(40):
        conn.execute("INSERT INTO cursorDiskKV VALUES (?, zeroblob(8000))", (f"k{index}",))
    conn.commit()
    conn.close()
    data = bytearray(path.read_bytes())
    offset = 4096 * 20
    for index in range(64):
        data[offset + index] ^= 0xA5
    path.write_bytes(data)


def test_degraded_itemtable_is_writable(tmp_path):
    db = tmp_path / "state.vscdb"
    _make_degraded_db(db)
    assert db_status(db) == "degraded"
    assert repair_state_db(db) == "degraded"
    write_bundle(
        SessionBundle(
            keys=[
                EncodedKey(key="cursorAuth/cachedEmail", value="two@example.com"),
                EncodedKey(key="cursorAuth/accessToken", value="token-two"),
            ]
        ),
        db,
    )
    assert read_live_bundle(db).get("cursorAuth/cachedEmail") == "two@example.com"
    conn = sqlite3.connect(db)
    rows = dict(conn.execute("SELECT key, value FROM ItemTable").fetchall())
    conn.close()
    assert rows["glass.theme.settingsId"] == "keep-me"


def test_atomic_replace(tmp_path):
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("hello", encoding="utf-8")
    dst.write_text("old", encoding="utf-8")
    replace_with_retry(src, dst)
    assert dst.read_text(encoding="utf-8") == "hello"
    assert not src.exists()
    atomic_write_bytes(dst, b"world")
    assert dst.read_bytes() == b"world"


def test_upsert_and_resolve(tmp_path, monkeypatch):
    monkeypatch.setenv("CUSWAP_HOME", str(tmp_path))
    index = AccountIndex()
    bundle = SessionBundle(keys=[EncodedKey(key="cursorAuth/accessToken", value="abc")])
    first = upsert_account(index, email="a@x.com", bundle=bundle)
    second = upsert_account(index, email="b@x.com", bundle=bundle)
    assert first.slot == 1
    assert second.slot == 2
    loaded = load_index()
    assert resolve_account(loaded, "1").email == "a@x.com"
    assert resolve_account(loaded, "b@x.com").slot == 2
    again = upsert_account(loaded, email="a@x.com", bundle=bundle)
    assert again.id == first.id
    assert load_index().by_email("a@x.com").email == "a@x.com"