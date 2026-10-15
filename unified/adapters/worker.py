"""Starts and supervises the charge-sheet worker: the unmodified CHARGE_SHEET_OCR snapshot as its own process.

Why a process: CHARGE_SHEET_OCR and IR_OCR both are Python packages called ``app`` whose modules differ
(``field_builder``, ``field_synonyms``, ``form_parser``, ``text_normalize``, ``pdf_service`` ...). They cannot be imported
into one interpreter, and renaming imports would mean editing OCR source. As a process the snapshot runs byte for byte
as shipped, with its own interpreter, database and upload folder.

The worker is started through ``unified/worker_entry/cs_worker_entry.py`` which imports the snapshot's ``app.main:app``
unchanged and only adds one read-only classification route (``/internal/probe``).
"""

from __future__ import annotations

import atexit
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

import httpx

from ..config import UnifiedSettings
from .base import AdapterUnavailable

ENTRY_DIR = Path(__file__).resolve().parent.parent / "worker_entry"
ENTRY_MODULE = "cs_worker_entry:app"
READY_PATH = "/static/css/styles.css"   # served by the snapshot without touching its database or the model server
READY_CACHE_SECONDS = 10.0


class WorkerManager:
    def __init__(self, settings: UnifiedSettings) -> None:
        self.settings = settings
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._internal_key = secrets.token_urlsafe(24)
        self._ready_until = 0.0
        self._log = None
        atexit.register(self.stop)

    # ------------------------------------------------------------------ access
    @property
    def url(self) -> str:
        return self.settings.worker_url

    def _host_settings(self):
        from app.config import get_settings

        return get_settings()

    def internal_headers(self) -> Dict[str, str]:
        """Credentials for the layer's own background calls (status polling). None needed when auth is off."""
        host = self._host_settings()
        if not host.enable_auth:
            return {}
        key = self.settings.worker_api_key or (self._internal_key if self.settings.worker_manage else "")
        return {host.api_key_header: key} if key else {}

    def healthy(self, timeout: float = 3.0) -> bool:
        """Is the worker answering? Uses a static file: the worker's ``/health`` also probes the shared vision-model
        server, and this is asked on every forwarded request."""
        try:
            ok = httpx.get(f"{self.url}{READY_PATH}", timeout=timeout).status_code == 200
        except httpx.HTTPError:
            ok = False
        self._ready_until = time.monotonic() + READY_CACHE_SECONDS if ok else 0.0
        return ok

    def _recently_ready(self) -> bool:
        alive = self._proc is None or self._proc.poll() is None
        return alive and time.monotonic() < self._ready_until

    # --------------------------------------------------------------- lifecycle
    def worker_env(self) -> Dict[str, str]:
        host = self._host_settings()
        data = self.settings.worker_data_dir
        keys = host.api_keys or ""
        if host.enable_auth:  # the layer's own read-only key, only ever sent to the loopback worker
            keys = ";".join(k for k in (keys, f"unified-internal:{self._internal_key}:viewer") if k)
        env = {k: v for k, v in os.environ.items() if k.upper() not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
        env.update({
            "PYTHONPATH": str(ENTRY_DIR),
            "PYTHONUTF8": "1",
            "UNIFIED_PARENT_PID": str(os.getpid()),  # the worker exits when this process does
            "APP_HOST": "127.0.0.1",
            "APP_PORT": str(urlparse(self.url).port or 8101),
            "DATABASE_URL": "sqlite:///" + (data / "charge_sheets.db").as_posix(),
            "UPLOAD_DIR": str(data / "uploads"),
            "ENABLE_AUTH": "true" if host.enable_auth else "false",
            "API_KEY": host.api_key if host.enable_auth else "",
            "API_KEYS": keys if host.enable_auth else "",
            "API_KEY_HEADER": host.api_key_header,
        })
        return env

    def ensure_running(self) -> None:
        """Make sure a healthy worker answers at ``worker_url`` (starting the snapshot when we manage it)."""
        if self._recently_ready() or self.healthy():
            return
        if not self.settings.worker_manage:
            raise AdapterUnavailable(f"Charge-sheet worker is not reachable at {self.url} and CS_WORKER_MANAGE is off")
        with self._lock:
            if self.healthy():
                return
            self._start()
            deadline = time.monotonic() + self.settings.worker_startup_timeout
            while time.monotonic() < deadline:
                if self._proc is not None and self._proc.poll() is not None:
                    raise AdapterUnavailable(
                        f"Charge-sheet worker exited during start-up (code {self._proc.returncode}); "
                        f"see {self.settings.worker_data_dir / 'worker.log'}")
                if self.healthy():
                    return
                time.sleep(0.5)
            raise AdapterUnavailable("Charge-sheet worker did not become ready in time")

    def _start(self) -> None:
        python = self.settings.worker_python
        if not Path(python).is_file() and python != sys.executable:
            raise AdapterUnavailable(f"CS_WORKER_PYTHON does not exist: {python}")
        if not (self.settings.worker_dir / "app" / "main.py").is_file():
            raise AdapterUnavailable(f"Charge-sheet snapshot not found at {self.settings.worker_dir}")
        data = self.settings.worker_data_dir
        (data / "uploads").mkdir(parents=True, exist_ok=True)
        self._log = open(data / "worker.log", "ab")
        port = str(urlparse(self.url).port or 8101)
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        self._proc = subprocess.Popen(
            [python, "-m", "uvicorn", ENTRY_MODULE, "--host", "127.0.0.1", "--port", port],
            cwd=str(self.settings.worker_dir), env=self.worker_env(), stdout=self._log, stderr=subprocess.STDOUT,
            creationflags=flags,
        )

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None
