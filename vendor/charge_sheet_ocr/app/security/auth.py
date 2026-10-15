"""API authentication and role-based authorization (API keys).

Keys come from the environment:
  API_KEY=<secret>                         → user "default", role admin
  API_KEYS=alice:<secret>:reviewer;ops:<secret>:operator

Roles (each includes the ones before it):
  viewer   — read documents and results
  operator — upload, reprocess, submit corrections
  reviewer — mark results verified
  admin    — delete documents

With ENABLE_AUTH=false the service only answers loopback clients and treats
them as an unauthenticated operator: they cannot verify results, because
verification must be attributable to an authorised person.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Callable, Dict, Tuple

from fastapi import Depends, HTTPException, Request, status

from app.config import Settings, get_settings

ROLE_ORDER = {"viewer": 0, "operator": 1, "reviewer": 2, "admin": 3}
PLACEHOLDER_KEYS = {"change-me-to-a-long-random-secret", "dev-local-api-key-change-me", ""}
MIN_KEY_LENGTH = 16
_LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


@dataclass(frozen=True)
class Principal:
    name: str
    role: str
    authenticated: bool

    def has(self, role: str) -> bool:
        return ROLE_ORDER.get(self.role, -1) >= ROLE_ORDER[role]


def _usable(key: str) -> bool:
    return key not in PLACEHOLDER_KEYS and len(key) >= MIN_KEY_LENGTH


def key_table(settings: Settings) -> Dict[str, Tuple[str, str]]:
    """secret → (user name, role); weak or placeholder secrets are never accepted."""
    table: Dict[str, Tuple[str, str]] = {}
    for entry in (settings.api_keys or "").split(";"):
        parts = [p.strip() for p in entry.split(":")]
        if len(parts) != 3:
            continue
        name, key, role = parts
        if name and role in ROLE_ORDER and _usable(key):
            table[key] = (name, role)
    if _usable(settings.api_key or ""):
        table.setdefault(settings.api_key, ("default", "admin"))
    return table


def auth_misconfigured(settings: Settings) -> bool:
    return settings.enable_auth and not key_table(settings)


def get_principal(request: Request, settings: Settings = Depends(get_settings)) -> Principal:
    if not settings.enable_auth:
        host = request.client.host if request.client else ""
        if host not in _LOOPBACK:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Authentication is disabled, so only local (loopback) requests are accepted. "
                "Set ENABLE_AUTH=true and API_KEY/API_KEYS to serve other clients.",
            )
        return Principal(name="local-unauthenticated", role="operator", authenticated=False)

    keys = key_table(settings)
    if not keys:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"API authentication is misconfigured: set API_KEY or API_KEYS (at least {MIN_KEY_LENGTH} "
            "characters, not the example value)",
        )
    provided = request.headers.get(settings.api_key_header) or ""
    for key, (name, role) in keys.items():
        if provided and secrets.compare_digest(provided.encode(), key.encode()):
            return Principal(name=name, role=role, authenticated=True)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing API key",
        headers={"WWW-Authenticate": "ApiKey"},
    )


def require_role(role: str) -> Callable[..., Principal]:
    def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if not principal.has(role):
            if not principal.authenticated:
                detail = f"This action requires an authenticated '{role}' (enable ENABLE_AUTH and use a {role} key)"
            else:
                detail = f"This action requires the '{role}' role; '{principal.name}' has '{principal.role}'"
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
        return principal

    return dependency


# Backwards-compatible name used by existing routes: any valid caller
require_api_key = require_role("viewer")
