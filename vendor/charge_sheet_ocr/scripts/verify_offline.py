"""Verify offline / security configuration (no document content is touched).

Exit code 0 = all checks passed. This is a configuration check, not proof of
zero data leakage — see docs/SECURITY.md for what it cannot detect.
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    from app.config import get_settings
    from app.security.auth import auth_misconfigured, key_table
    from app.security.network_guard import install_network_guard, uninstall_network_guard
    import app.services.ocr_service as ocr  # noqa: F401  (sets the PaddleX env flag on import)

    settings = get_settings()
    ok = True

    def check(cond: bool, good: str, bad: str, *, warn_only: bool = False) -> None:
        nonlocal ok
        if cond:
            print("OK:  ", good)
        else:
            print("WARN:" if warn_only else "FAIL:", bad)
            if not warn_only:
                ok = False

    check(settings.offline_mode, "OFFLINE_MODE=true", "OFFLINE_MODE should be true for sensitive deployments")
    check(not settings.allow_external_network, "ALLOW_EXTERNAL_NETWORK=false", "ALLOW_EXTERNAL_NETWORK must be false")
    check(not settings.llm_allow_external, "LLM_ALLOW_EXTERNAL=false", "LLM_ALLOW_EXTERNAL must be false")
    check(not settings.llm_enabled or settings.llm_is_local_only(), "no external LLM endpoint", "LLM endpoint is not local")
    check(
        os.environ.get("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK") == "True",
        "PaddleX model-hoster connectivity probe disabled",
        "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK is not set",
    )
    check(not settings.log_pii, "LOG_PII=false (phone/ID/e-mail redaction on)", "LOG_PII=true writes personal data to logs")
    if settings.enable_auth:
        check(not auth_misconfigured(settings), f"auth enabled with {len(key_table(settings))} usable key(s)",
              "ENABLE_AUTH=true but no usable API key (>=16 chars, not the example value)")
    else:
        check(False, "", "ENABLE_AUTH=false: only loopback clients are served and results cannot be verified",
              warn_only=True)
    check(not settings.api_docs_enabled, "API docs disabled",
          "API_DOCS_ENABLED=true: opening /docs makes the browser fetch Swagger UI from cdn.jsdelivr.net",
          warn_only=True)

    install_network_guard()
    try:
        for method in ("connect", "connect_ex"):
            s = socket.socket()
            try:
                s.settimeout(1)
                getattr(s, method)(("93.184.216.34", 80))
                check(False, "", f"outbound {method} to a public address was allowed")
            except OSError as exc:
                check("blocked" in str(exc), f"outbound {method} blocked by the network guard",
                      f"outbound {method} failed for another reason: {type(exc).__name__}")
            finally:
                s.close()
    finally:
        uninstall_network_guard()

    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
