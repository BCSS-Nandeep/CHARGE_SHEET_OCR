"""Attach the person-image integration to an existing FastAPI application (without editing it)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import service
from .hooks import STATIC_DIR, STATIC_URL, PersonImagesMiddleware
from .routes import router


def attach(app: FastAPI) -> FastAPI:
    """Add the routes, the static assets and the response hooks. Idempotent."""
    if getattr(app.state, "person_images_attached", False):
        return app
    app.include_router(router)
    app.mount(STATIC_URL, StaticFiles(directory=str(STATIC_DIR)), name="person_images_static")
    app.add_middleware(PersonImagesMiddleware)
    service.recover_interrupted()
    app.state.person_images_attached = True
    return app
