"""Outbound network restrictions for offline-first deployments."""

from __future__ import annotations

import socket
from typing import Iterable, Set

from app.config import get_settings
from app.logging_utils import get_logger

logger = get_logger(__name__)

_original_connect = socket.socket.connect
_patched = False

# Local / private targets always allowed
_LOCAL_HOSTS: Set[str] = {"127.0.0.1", "::1", "localhost"}


def _host_allowed(host: str, allow_external: bool) -> bool:
    if allow_external:
        return True
    h = (host or "").lower().strip("[]")
    if h in _LOCAL_HOSTS:
        return True
    # Link-local / private IPv4
    if h.startswith("10.") or h.startswith("192.168.") or h.startswith("172."):
        return True
    return False


def install_network_guard() -> None:
    """Monkey-patch socket.connect to block unexpected outbound connections.

    This is a best-effort safeguard for offline mode. It does not replace
    OS-level firewall rules. Database connections to private hosts remain allowed.
    """
    global _patched
    settings = get_settings()
    if not settings.offline_mode or settings.allow_external_network:
        logger.info("network_guard=disabled offline_mode=%s", settings.offline_mode)
        return
    if _patched:
        return

    def guarded_connect(self, address):  # type: ignore[no-untyped-def]
        host = address[0] if isinstance(address, tuple) else str(address)
        if not _host_allowed(str(host), False):
            logger.warning("blocked_outbound_connect host=%s", host)
            raise OSError(f"Outbound connection blocked by offline mode: {host}")
        return _original_connect(self, address)

    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    _patched = True
    logger.info("network_guard=enabled")


def uninstall_network_guard() -> None:
    global _patched
    if _patched:
        socket.socket.connect = _original_connect  # type: ignore[method-assign]
        _patched = False
