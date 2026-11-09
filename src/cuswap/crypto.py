from __future__ import annotations

import os
import sys
from pathlib import Path


class CryptoError(RuntimeError):
    pass


def protect(data: bytes) -> bytes:
    if sys.platform == "win32":
        return _dpapi_protect(data)
    return data


def unprotect(data: bytes) -> bytes:
    if sys.platform == "win32":
        return _dpapi_unprotect(data)
    return data


def write_secret_file(path: Path, data: bytes) -> None:
    from cuswap.fsutil import atomic_write_bytes

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, protect(data))


def read_secret_file(path: Path) -> bytes:
    return unprotect(path.read_bytes())


def _dpapi_protect(data: bytes) -> bytes:
    blob_in, blob_out = _blobs(data)
    if not _crypt32().CryptProtectData(
        _ctypes().byref(blob_in),
        "cuswap",
        None,
        None,
        None,
        0,
        _ctypes().byref(blob_out),
    ):
        raise CryptoError("Windows DPAPI encrypt failed")
    try:
        return _ctypes().string_at(blob_out.pbData, blob_out.cbData)
    finally:
        _kernel32().LocalFree(blob_out.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    blob_in, blob_out = _blobs(data)
    if not _crypt32().CryptUnprotectData(
        _ctypes().byref(blob_in),
        None,
        None,
        None,
        None,
        0,
        _ctypes().byref(blob_out),
    ):
        raise CryptoError("Windows DPAPI decrypt failed")
    try:
        return _ctypes().string_at(blob_out.pbData, blob_out.cbData)
    finally:
        _kernel32().LocalFree(blob_out.pbData)


def _ctypes():
    import ctypes

    return ctypes


def _crypt32():
    return _ctypes().windll.crypt32


def _kernel32():
    return _ctypes().windll.kernel32


def _blobs(data: bytes):
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    return blob_in, blob_out
