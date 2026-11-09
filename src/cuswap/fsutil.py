"""Filesystem primitives with no cuswap dependencies.

Atomic replace is the same pattern claude-swap uses: write a sibling temp
file, then os.replace, retrying the Windows sharing/AV failures that make
a single replace fail ~half the time on a freshly written path.
"""

from __future__ import annotations

import os
import sys
import time

# ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION
_TRANSIENT_WIN_ERRORS = frozenset({5, 32, 33})


def replace_with_retry(
    src: os.PathLike | str,
    dst: os.PathLike | str,
    *,
    attempts: int = 10,
    initial_delay: float = 0.002,
) -> None:
    """``os.replace``, retried past transient Windows sharing failures."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    delay = initial_delay
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            transient = (
                sys.platform == "win32"
                and getattr(exc, "winerror", None) in _TRANSIENT_WIN_ERRORS
            )
            if not transient or attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.25)


def atomic_write_bytes(path: os.PathLike | str, data: bytes, *, mode: int | None = 0o600) -> None:
    target = os.fspath(path)
    parent = os.path.dirname(target) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = _mkstemp_in(parent)
    try:
        os.write(fd, data)
        os.close(fd)
        fd = -1
        if mode is not None and sys.platform != "win32":
            os.chmod(tmp_path, mode)
        replace_with_retry(tmp_path, target)
        if mode is not None and sys.platform != "win32":
            os.chmod(target, mode)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _mkstemp_in(parent: str) -> tuple[int, str]:
    import tempfile

    return tempfile.mkstemp(dir=parent, suffix=".tmp")