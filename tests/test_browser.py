import sys

from cuswap.browser import _windows_browser_exes


def test_windows_finds_chrome_or_edge():
    if sys.platform != "win32":
        return
    found = _windows_browser_exes()
    names = {name for name, path, _flags in found}
    assert names & {"chrome", "edge"}
    assert any(path.is_file() for _name, path, _flags in found)
