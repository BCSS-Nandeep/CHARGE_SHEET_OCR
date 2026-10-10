"""Outbound network restrictions for offline-first deployments.

Best-effort, in-process only: it patches ``socket.connect``/``connect_ex`` so
library code (OCR model downloaders, telemetry, HTTP clients) cannot open
connections to public addresses. It does not see native code that bypasses
Python sockets and does not replace OS firewall rules.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Set

from app.config import get_settings
from app.logging_utils import get_logger

logger = get_logger(__name__)

_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
_patched = False


def _allowlist() -> Set[str]:
    raw = getattr(get_settings(), "network_allowlist", "") or ""
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _host_allowed(host: str, allow_external: bool) -> bool:
    if allow_external:
        return True
    h = (host or "").lower().strip("[]")
    if h == "localhost" or h in _allowlist():
        return True
    try:
        ip = ipaddress.ip_address(h.split("%", 1)[0])
    except ValueError:
        # Other hostnames are not resolved here; allow them explicitly via NETWORK_ALLOWLIST
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def install_network_guard() -> None:
    """Block outbound connections to non-local addresses when OFFLINE_MODE is on."""
    global _patched
    settings = get_settings()
    if not settings.offline_mode or settings.allow_external_network:
        logger.info("network_guard=disabled offline_mode=%s", settings.offline_mode)
        return
    if _patched:
        return

    def _check(address) -> None:
        if not isinstance(address, tuple):  # AF_UNIX paths are local
            return
        host = str(address[0])
        if not _host_allowed(host, False):
            logger.warning("blocked_outbound_connect host=%s", host)
            raise OSError(f"Outbound connection blocked by offline mode: {host}")

    def guarded_connect(self, address):  # type: ignore[no-untyped-def]
        _check(address)
        return _original_connect(self, address)

    def guarded_connect_ex(self, address):  # type: ignore[no-untyped-def]
        _check(address)
        return _original_connect_ex(self, address)

    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]
    _patched = True
    logger.info("network_guard=enabled")


def uninstall_network_guard() -> None:
    global _patched
    if _patched:
        socket.socket.connect = _original_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = _original_connect_ex  # type: ignore[method-assign]
        _patched = False
