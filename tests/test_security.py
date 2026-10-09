from pathlib import Path

import pytest

from app.logging_utils import redact
from app.security.files import resolve_under, safe_store_filename


def test_safe_filename_no_traversal():
    name = safe_store_filename("../../etc/passwd.pdf")
    assert ".." not in name
    assert name.endswith(".pdf")
    assert not name.startswith("/")


def test_resolve_under_blocks_escape(tmp_path: Path):
    from fastapi import HTTPException

    # resolve_under uses Path.name so ../ is stripped — result stays under root
    p = resolve_under(tmp_path, "../secret.pdf")
    assert tmp_path.resolve() in p.parents or p.parent == tmp_path.resolve()


def test_redact_pii():
    text = "Call 9876543210 or id 1234 5678 9012 mail a@b.com"
    out = redact(text)
    assert "9876543210" not in out
    assert "a@b.com" not in out
    assert "REDACTED" in out


def test_network_guard_blocks_external(monkeypatch):
    monkeypatch.setenv("OFFLINE_MODE", "true")
    monkeypatch.setenv("ALLOW_EXTERNAL_NETWORK", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.security.network_guard import install_network_guard, uninstall_network_guard
    import socket

    install_network_guard()
    try:
        s = socket.socket()
        with pytest.raises(OSError, match="blocked"):
            s.connect(("example.com", 80))
    finally:
        uninstall_network_guard()
        get_settings.cache_clear()
