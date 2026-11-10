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

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WM_CLOSE = 0x0010
SMTO_ABORTIFHUNG = 0x0002


def _hidden_kwargs() -> dict:
    """Prevent console apps (taskkill/tasklist) from flashing a CMD window."""
    if sys.platform != "win32":
        return {}
    return {"creationflags": CREATE_NO_WINDOW}


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
    try:
        return _win_toolhelp_pids("cursor.exe")
    except OSError:
        return _win_tasklist_pids()


def _win_toolhelp_pids(image_name: str) -> list[int]:
    """List PIDs via Toolhelp — no console window."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    CreateToolhelp32Snapshot = kernel32.CreateToolhelp32Snapshot
    CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    CreateToolhelp32Snapshot.restype = wintypes.HANDLE

    Process32FirstW = kernel32.Process32FirstW
    Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    Process32FirstW.restype = wintypes.BOOL

    Process32NextW = kernel32.Process32NextW
    Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    Process32NextW.restype = wintypes.BOOL

    invalid = ctypes.c_void_p(-1).value
    snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == invalid:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")

    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    pids: list[int] = []
    try:
        more = Process32FirstW(snapshot, ctypes.byref(entry))
        target = image_name.lower()
        while more:
            if entry.szExeFile.lower() == target:
                pids.append(int(entry.th32ProcessID))
            more = Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return pids


def _win_tasklist_pids() -> list[int]:
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Cursor.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=False,
        **_hidden_kwargs(),
    )
    pids: list[int] = []
    for line in (result.stdout or "").splitlines():
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
        _win_soft_close_cursor()
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
        _win_force_kill_cursor()
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


def _win_soft_close_cursor() -> None:
    """Ask Cursor windows to close via WM_CLOSE — no taskkill console."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)

    GetWindowThreadProcessId = user32.GetWindowThreadProcessId
    GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    GetWindowThreadProcessId.restype = wintypes.DWORD

    IsWindowVisible = user32.IsWindowVisible
    IsWindowVisible.argtypes = [wintypes.HWND]
    IsWindowVisible.restype = wintypes.BOOL

    SendMessageTimeoutW = user32.SendMessageTimeoutW
    SendMessageTimeoutW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    SendMessageTimeoutW.restype = ctypes.c_size_t

    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    EnumWindows = user32.EnumWindows
    EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
    EnumWindows.restype = wintypes.BOOL

    target_pids = set(cursor_pids())
    if not target_pids:
        return

    def _callback(hwnd: int, _lparam: int) -> bool:
        pid = wintypes.DWORD()
        GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in target_pids and IsWindowVisible(hwnd):
            result = ctypes.c_size_t()
            SendMessageTimeoutW(
                hwnd,
                WM_CLOSE,
                0,
                0,
                SMTO_ABORTIFHUNG,
                500,
                ctypes.byref(result),
            )
        return True

    EnumWindows(EnumWindowsProc(_callback), 0)
    # Headless / tray processes: soft signal without a visible console.
    subprocess.run(
        ["taskkill", "/IM", "Cursor.exe"],
        capture_output=True,
        check=False,
        **_hidden_kwargs(),
    )


def _win_force_kill_cursor() -> None:
    """Terminate remaining Cursor.exe PIDs via Win32 — no CMD flash."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    OpenProcess = kernel32.OpenProcess
    OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    OpenProcess.restype = wintypes.HANDLE
    TerminateProcess = kernel32.TerminateProcess
    TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    TerminateProcess.restype = wintypes.BOOL

    for pid in cursor_pids():
        handle = OpenProcess(PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            handle = OpenProcess(PROCESS_TERMINATE, False, pid)
        if not handle:
            continue
        try:
            TerminateProcess(handle, 1)
        finally:
            kernel32.CloseHandle(handle)

    # Fallback for any leftover (still hidden).
    if cursor_pids():
        subprocess.run(
            ["taskkill", "/F", "/IM", "Cursor.exe"],
            capture_output=True,
            check=False,
            **_hidden_kwargs(),
        )


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
