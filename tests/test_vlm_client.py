"""VLM client: configuration, health, retries, error mapping and log hygiene (no real server)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import get_settings
from app.services.vlm_client import (
    VlmBadResponse,
    VlmClient,
    VlmRequestError,
    VlmServerError,
    VlmTimeout,
    VlmUnavailable,
)
from app.services.vlm_text import parse_transcription, split_for_mapping


@pytest.fixture(autouse=True)
def vlm_env(monkeypatch):
    monkeypatch.setenv("VLM_ENABLED", "true")
    monkeypatch.setenv("VLM_BASE_URL", "http://127.0.0.1:8081/v1")
    monkeypatch.setenv("VLM_MODEL", "nanonets-ocr2-3b")
    monkeypatch.setenv("VLM_API_KEY", "super-secret-key-value")
    monkeypatch.setenv("VLM_RETRY_BACKOFF_SECONDS", "0")
    monkeypatch.setenv("VLM_MAX_RETRIES", "2")
    get_settings.cache_clear()


def client(handler) -> VlmClient:
    return VlmClient(get_settings(), transport=httpx.MockTransport(handler))


def ok_reply(text="hello", finish="stop"):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}, "finish_reason": finish}],
                                     "usage": {"prompt_tokens": 5, "completion_tokens": 2}})


MODELS = {"data": [{"id": "nanonets-ocr2-3b", "aliases": ["nanonets-ocr2-3b"],
                    "architecture": {"input_modalities": ["text", "image"]}}]}


# ---------------------------------------------------------------- configuration / health


def test_not_configured_is_reported_not_attempted(monkeypatch):
    monkeypatch.setenv("VLM_ENABLED", "false")
    get_settings.cache_clear()
    calls = []
    c = client(lambda r: calls.append(r) or ok_reply())
    assert c.config_problem() == "VLM_ENABLED=false"
    with pytest.raises(VlmUnavailable):
        c.chat([{"role": "user", "content": "x"}], max_tokens=5)
    assert calls == []


def test_missing_url_or_model_is_a_config_problem(monkeypatch):
    monkeypatch.setenv("VLM_BASE_URL", "")
    get_settings.cache_clear()
    assert "VLM_BASE_URL" in VlmClient(get_settings()).config_problem()
    monkeypatch.setenv("VLM_BASE_URL", "http://127.0.0.1:8081/v1")
    monkeypatch.setenv("VLM_MODEL", "")
    get_settings.cache_clear()
    assert "VLM_MODEL" in VlmClient(get_settings()).config_problem()


def test_external_host_is_refused_in_offline_mode(monkeypatch):
    monkeypatch.setenv("VLM_BASE_URL", "https://api.example.com/v1")
    get_settings.cache_clear()
    assert "offline mode" in VlmClient(get_settings()).config_problem()


def test_health_reports_model_and_image_support():
    def handler(req):
        assert req.url.path == "/v1/models"
        return httpx.Response(200, json=MODELS)

    h = client(handler).health()
    assert h["reachable"] and h["model_available"] and h["supports_images"] is True and h["error"] is None


def test_health_when_model_not_served_or_server_down():
    h = client(lambda r: httpx.Response(200, json={"data": [{"id": "other"}]})).health()
    assert h["reachable"] and not h["model_available"] and "not served" in h["error"]

    def down(req):
        raise httpx.ConnectError("refused")

    h = client(down).health()
    assert not h["reachable"] and "not reachable" in h["error"]


def test_health_sends_no_inference_request():
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path))
        return httpx.Response(200, json=MODELS)

    client(handler).health()
    assert seen == [("GET", "/v1/models")]


def test_api_key_is_sent_as_bearer_and_never_in_errors():
    seen = {}

    def handler(req):
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(400, json={"error": {"message": "bad image"}})

    with pytest.raises(VlmRequestError) as ei:
        client(handler).chat([{"role": "user", "content": "x"}], max_tokens=5)
    assert seen["auth"] == "Bearer super-secret-key-value"
    assert "super-secret" not in str(ei.value)


# ---------------------------------------------------------------- request / retry behaviour


def test_success_returns_text_and_usage():
    r = client(lambda req: ok_reply("page text")).chat([{"role": "user", "content": "x"}], max_tokens=5)
    assert r.text == "page text" and r.completion_tokens == 2 and not r.truncated


def test_request_body_names_the_configured_model_and_never_another():
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return ok_reply()

    client(handler).chat([{"role": "user", "content": "x"}], max_tokens=5)
    assert bodies[0]["model"] == "nanonets-ocr2-3b" and bodies[0]["temperature"] == 0.0


def test_transcribe_sends_the_page_image():
    bodies = []
    client(lambda req: bodies.append(json.loads(req.content)) or ok_reply()).transcribe_image(b"\x89PNGfake")
    parts = bodies[0]["messages"][0]["content"]
    assert parts[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_connection_error_is_retried_then_reported():
    calls = []

    def handler(req):
        calls.append(1)
        raise httpx.ConnectError("refused")

    with pytest.raises(VlmUnavailable):
        client(handler).chat([{"role": "user", "content": "x"}], max_tokens=5)
    assert len(calls) == 3  # 1 + VLM_MAX_RETRIES


def test_timeout_is_retried_and_reported_as_timeout():
    calls = []

    def handler(req):
        calls.append(1)
        raise httpx.ReadTimeout("slow")

    with pytest.raises(VlmTimeout) as ei:
        client(handler).chat([{"role": "user", "content": "x"}], max_tokens=5)
    assert len(calls) == 3 and "VLM_TIMEOUT_SECONDS" in str(ei.value)


def test_server_error_is_retried_and_can_recover():
    replies = [httpx.Response(503), ok_reply("recovered")]
    r = client(lambda req: replies.pop(0)).chat([{"role": "user", "content": "x"}], max_tokens=5)
    assert r.text == "recovered"


def test_persistent_5xx_raises_server_error():
    with pytest.raises(VlmServerError):
        client(lambda req: httpx.Response(500)).chat([{"role": "user", "content": "x"}], max_tokens=5)


def test_permanent_errors_are_not_retried():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(400, json={"error": {"message": "image input is not supported"}})

    with pytest.raises(VlmRequestError) as ei:
        client(handler).chat([{"role": "user", "content": "x"}], max_tokens=5)
    assert len(calls) == 1 and "not supported" in str(ei.value)


@pytest.mark.parametrize("response", [
    httpx.Response(200, text="<html>not json</html>"),
    httpx.Response(200, json={"choices": []}),
    httpx.Response(200, json={"choices": [{"message": {"content": None}}]}),
])
def test_malformed_replies_raise_bad_response_without_retry(response):
    calls = []

    def handler(req):
        calls.append(1)
        return response

    with pytest.raises(VlmBadResponse):
        client(handler).chat([{"role": "user", "content": "x"}], max_tokens=5)
    assert len(calls) == 1


def test_complete_json_parses_and_rejects_bad_json_and_truncation():
    schema = {"type": "object"}
    assert client(lambda r: ok_reply('{"a": "b"}')).complete_json("s", "u", schema) == {"a": "b"}
    with pytest.raises(VlmBadResponse, match="not valid JSON"):
        client(lambda r: ok_reply("```json\n{}\n```")).complete_json("s", "u", schema)
    with pytest.raises(VlmBadResponse, match="not an object"):
        client(lambda r: ok_reply("[1]")).complete_json("s", "u", schema)
    with pytest.raises(VlmBadResponse, match="cut off"):
        client(lambda r: ok_reply('{"a": "b', finish="length")).complete_json("s", "u", schema)


def test_complete_json_sends_the_schema_as_response_format():
    bodies = []
    client(lambda r: bodies.append(json.loads(r.content)) or ok_reply("{}")).complete_json("s", "u", {"type": "object"})
    assert bodies[0]["response_format"]["json_schema"]["schema"] == {"type": "object"}


def test_server_busy_check_is_read_only_and_tolerant():
    c = client(lambda r: httpx.Response(200, json=[{"id": 0, "is_processing": True}]))
    assert c.server_busy() is True
    assert client(lambda r: httpx.Response(404)).server_busy() is None


# ---------------------------------------------------------------- transcription parsing


def test_transcription_tables_and_tags():
    raw = (
        "<watermark>OFFICIAL COPY</watermark>\n"
        "CHARGE SHEET\n"
        "<table><tr><td>FIR No</td><td>901/2021</td></tr><tr><td>District</td><td></td><td>Coastal<br>District</td></tr></table>\n"
        "<page_number>2</page_number>\n"
        "<img>a stamp</img>\n"
        "Brief facts follow"
    )
    p = parse_transcription(raw)
    assert p.items == [("line", "CHARGE SHEET"), ("row", ["FIR No", "901/2021"]), ("row", ["District", "Coastal District"]),
                       ("line", "Brief facts follow")]
    assert p.mapping_text.splitlines()[1] == "FIR No | 901/2021"
    assert "OFFICIAL COPY" not in p.mapping_text and "a stamp" not in p.mapping_text
    assert any("OFFICIAL COPY" in l for l in p.raw_lines)  # raw view keeps everything as read


def test_empty_transcription_is_empty():
    assert parse_transcription("  \n ").char_count == 0


def test_split_for_mapping_keeps_every_line_once():
    text = "\n".join(f"line {i} " + "x" * 30 for i in range(20))
    parts = split_for_mapping(text, 200)
    assert len(parts) > 1 and "\n".join(parts) == text
    assert split_for_mapping("short", 200) == ["short"]
