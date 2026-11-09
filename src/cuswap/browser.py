from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def open_private(url: str) -> str | None:
    """Open URL in a private window. Returns the browser used, or None."""
    if sys.platform == "win32":
        return _open_windows(url)
    if sys.platform == "darwin":
        return _open_macos(url)
    return _open_linux(url)


def _windows_roots() -> list[Path]:
    roots: list[Path] = []
    for key in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        value = os.environ.get(key)
        if value:
            roots.append(Path(value))
    home_local = Path.home() / "AppData" / "Local"
    if home_local not in roots:
        roots.append(home_local)
    return roots


def _registry_app_path(exe_name: str) -> Path | None:
    if sys.platform != "win32":
        return None
    import winreg

    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}") as key:
                value, _ = winreg.QueryValueEx(key, None)
        except OSError:
            continue
        path = Path(value)
        if path.is_file():
            return path
    return None


def _windows_browser_exes() -> list[tuple[str, Path, list[str]]]:
    roots = _windows_roots()
    relative = [
        ("chrome", Path("Google/Chrome/Application/chrome.exe"), ["--incognito"]),
        ("edge", Path("Microsoft/Edge/Application/msedge.exe"), ["--inprivate"]),
        ("brave", Path("BraveSoftware/Brave-Browser/Application/brave.exe"), ["--incognito"]),
        ("coccoc", Path("CocCoc/Browser/Application/browser.exe"), ["--incognito"]),
    ]
    found: list[tuple[str, Path, list[str]]] = []
    seen: set[Path] = set()

    def add(name: str, path: Path, flags: list[str]) -> None:
        resolved = path.resolve() if path.is_file() else path
        if not path.is_file() or resolved in seen:
            return
        seen.add(resolved)
        found.append((name, path, flags))

    for name, rel, flags in relative:
        for root in roots:
            add(name, root / rel, flags)
    for name, exe, flags in (
        ("chrome", "chrome.exe", ["--incognito"]),
        ("edge", "msedge.exe", ["--inprivate"]),
        ("firefox", "firefox.exe", ["-private-window"]),
        ("brave", "brave.exe", ["--incognito"]),
    ):
        which = shutil.which(exe)
        if which:
            add(name, Path(which), flags)
        registered = _registry_app_path(exe)
        if registered:
            add(name, registered, flags)
    return found


def _open_windows(url: str) -> str | None:
    for name, path, flags in _windows_browser_exes():
        try:
            subprocess.Popen([str(path), *flags, url], close_fds=False)
        except OSError:
            continue
        return name
    return None


def _open_macos(url: str) -> str | None:
    apps = [
        ("chrome", "Google Chrome", ["--args", "--incognito", url]),
        ("edge", "Microsoft Edge", ["--args", "--inprivate", url]),
        ("brave", "Brave Browser", ["--args", "--incognito", url]),
    ]
    for name, app, extra in apps:
        app_path = Path("/Applications") / f"{app}.app"
        if app_path.is_dir():
            subprocess.Popen(["open", "-na", app, *extra])
            return name
    return None


def _open_linux(url: str) -> str | None:
    for name, args in (
        ("google-chrome", ["--incognito", url]),
        ("google-chrome-stable", ["--incognito", url]),
        ("chromium", ["--incognito", url]),
        ("microsoft-edge", ["--inprivate", url]),
        ("brave-browser", ["--incognito", url]),
        ("firefox", ["-private-window", url]),
    ):
        binary = shutil.which(name)
        if binary:
            subprocess.Popen([binary, *args], start_new_session=True)
            return name
    return None
