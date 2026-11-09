import json

from cuswap.apply_switch import helper_argv, load_pending, write_pending


def test_helper_argv_uses_python_module(monkeypatch):
    monkeypatch.setattr("cuswap.apply_switch.sys.executable", r"C:\Python\python.exe")
    monkeypatch.setattr("cuswap.apply_switch.sys.argv", [r"C:\Tools\cuswap.exe", "switch", "2"])
    argv = helper_argv()
    assert argv[0].lower().endswith("python.exe") or argv[0].lower().endswith("pythonw.exe")
    assert argv[-2:] == ["-m", "cuswap.apply_switch"]


def test_pending_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("CUSWAP_HOME", str(tmp_path))
    payload = {"email": "alt@example.com", "restart": True, "bundle": {"keys": [], "sidecars": {}}}
    path = write_pending(payload)
    assert path.is_file()
    loaded = load_pending()
    assert loaded["email"] == "alt@example.com"
    assert loaded["restart"] is True
