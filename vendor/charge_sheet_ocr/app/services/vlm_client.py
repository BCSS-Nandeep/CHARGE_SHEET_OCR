"""Client for the local vision-language model (OpenAI-compatible chat API).

Built for a llama.cpp server that serves ONE request at a time and may be shared
with other applications:

* every request goes through one process-wide lock, so this service never has two
  requests in flight;
* only timeouts, connection errors and 5xx answers are retried (a few times, with a
  pause) — permanent errors (bad request, bad response) are not;
* nothing silently switches to another model or engine: a failure is raised as a
  typed error for the caller to report;
* neither the API key nor document content (prompts, page text, replies) is logged
  or put into error messages.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import httpx

from app.config import Settings, get_settings
from app.logging_utils import get_logger, redact
from app.security.network_guard import _host_allowed

logger = get_logger(__name__)

# One request at a time per process (the server has a single slot).
_request_lock = threading.Lock()

# The model's own transcription prompt (from its model card): plain text, HTML tables.
TRANSCRIBE_PROMPT = (
    "Extract the text from the above document as if you were reading it naturally. "
    "Return the tables in html format. Return the equations in LaTeX representation. "
    "If there is an image in the document and image caption is not present, add a small description "
    "of the image inside the <img></img> tag; otherwise, add the image caption inside <img></img>. "
    "Watermarks should be wrapped in brackets. Ex: <watermark>OFFICIAL COPY</watermark>. "
    "Page numbers should be wrapped in brackets. Ex: <page_number>14</page_number> or <page_number>9/22</page_number>. "
    "Prefer using ☐ and ☑ for check boxes."
)


class VlmError(Exception):
    """Base class. ``code`` is stable and safe to show; ``retryable`` drives the retry loop."""

    code = "vlm_error"
    retryable = False

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class VlmUnavailable(VlmError):
    """Not configured, not reachable, or the model is not served."""

    code = "vlm_unavailable"
    retryable = True


class VlmTimeout(VlmError):
    code = "vlm_timeout"
    retryable = True


class VlmServerError(VlmError):
    code = "vlm_server_error"
    retryable = True


class VlmRequestError(VlmError):
    """The server refused the request (4xx): retrying the same request cannot help."""

    code = "vlm_request_rejected"


class VlmBadResponse(VlmError):
    """Reply was not usable (not JSON, empty, wrong shape, cut off)."""

    code = "vlm_bad_response"


@dataclass
class VlmReply:
    text: str
    finish_reason: str = ""
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    seconds: float = 0.0

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"


def png_data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


class VlmClient:
    def __init__(self, settings: Optional[Settings] = None, transport: Optional[httpx.BaseTransport] = None):
        self.settings = settings or get_settings()
        self._transport = transport  # tests inject httpx.MockTransport

    # ------------------------------------------------------------------ config

    @property
    def base_url(self) -> str:
        return (self.settings.vlm_base_url or "").rstrip("/")

    @property
    def model(self) -> str:
        return self.settings.vlm_model

    def config_problem(self) -> Optional[str]:
        """Why the client cannot be used at all (None = configured)."""
        s = self.settings
        if not s.vlm_enabled:
            return "VLM_ENABLED=false"
        if not self.base_url:
            return "VLM_BASE_URL is not set"
        if not self.model:
            return "VLM_MODEL is not set"
        parts = urlsplit(self.base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return "VLM_BASE_URL is not a valid http(s) URL"
        allow_external = s.allow_external_network or not s.offline_mode
        if not _host_allowed(parts.hostname, allow_external):
            return (
                f"VLM_BASE_URL host '{parts.hostname}' is blocked by offline mode "
                "(use a local address or add the host to NETWORK_ALLOWLIST)"
            )
        return None

    def _client(self, read_timeout: Optional[float] = None) -> httpx.Client:
        s = self.settings
        headers = {"Content-Type": "application/json"}
        if s.vlm_api_key:
            headers["Authorization"] = f"Bearer {s.vlm_api_key}"
        timeout = httpx.Timeout(
            connect=float(s.vlm_connect_timeout_seconds),
            read=float(read_timeout if read_timeout is not None else s.vlm_timeout_seconds),
            write=60.0,
            pool=float(s.vlm_connect_timeout_seconds),
        )
        return httpx.Client(timeout=timeout, headers=headers, transport=self._transport)

    # ------------------------------------------------------------------ health

    def health(self) -> Dict[str, Any]:
        """Is the server reachable, is the configured model served, does it take images.

        Read-only (GET /models); sends no inference request.
        """
        problem = self.config_problem()
        out: Dict[str, Any] = {
            "enabled": self.settings.vlm_enabled,
            "configured": problem is None,
            "reachable": False,
            "model": self.model or None,
            "model_available": False,
            "supports_images": None,
            "error": problem,
        }
        if problem:
            return out
        try:
            with self._client(read_timeout=10) as c:
                res = c.get(f"{self.base_url}/models")
            if res.status_code != 200:
                out["reachable"] = True
                out["error"] = f"model list returned HTTP {res.status_code}"
                return out
            out["reachable"] = True
            body = res.json()
            models = body.get("data") if isinstance(body, dict) else None
            for m in models or []:
                ids = {m.get("id")} | set(m.get("aliases") or [])
                if self.model in ids:
                    out["model_available"] = True
                    mods = ((m.get("architecture") or {}).get("input_modalities")) or []
                    out["supports_images"] = ("image" in mods) if mods else None
                    break
            if not out["model_available"]:
                out["error"] = f"model '{self.model}' is not served by the configured server"
        except httpx.HTTPError as exc:
            out["error"] = f"server not reachable ({type(exc).__name__})"
        except ValueError:
            out["error"] = "model list was not valid JSON"
        return out

    def server_busy(self) -> Optional[bool]:
        """Read-only: is the server's slot processing a request? None when it cannot tell."""
        root = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
        try:
            with self._client(read_timeout=5) as c:
                res = c.get(f"{root}/slots")
            if res.status_code != 200:
                return None
            slots = res.json()
            if isinstance(slots, list) and slots:
                return any(bool(s.get("is_processing")) for s in slots if isinstance(s, dict))
        except (httpx.HTTPError, ValueError):
            pass
        return None

    def _wait_idle(self) -> None:
        budget = self.settings.vlm_wait_idle_seconds
        if budget <= 0:
            return
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            if self.server_busy() is not True:
                return
            time.sleep(2.0)
        logger.info("vlm_server_still_busy after=%ss; sending anyway (the server queues requests)", budget)

    # ------------------------------------------------------------------ requests

    def chat(
        self,
        messages: List[dict],
        *,
        max_tokens: int,
        response_format: Optional[dict] = None,
        temperature: float = 0.0,
        purpose: str = "chat",
    ) -> VlmReply:
        """One chat completion, retried only for transient failures."""
        problem = self.config_problem()
        if problem:
            raise VlmUnavailable(problem)
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            body["response_format"] = response_format
        attempts = 1 + max(0, self.settings.vlm_max_retries)
        last: Optional[VlmError] = None
        for attempt in range(1, attempts + 1):
            try:
                with _request_lock:
                    self._wait_idle()
                    return self._post(body, purpose)
            except VlmError as exc:
                last = exc
                logger.warning("vlm_request_failed purpose=%s attempt=%d/%d code=%s", purpose, attempt, attempts, exc.code)
                if not exc.retryable or attempt == attempts:
                    break
                time.sleep(self.settings.vlm_retry_backoff_seconds * attempt)
        assert last is not None
        raise last

    def _post(self, body: dict, purpose: str) -> VlmReply:
        started = time.monotonic()
        try:
            with self._client() as c:
                res = c.post(f"{self.base_url}/chat/completions", json=body)
        except httpx.TimeoutException as exc:
            kind = "connecting to" if isinstance(exc, httpx.ConnectTimeout) else "waiting for"
            raise VlmTimeout(
                f"timed out {kind} the model server after {time.monotonic() - started:.0f}s (VLM_TIMEOUT_SECONDS)"
            ) from None
        except httpx.HTTPError as exc:
            raise VlmUnavailable(f"model server not reachable ({type(exc).__name__})") from None
        seconds = time.monotonic() - started
        if res.status_code >= 500:
            raise VlmServerError(f"model server error (HTTP {res.status_code})")
        if res.status_code >= 400:
            raise VlmRequestError(f"model server rejected the request (HTTP {res.status_code}): {_error_text(res)}")
        try:
            data = res.json()
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise VlmBadResponse("model server returned an unexpected response shape") from None
        if not isinstance(content, str):
            raise VlmBadResponse("model reply has no text content")
        usage = data.get("usage") or {}
        reply = VlmReply(
            text=content,
            finish_reason=str(choice.get("finish_reason") or ""),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            seconds=round(seconds, 1),
        )
        logger.info(
            "vlm_request_ok purpose=%s seconds=%.1f prompt_tokens=%s completion_tokens=%s finish=%s",
            purpose, seconds, reply.prompt_tokens, reply.completion_tokens, reply.finish_reason,
        )
        return reply

    # ------------------------------------------------------------------ tasks

    def transcribe_image(self, png: bytes) -> VlmReply:
        """The page image → text (plain lines + HTML tables) as the model reads it."""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": png_data_uri(png)}},
                    {"type": "text", "text": TRANSCRIBE_PROMPT},
                ],
            }
        ]
        return self.chat(messages, max_tokens=self.settings.vlm_transcribe_max_tokens, purpose="transcribe")

    def complete_json(self, system: str, user: str, schema: dict, *, max_tokens: Optional[int] = None) -> dict:
        """Schema-constrained JSON answer, parsed. A cut-off or invalid reply raises VlmBadResponse."""
        reply = self.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=max_tokens or self.settings.vlm_map_max_tokens,
            response_format={"type": "json_schema", "json_schema": {"name": "extraction", "schema": schema}},
            purpose="map",
        )
        if reply.truncated:
            raise VlmBadResponse("model reply was cut off at the token limit")
        try:
            parsed = json.loads(reply.text)
        except ValueError:
            raise VlmBadResponse("model reply was not valid JSON") from None
        if not isinstance(parsed, dict):
            raise VlmBadResponse("model reply JSON is not an object")
        return parsed


def _error_text(res: httpx.Response) -> str:
    try:
        err = res.json().get("error")
        msg = err.get("message") if isinstance(err, dict) else err
        return redact(str(msg))[:160]
    except Exception:
        return "no details"
