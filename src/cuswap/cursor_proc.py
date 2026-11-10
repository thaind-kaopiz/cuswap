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


CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def cursor_pids() -> list[int]:
    if sys.platform == "win32":
        return _win_cursor_pids()
    result = subprocess.run(
        ["pgrep", "-x", "Cursor"],
        capture_output=True,
        text=True,
        check=False,
    )
    pids: list[int] = []
    if result.returncode == 0:
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
    result = subprocess.run(
        ["pgrep", "-x", "cursor"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
    return sorted(set(pids))


def _win_cursor_pids() -> list[int]:
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Cursor.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=False,
    )
    pids: list[int] = []
    for line in (result.stdout or "").splitlines():
        # "Cursor.exe","1234","Session Name","Session#","Mem Usage"
        parts = [part.strip().strip('"') for part in line.split(",")]
        if len(parts) >= 2 and parts[0].lower() == "cursor.exe" and parts[1].isdigit():
            pids.append(int(parts[1]))
    return pids


def is_running() -> bool:
    return bool(cursor_pids())


def quit_cursor(*, timeout: float = 12.0, force: bool = True) -> None:
    """Close every Cursor process. Soft close alone only drops one window."""
    if not is_running():
        _wait_for_db_writable(timeout=2.0)
        return

    if sys.platform == "win32":
        # Soft close first so windows can flush.
        subprocess.run(["taskkill", "/IM", "Cursor.exe"], capture_output=True, check=False)
    else:
        subprocess.run(["pkill", "-x", "Cursor"], capture_output=True, check=False)
        subprocess.run(["pkill", "-x", "cursor"], capture_output=True, check=False)

    soft_deadline = time.time() + min(2.5, timeout / 2)
    while time.time() < soft_deadline:
        if not is_running():
            _wait_for_db_writable(timeout=3.0)
            return
        time.sleep(0.2)

    if not force:
        raise CursorProcessError("Cursor is still running")

    if sys.platform == "win32":
        # Force every remaining PID. Do not use /T — that can kill the
        # pythonw helper if it is still loosely attached to a job.
        for pid in cursor_pids():
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True,
                check=False,
            )
        subprocess.run(["taskkill", "/F", "/IM", "Cursor.exe"], capture_output=True, check=False)
    else:
        subprocess.run(["pkill", "-9", "-x", "Cursor"], capture_output=True, check=False)
        subprocess.run(["pkill", "-9", "-x", "cursor"], capture_output=True, check=False)

    deadline = time.time() + max(3.0, timeout - 2.5)
    while time.time() < deadline:
        if not is_running():
            _wait_for_db_writable(timeout=3.0)
            return
        time.sleep(0.2)

    if is_running():
        raise CursorProcessError(
            f"Could not quit Cursor (still running: {cursor_pids()})"
        )
    _wait_for_db_writable(timeout=3.0)


def spawn_detached(argv: list[str]) -> None:
    """Start a process that survives Cursor being killed."""
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
    """Launch without a console window (no flashing CMD)."""
    from cuswap.paths import switch_log_path

    log = switch_log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        _create_process_no_window(argv)
        return
    except OSError as exc:
        with log.open("a", encoding="utf-8") as handle:
            handle.write(f"CreateProcess failed: {exc}\n")

    # Fallback: DETACHED pythonw — still no PowerShell console flash.
    log_handle = log.open("a", encoding="utf-8")
    subprocess.Popen(
        argv,
        cwd=str(Path.home()),
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=log_handle,
        creationflags=(
            DETACHED_PROCESS
            | CREATE_NEW_PROCESS_GROUP
            | CREATE_BREAKAWAY_FROM_JOB
            | CREATE_NO_WINDOW
        ),
        close_fds=False,
    )


def _create_process_no_window(argv: list[str]) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class STARTUPINFO(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    command = subprocess.list2cmdline(argv)
    startup = STARTUPINFO()
    startup.cb = ctypes.sizeof(STARTUPINFO)
    process_info = PROCESS_INFORMATION()
    flags = (
        DETACHED_PROCESS
        | CREATE_NEW_PROCESS_GROUP
        | CREATE_BREAKAWAY_FROM_JOB
        | CREATE_NO_WINDOW
    )
    created = kernel32.CreateProcessW(
        None,
        ctypes.c_wchar_p(command),
        None,
        None,
        False,
        flags,
        None,
        str(Path.home()),
        ctypes.byref(startup),
        ctypes.byref(process_info),
    )
    if not created:
        raise OSError(ctypes.get_last_error(), "CreateProcessW failed")
    kernel32.CloseHandle(process_info.hThread)
    kernel32.CloseHandle(process_info.hProcess)


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
    if sys.platform == "win32":
        os.startfile(str(exe))  # noqa: S606
        return
    subprocess.Popen([str(exe)], start_new_session=True, cwd=str(Path.home()))


def _wait_for_db_writable(*, timeout: float = 3.0) -> None:
    from cuswap.cursor_db import CursorDbError, wait_until_writable

    db = state_db_path()
    if not db.is_file():
        return
    try:
        wait_until_writable(db, timeout=timeout)
    except CursorDbError:
        return
