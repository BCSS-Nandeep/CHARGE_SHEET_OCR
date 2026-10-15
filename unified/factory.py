"""Build the unified ASGI application (the gateway) from its parts. Tests pass their own settings / host / worker."""

from __future__ import annotations

from typing import Optional

from .adapters.worker import WorkerManager
from .config import UnifiedSettings, load_settings
from .coordinator import Coordinator
from .gateway import Gateway
from .registry import Registry
from .routes import build_unified_app


def create_gateway(settings: Optional[UnifiedSettings] = None, host_app=None,
                   manager: Optional[WorkerManager] = None) -> Gateway:
    settings = settings or load_settings()
    if host_app is None:
        from app.main import app as host_app  # the IR_OCR application object, unchanged
    registry = Registry(settings.registry_path)
    coordinator = Coordinator(settings, registry, host_app, manager)
    gateway = Gateway(host_app, build_unified_app(coordinator), coordinator)
    gateway.coordinator = coordinator
    return gateway
