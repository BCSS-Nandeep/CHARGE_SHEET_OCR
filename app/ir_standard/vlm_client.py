"""Client for a local OpenAI-compatible vision-language server.

The model (e.g. Nanonets-OCR served by llama.cpp, Ollama or vLLM) runs as a
separate local process; this module only speaks HTTP to it. It refuses
non-local endpoints, so report content never leaves the machine, and it is
independent of the runtime: swapping CPU llama.cpp for a GPU vLLM is a
settings change.
"""

from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional

import httpx

from app.config import Settings, get_settings


class VlmError(RuntimeError):
    """The VLM server is unreachable, refused the request, or returned nothing usable."""


class VlmClient:
    def __init__(
        self,
        settings: Optional[Settings] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self.settings = settings or get_settings()
        s = self.settings
        if not s.llm_enabled:
            raise VlmError("VLM disabled (set LLM_ENABLED=true and LLM_BASE_URL)")
        if not s.llm_base_url or not s.llm_model:
            raise VlmError("LLM_BASE_URL and LLM_MODEL must be set for the VLM")
        if not s.llm_is_local_only():
            raise VlmError("VLM endpoint must be local (127.0.0.1/localhost) unless LLM_ALLOW_EXTERNAL=true")
        headers = {"Authorization": f"Bearer {s.llm_api_key}"} if s.llm_api_key else {}
        self._http = httpx.Client(
            base_url=s.llm_base_url.rstrip("/"),
            timeout=httpx.Timeout(s.vlm_timeout_seconds, connect=10.0),
            headers=headers,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def is_up(self) -> bool:
        try:
            return self._http.get("/models", timeout=5.0).status_code < 500
        except httpx.HTTPError:
            return False

    def _chat(self, messages: List[Dict[str, Any]], max_tokens: int) -> str:
        body = {
            "model": self.settings.llm_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0,
        }
        try:
            resp = self._http.post("/chat/completions", json=body)
        except httpx.TimeoutException as exc:
            raise VlmError(f"VLM request timed out after {self.settings.vlm_timeout_seconds}s") from exc
        except httpx.HTTPError as exc:
            raise VlmError(f"VLM server unreachable: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise VlmError(f"VLM server returned HTTP {resp.status_code}")
        try:
            text = resp.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise VlmError("VLM response had no message content") from exc
        if not isinstance(text, str) or not text.strip():
            raise VlmError("VLM returned an empty answer")
        return text

    def transcribe_png(self, png: bytes, prompt: str, max_tokens: Optional[int] = None) -> str:
        """Send one page image with an instruction; returns the model's text."""
        data_uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_uri}},
                    {"type": "text", "text": prompt},
                ],
            }
        ]
        return self._chat(messages, max_tokens or self.settings.vlm_page_max_tokens)

    def complete(self, system: str, user: str, max_tokens: Optional[int] = None) -> str:
        """Text-only request (mapping report text onto the template)."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        return self._chat(messages, max_tokens or self.settings.vlm_section_max_tokens)
