import sys

from cuswap.crypto import protect, read_secret_file, unprotect, write_secret_file


def test_secret_file_roundtrip(tmp_path):
    path = tmp_path / "cred.bin"
    payload = b'{"keys":[{"key":"cursorAuth/accessToken","value":"abc"}]}'
    write_secret_file(path, payload)
    assert path.is_file()
    assert read_secret_file(path) == payload


def test_protect_unprotect_bytes():
    raw = b"hello-cuswap"
    encoded = protect(raw)
    assert unprotect(encoded) == raw
    if sys.platform == "win32":
        assert encoded != raw
