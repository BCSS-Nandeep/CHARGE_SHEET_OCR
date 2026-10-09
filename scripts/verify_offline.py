"""Verify offline / security configuration (no document content transmitted)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    from app.config import get_settings
    from app.security.network_guard import install_network_guard, uninstall_network_guard

    settings = get_settings()
    print("offline_mode=", settings.offline_mode)
    print("allow_external_network=", settings.allow_external_network)
    print("llm_enabled=", settings.llm_enabled)
    print("llm_allow_external=", settings.llm_allow_external)
    print("llm_is_local_only=", settings.llm_is_local_only())
    print("enable_auth=", settings.enable_auth)
    print("log_pii=", settings.log_pii)

    ok = True
    if not settings.offline_mode:
        print("FAIL: OFFLINE_MODE should be true for sensitive deployments")
        ok = False
    if settings.allow_external_network:
        print("FAIL: ALLOW_EXTERNAL_NETWORK should be false")
        ok = False
    if settings.llm_enabled and not settings.llm_is_local_only():
        print("FAIL: LLM is enabled with a non-local endpoint")
        ok = False
    if settings.llm_allow_external:
        print("FAIL: LLM_ALLOW_EXTERNAL must be false")
        ok = False

    install_network_guard()
    try:
        import socket

        s = socket.socket()
        try:
            s.settimeout(1)
            s.connect(("example.com", 80))
            print("FAIL: outbound connect to example.com was allowed")
            ok = False
        except OSError as exc:
            print("OK: outbound blocked:", type(exc).__name__)
        finally:
            s.close()
    finally:
        uninstall_network_guard()

    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
