from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from cuswap.paths import default_cursor_exe, state_db_path


class CursorProcessError(RuntimeError):
    pass


def _win_cursor_running() -> bool:
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Cursor.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=False,
    )
    return "Cursor.exe" in (result.stdout or "")


def is_running() -> bool:
    if sys.platform == "win32":
        return _win_cursor_running()
    result = subprocess.run(
        ["pgrep", "-x", "Cursor"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return True
    result = subprocess.run(
        ["pgrep", "-x", "cursor"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def quit_cursor(*, timeout: float = 20.0, force: bool = True) -> None:
    if not is_running():
        _wait_for_db_writable(timeout=6.0)
        return
    if sys.platform == "win32":
        # Do not use /T — that kills the switch helper if it is still
        # loosely attached to Cursor's process tree.
        subprocess.run(["taskkill", "/IM", "Cursor.exe"], capture_output=True, check=False)
    else:
        subprocess.run(["pkill", "-x", "Cursor"], capture_output=True, check=False)
        subprocess.run(["pkill", "-x", "cursor"], capture_output=True, check=False)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_running():
            _wait_for_db_writable(timeout=8.0)
            return
        time.sleep(0.4)

    if not force:
        raise CursorProcessError("Cursor is still running")
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/IM", "Cursor.exe"], capture_output=True, check=False)
    else:
        subprocess.run(["pkill", "-9", "-x", "Cursor"], capture_output=True, check=False)
        subprocess.run(["pkill", "-9", "-x", "cursor"], capture_output=True, check=False)
    time.sleep(0.8)
    if is_running():
        raise CursorProcessError("Could not quit Cursor")
    _wait_for_db_writable(timeout=8.0)


CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000


def spawn_detached(argv: list[str]) -> None:
    """Start a process that survives Cursor being killed (job-object breakaway)."""
    if sys.platform == "win32":
        _spawn_windows(argv)
        return
    subprocess.Popen(
        argv,
        cwd=str(Path.home()),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def _spawn_windows(argv: list[str]) -> None:
    from cuswap.paths import switch_log_path

    log = switch_log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    # Start-Process creates a process outside Cursor's job object, so the
    # helper keeps running after the IDE is killed.
    exe, *args = argv
    quoted_args = ",".join(f"'{item}'" for item in args)
    command = (
        f"Start-Process -FilePath '{exe}' -ArgumentList {quoted_args} "
        f"-WorkingDirectory '{Path.home()}' -WindowStyle Hidden"
    )
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-WindowStyle",
            "Hidden",
            "-Command",
            command,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"powershell start failed: {result.stderr or result.stdout}\n")
    log_handle = log.open("a", encoding="utf-8")
    subprocess.Popen(
        argv,
        cwd=str(Path.home()),
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=log_handle,
        creationflags=(
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | CREATE_BREAKAWAY_FROM_JOB
            | CREATE_NO_WINDOW
        ),
        close_fds=False,
    )


def start_cursor() -> None:
    exe = default_cursor_exe()
    if exe is not None:
        _launch_gui(exe)
        return
    binary = shutil.which("cursor")
    if binary:
        _launch_gui(Path(binary))
        return
    raise CursorProcessError("Could not find Cursor.exe. Set CUSWAP_CURSOR_EXE.")


def _launch_gui(exe: Path) -> None:
    # DETACHED_PROCESS makes Electron/Cursor open Chrome without a URL.
    if sys.platform == "win32":
        os.startfile(str(exe))  # noqa: S606
        return
    subprocess.Popen([str(exe)], start_new_session=True, cwd=str(Path.home()))


def _wait_for_db_writable(*, timeout: float = 8.0) -> None:
    from cuswap.cursor_db import CursorDbError, wait_until_writable

    db = state_db_path()
    if not db.is_file():
        return
    try:
        wait_until_writable(db, timeout=timeout)
    except CursorDbError:
        # Caller still attempts the write; it will raise a clearer error.
        return