"""Undo Cursor's at-rest token encryption.

Cursor rewrites ``cursorAuth/accessToken`` and ``cursorAuth/refreshToken`` in
``state.vscdb`` as ``cursor-safe-storage:v1:`` blobs (Electron safeStorage /
Chromium os_crypt). Those blobs are not session JWTs. Sending one to the
refresh endpoint comes back as ``shouldLogout``, so cuswap marked the account
``needs_reauth`` even while the IDE was still signed in with that session.
"""

from __future__ import annotations

import base64
import json
import sys

from cuswap.crypto import CryptoError, unprotect
from cuswap.models import SessionBundle
from cuswap.paths import cursor_user_data_dir

SEALED_PREFIX = "cursor-safe-storage:v1:"
AUTH_TOKEN_KEYS = ("cursorAuth/accessToken", "cursorAuth/refreshToken")

_os_key: bytes | None = None


def is_sealed(value: str | None) -> bool:
    return bool(value) and value.startswith(SEALED_PREFIX)


def unseal_token(value: str) -> str | None:
    """Return the session token. Plaintext passes through. Sealed values decrypt."""
    if not is_sealed(value):
        return value
    if sys.platform != "win32":
        return None
    try:
        raw = base64.b64decode(value[len(SEALED_PREFIX) :])
        if not raw.startswith(b"v10") or len(raw) < 3 + 12 + 16:
            return None
        plain = _aes_gcm_decrypt(_os_crypt_key(), raw[3:15], raw[15:])
        text = plain.decode("utf-8")
    except (CryptoError, OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return text or None


def apply_plaintext_tokens(bundle: SessionBundle) -> bool:
    """Replace sealed auth tokens on ``bundle``. Leave a value untouched if decrypt fails."""
    changed = False
    for key in AUTH_TOKEN_KEYS:
        value = bundle.get(key)
        if not is_sealed(value) or value is None:
            continue
        plain = unseal_token(value)
        if not plain or is_sealed(plain):
            continue
        bundle.set(key, plain)
        changed = True
    return changed


def _os_crypt_key() -> bytes:
    global _os_key
    if _os_key is not None:
        return _os_key
    path = cursor_user_data_dir() / "Local State"
    data = json.loads(path.read_text(encoding="utf-8"))
    encrypted = base64.b64decode(data["os_crypt"]["encrypted_key"])
    if encrypted.startswith(b"DPAPI"):
        encrypted = encrypted[5:]
    key = unprotect(encrypted)
    if len(key) not in {16, 24, 32}:
        raise CryptoError("Cursor os_crypt key has an unexpected length")
    _os_key = key
    return key


def _aes_gcm_decrypt(key: bytes, nonce: bytes, payload: bytes) -> bytes:
    """Decrypt Chromium ``v10`` payload (ciphertext || 16-byte tag) with Windows CNG."""
    from ctypes import (
        POINTER,
        Structure,
        WinDLL,
        byref,
        c_ubyte,
        c_ulong,
        c_ulonglong,
        c_void_p,
        cast,
        create_string_buffer,
        create_unicode_buffer,
        sizeof,
        wintypes,
    )

    class AuthInfo(Structure):
        _fields_ = [
            ("cbSize", wintypes.ULONG),
            ("dwInfoVersion", wintypes.ULONG),
            ("pbNonce", POINTER(c_ubyte)),
            ("cbNonce", wintypes.ULONG),
            ("pbAuthData", POINTER(c_ubyte)),
            ("cbAuthData", wintypes.ULONG),
            ("pbTag", POINTER(c_ubyte)),
            ("cbTag", wintypes.ULONG),
            ("pbMacContext", POINTER(c_ubyte)),
            ("cbMacContext", wintypes.ULONG),
            ("cbAAD", wintypes.ULONG),
            ("cbData", c_ulonglong),
            ("dwFlags", wintypes.ULONG),
        ]

    if len(payload) < 16:
        raise ValueError("sealed payload is too short")
    tag = payload[-16:]
    ciphertext = payload[:-16]
    bcrypt = WinDLL("bcrypt")
    h_alg = c_void_p()
    status = bcrypt.BCryptOpenAlgorithmProvider(byref(h_alg), "AES", None, 0)
    if status != 0:
        raise OSError(f"BCryptOpenAlgorithmProvider failed ({status:#x})")
    try:
        mode = create_unicode_buffer("ChainingModeGCM")
        status = bcrypt.BCryptSetProperty(h_alg, "ChainingMode", mode, sizeof(mode), 0)
        if status != 0:
            raise OSError(f"BCryptSetProperty failed ({status:#x})")
        h_key = c_void_p()
        key_buf = create_string_buffer(key, len(key))
        status = bcrypt.BCryptGenerateSymmetricKey(h_alg, byref(h_key), None, 0, key_buf, len(key), 0)
        if status != 0:
            raise OSError(f"BCryptGenerateSymmetricKey failed ({status:#x})")
        try:
            nonce_buf = (c_ubyte * len(nonce)).from_buffer_copy(nonce)
            tag_buf = (c_ubyte * len(tag)).from_buffer_copy(tag)
            ct_buf = create_string_buffer(ciphertext, len(ciphertext))
            info = AuthInfo()
            info.cbSize = sizeof(AuthInfo)
            info.dwInfoVersion = 1
            info.pbNonce = cast(nonce_buf, POINTER(c_ubyte))
            info.cbNonce = len(nonce)
            info.pbTag = cast(tag_buf, POINTER(c_ubyte))
            info.cbTag = len(tag)
            out_len = c_ulong(0)
            status = bcrypt.BCryptDecrypt(
                h_key, ct_buf, len(ciphertext), byref(info), None, 0, None, 0, byref(out_len), 0
            )
            if status != 0:
                raise OSError(f"BCryptDecrypt size failed ({status:#x})")
            out = create_string_buffer(out_len.value)
            status = bcrypt.BCryptDecrypt(
                h_key,
                ct_buf,
                len(ciphertext),
                byref(info),
                None,
                0,
                out,
                out_len.value,
                byref(out_len),
                0,
            )
            if status != 0:
                raise OSError(f"BCryptDecrypt failed ({status:#x})")
            return out.raw[: out_len.value]
        finally:
            bcrypt.BCryptDestroyKey(h_key)
    finally:
        bcrypt.BCryptCloseAlgorithmProvider(h_alg, 0)
