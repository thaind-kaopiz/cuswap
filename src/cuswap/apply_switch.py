from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from cuswap import cursor_db, cursor_proc
from cuswap.crypto import read_secret_file, write_secret_file
from cuswap.models import SessionBundle
from cuswap.paths import pending_switch_path, switch_log_path


def _log(message: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat()} {message}\n"
    path = switch_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def write_pending(payload: dict) -> Path:
    path = pending_switch_path()
    write_secret_file(path, json.dumps(payload).encode("utf-8"))
    return path


def load_pending() -> dict:
    raw = read_secret_file(pending_switch_path())
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("pending switch payload is invalid")
    return data


def helper_python() -> Path:
    current = Path(sys.executable)
    pythonw = current.with_name("pythonw.exe")
    if pythonw.is_file():
        return pythonw
    return current


def helper_argv() -> list[str]:
    # Never reuse cuswap.exe — the console shim opens a blank CMD.
    return [str(helper_python()), "-m", "cuswap.apply_switch"]


def _verify_email() -> str | None:
    return cursor_db.read_text_key("cursorAuth/cachedEmail")


def run() -> int:
    _log("apply-switch started")
    try:
        payload = load_pending()
    except Exception as exc:  # noqa: BLE001
        _log(f"no pending switch: {exc}")
        return 1

    email = str(payload.get("email") or "")
    restart = bool(payload.get("restart", True))
    bundle = SessionBundle.from_dict(payload.get("bundle") or {})
    _log(f"target={email} restart={restart} pids_before={cursor_proc.cursor_pids()}")

    time.sleep(0.2)
    try:
        cursor_proc.quit_cursor(timeout=12, force=True)
        _log(f"cursor quit; pids_after={cursor_proc.cursor_pids()}")
    except Exception as exc:  # noqa: BLE001
        _log(f"quit warning: {exc}; pids={cursor_proc.cursor_pids()}")
        # Last resort: force again so the DB unlocks.
        try:
            cursor_proc.quit_cursor(timeout=8, force=True)
            _log(f"cursor force-quit retry; pids={cursor_proc.cursor_pids()}")
        except Exception as retry_exc:  # noqa: BLE001
            _log(f"quit failed: {retry_exc}")
            return 4

    last_error = "write not attempted"
    for attempt in range(1, 6):
        try:
            cursor_db.write_bundle(bundle)
            last_error = ""
            break
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            _log(f"write attempt {attempt} failed: {exc}")
            if attempt == 1:
                try:
                    if cursor_db.clear_stale_wal():
                        _log("cleared stale wal/shm")
                except Exception as repair_exc:  # noqa: BLE001
                    _log(f"wal clear skipped: {repair_exc}")
            # Remaining Cursor process holding the lock?
            if cursor_proc.is_running():
                _log(f"cursor still running during write: {cursor_proc.cursor_pids()}")
                try:
                    cursor_proc.quit_cursor(timeout=6, force=True)
                except Exception as quit_exc:  # noqa: BLE001
                    _log(f"re-quit failed: {quit_exc}")
            time.sleep(0.5)
    if last_error:
        _log(f"write failed: {last_error}")
        return 2

    actual = _verify_email()
    _log(f"wrote email={actual}")
    if email and actual and actual.lower() != email.lower():
        _log("verify mismatch; rewriting")
        try:
            cursor_db.write_bundle(bundle)
            actual = _verify_email()
            _log(f"rewrote email={actual}")
        except Exception as exc:  # noqa: BLE001
            _log(f"rewrite failed: {exc}")
            return 2
        if email and actual and actual.lower() != email.lower():
            _log("verify still mismatched after rewrite")
            return 2

    pending_switch_path().unlink(missing_ok=True)
    if restart:
        try:
            cursor_proc.start_cursor()
            _log("started Cursor")
        except Exception as exc:  # noqa: BLE001
            _log(f"start failed: {exc}")
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
