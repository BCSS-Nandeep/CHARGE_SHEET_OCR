"""Structured logging without PII / document content."""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

from app.config import get_settings

_PHONE = re.compile(r"\b\d{10}\b")
_AADHAAR = re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def redact(text: str) -> str:
    if not text:
        return text
    text = _EMAIL.sub("[REDACTED_EMAIL]", text)
    text = _AADHAAR.sub("[REDACTED_ID]", text)
    text = _PHONE.sub("[REDACTED_PHONE]", text)
    return text


class SafeFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        settings = get_settings()
        if not settings.log_pii and isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            try:
                if isinstance(record.args, dict):
                    record.args = {
                        k: redact(str(v)) if isinstance(v, str) else v
                        for k, v in record.args.items()
                    }
                elif isinstance(record.args, tuple):
                    record.args = tuple(
                        redact(str(a)) if isinstance(a, str) else a for a in record.args
                    )
            except Exception:
                record.args = ()
        return super().format(record)


def setup_logging() -> None:
    settings = get_settings()
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        SafeFormatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def safe_exc_message(exc: BaseException) -> str:
    """Return a short exception type/message without payload content."""
    return f"{type(exc).__name__}: {redact(str(exc)[:200])}"


def audit(event: str, **kwargs: Any) -> None:
    """Log operational events without document body content."""
    logger = get_logger("audit")
    safe = {k: v for k, v in kwargs.items() if k not in {"text", "content", "payload", "narrative"}}
    logger.info("event=%s %s", event, " ".join(f"{k}={v}" for k, v in safe.items()))
