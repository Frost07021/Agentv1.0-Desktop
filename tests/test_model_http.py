import asyncio
import json

import httpx

from app.config import ModelSettings
from app.model_http import completion_content, reasoning_options, request_chat_completion, with_reasoning


def _settings(model: str = "qwen3.7-plus") -> ModelSettings:
    return ModelSettings(model, "https://model.example/v1", "secret")


def test_qwen_complex_requests_enable_bounded_thinking_without_losing_answer_budget(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_MODEL_THINKING", raising=False)
    monkeypatch.delenv("AGENT_MODEL_THINKING_BUDGET", raising=False)
    payload = with_reasoning({"max_tokens": 5000}, _settings(), default_budget=4096)
    assert payload["enable_thinking"] is True
    assert payload["thinking_budget"] == 4096
    assert payload["stream"] is True
    assert payload["max_tokens"] == 9096


def test_reasoning_policy_does_not_leak_qwen_parameters_to_unknown_providers(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_MODEL_THINKING", raising=False)
    assert reasoning_options(_settings("generic-vision-model"), 4096) == {}


def test_reasoning_can_be_explicitly_disabled_for_cost_or_latency(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_MODEL_THINKING", "off")
    assert reasoning_options(_settings(), 4096) == {"enable_thinking": False}


def test_streamed_completion_uses_visible_content_and_ignores_reasoning() -> None:
    events = [
        {"choices": [{"delta": {"reasoning_content": "内部推理", "content": None}}]},
        {"choices": [{"delta": {"content": '{"answer":'}}]},
        {"choices": [{"delta": {"content": '"ok"}'}}]},
    ]
    body = "\n".join(f"data: {json.dumps(event, ensure_ascii=False)}" for event in events) + "\ndata: [DONE]\n"
    response = httpx.Response(200, text=body)
    assert completion_content(response) == '{"answer":"ok"}'


def test_non_streamed_completion_remains_supported() -> None:
    response = httpx.Response(200, json={"choices": [{"message": {"content": "done"}}]})
    assert completion_content(response) == "done"


def test_request_can_reuse_a_caller_owned_http_client() -> None:
    class ReusableClient:
        def __init__(self) -> None:
            self.calls = 0

        async def post(self, url: str, **_kwargs) -> httpx.Response:
            self.calls += 1
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={"choices": [{"message": {"content": "pooled"}}]},
            )

    client = ReusableClient()
    content = asyncio.run(
        request_chat_completion(
            _settings(),
            {"model": "qwen3.7-plus", "messages": []},
            timeout_seconds=30,
            client=client,  # type: ignore[arg-type]
        )
    )

    assert content == "pooled"
    assert client.calls == 1


def test_streaming_request_records_first_event_and_request_id() -> None:
    body = (
        'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"{\\"answer\\":\\"ok\\"}"}}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"x-request-id": "req-native-1"},
            text=body,
        )

    diagnostics: dict = {}
    first_sse_event = asyncio.Event()

    async def run() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await request_chat_completion(
                _settings(),
                {"model": "qwen3.7-plus", "messages": [], "stream": True},
                timeout_seconds=30,
                client=client,
                connect_timeout_seconds=5,
                write_timeout_seconds=10,
                pool_timeout_seconds=3,
                total_timeout_seconds=40,
                diagnostics=diagnostics,
                first_sse_event=first_sse_event,
            )

    assert asyncio.run(run()) == '{"answer":"ok"}'
    assert diagnostics["outcome"] == "completed"
    assert diagnostics["request_id"] == "req-native-1"
    assert diagnostics["first_body_byte_ms"] is not None
    assert diagnostics["first_sse_event_ms"] is not None
    assert diagnostics["http_attempts"][0]["sse_event_count"] == 3
    assert diagnostics["http_attempts"][0]["outcome"] == "completed"
    assert first_sse_event.is_set()
    assert diagnostics["timeouts"] == {
        "connect_seconds": 5,
        "write_seconds": 10,
        "read_idle_seconds": 30,
        "pool_seconds": 3,
        "total_seconds": 40,
    }
