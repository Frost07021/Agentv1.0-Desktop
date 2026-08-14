from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any

import httpx

from .config import ModelSettings


_QWEN_HYBRID_THINKING_RE = re.compile(r"^qwen3\.(?:5|6|7)(?:[-._]|$)", re.IGNORECASE)
_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}


def reasoning_options(settings: ModelSettings, default_budget: int) -> dict[str, Any]:
    """Return safe reasoning options for known hybrid-thinking models.

    AGENT_MODEL_THINKING accepts auto/on/off. In auto mode we only send the
    provider-specific fields to Qwen model families known to support them, so a
    generic OpenAI-compatible endpoint does not receive unknown parameters.
    """
    raw_mode = os.getenv("AGENT_MODEL_THINKING", "auto").strip().lower()
    if raw_mode not in {"auto", *_TRUE_VALUES, *_FALSE_VALUES}:
        raise ValueError("AGENT_MODEL_THINKING 必须为 auto、on 或 off")

    known_hybrid_model = bool(_QWEN_HYBRID_THINKING_RE.match(settings.model.strip()))
    explicitly_enabled = raw_mode in _TRUE_VALUES
    explicitly_disabled = raw_mode in _FALSE_VALUES
    if explicitly_disabled:
        return {"enable_thinking": False} if known_hybrid_model else {}
    if not explicitly_enabled and not known_hybrid_model:
        return {}

    raw_budget = os.getenv("AGENT_MODEL_THINKING_BUDGET")
    budget = int(raw_budget) if raw_budget else default_budget
    if budget <= 0:
        raise ValueError("AGENT_MODEL_THINKING_BUDGET 必须为正整数")
    return {
        "enable_thinking": True,
        "thinking_budget": budget,
        # Streaming is the broadly compatible way to invoke Qwen thinking
        # models. The caller may still buffer the SSE body before validation.
        "stream": True,
    }


def with_reasoning(
    payload: dict[str, Any],
    settings: ModelSettings,
    *,
    default_budget: int,
) -> dict[str, Any]:
    """Attach reasoning policy without stealing the visible-answer budget."""
    result = dict(payload)
    options = reasoning_options(settings, default_budget)
    result.update(options)
    if options.get("enable_thinking") is True:
        visible_budget = int(result.get("max_tokens") or 0)
        result["max_tokens"] = visible_budget + int(options["thinking_budget"])
    return result


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            str(item.get("text", ""))
            for item in value
            if isinstance(item, dict)
        )
    return ""


def completion_content(response: httpx.Response) -> str:
    """Read visible assistant content from JSON or OpenAI-compatible SSE."""
    text = response.text
    if any(line.lstrip().startswith("data:") for line in text.splitlines()):
        parts: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            event = json.loads(data)
            choices = event.get("choices") or []
            if not choices:
                continue
            choice = choices[0] if isinstance(choices[0], dict) else {}
            delta = choice.get("delta") or choice.get("message") or {}
            parts.append(_content_text(delta.get("content")))
        content = "".join(parts)
        if content:
            return content

    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise ValueError("模型响应缺少 choices")
    message = choices[0].get("message") or choices[0].get("delta") or {}
    content = _content_text(message.get("content"))
    if not content:
        raise ValueError("模型未返回可见答案内容")
    return content


async def request_chat_completion(
    settings: ModelSettings,
    payload: dict[str, Any],
    *,
    timeout_seconds: float,
    client: httpx.AsyncClient | None = None,
    connect_timeout_seconds: float = 20.0,
    write_timeout_seconds: float | None = None,
    pool_timeout_seconds: float = 20.0,
    total_timeout_seconds: float | None = None,
    diagnostics: dict[str, Any] | None = None,
    first_sse_event: asyncio.Event | None = None,
) -> str:
    """Execute one compatible chat completion and normalize its answer text.

    A caller-owned client lets a multi-stage analysis reuse its TLS connection
    pool.  Single-call adapters keep the previous self-contained behavior.
    """
    headers = {"Authorization": f"Bearer {settings.api_key}", "Content-Type": "application/json"}
    write_timeout = timeout_seconds if write_timeout_seconds is None else write_timeout_seconds
    timeout = httpx.Timeout(
        connect=connect_timeout_seconds,
        write=write_timeout,
        read=timeout_seconds,
        pool=pool_timeout_seconds,
    )
    started = time.perf_counter()
    telemetry = diagnostics if diagnostics is not None else {}
    telemetry["timeouts"] = {
        "connect_seconds": connect_timeout_seconds,
        "write_seconds": write_timeout,
        "read_idle_seconds": timeout_seconds,
        "pool_seconds": pool_timeout_seconds,
        "total_seconds": total_timeout_seconds,
    }
    telemetry["stream_requested"] = bool(payload.get("stream"))
    telemetry["http_attempts"] = []

    def request_id(response: httpx.Response) -> str | None:
        for name in ("x-request-id", "x-dashscope-request-id", "request-id"):
            value = response.headers.get(name)
            if value:
                return value
        return None

    async def send_once(
        active_client: httpx.AsyncClient,
        active_payload: dict[str, Any],
    ) -> httpx.Response:
        attempt_started = time.perf_counter()
        attempt: dict[str, Any] = {
            "attempt": len(telemetry["http_attempts"]) + 1,
            "response_format_requested": "response_format" in active_payload,
        }
        telemetry["http_attempts"].append(attempt)
        try:
            # Real httpx clients expose stream(); small duck-typed test clients
            # may only implement post(), so retain a compatibility path.
            stream_method = getattr(active_client, "stream", None)
            if callable(stream_method):
                async with stream_method(
                    "POST",
                    settings.chat_completions_url,
                    headers=headers,
                    json=active_payload,
                    timeout=timeout,
                ) as live_response:
                    attempt["response_headers_ms"] = round(
                        (time.perf_counter() - attempt_started) * 1000,
                        2,
                    )
                    attempt["status_code"] = live_response.status_code
                    upstream_id = request_id(live_response)
                    if upstream_id:
                        attempt["request_id"] = upstream_id
                    body = bytearray()
                    first_byte_ms: float | None = None
                    first_sse_event_ms: float | None = None
                    last_byte_ms: float | None = None
                    suffix = b""
                    async for chunk in live_response.aiter_bytes():
                        if not chunk:
                            continue
                        now_ms = round((time.perf_counter() - attempt_started) * 1000, 2)
                        if first_byte_ms is None:
                            first_byte_ms = now_ms
                        last_byte_ms = now_ms
                        combined = suffix + chunk
                        if first_sse_event_ms is None and b"data:" in combined:
                            first_sse_event_ms = now_ms
                            if first_sse_event is not None:
                                first_sse_event.set()
                        suffix = combined[-5:]
                        body.extend(chunk)
                    attempt["first_body_byte_ms"] = first_byte_ms
                    attempt["first_sse_event_ms"] = first_sse_event_ms
                    attempt["last_body_byte_ms"] = last_byte_ms
                    attempt["response_bytes"] = len(body)
                    # Count once over the complete body. Counting each chunk
                    # with an overlap suffix can otherwise double-count an
                    # event whose marker was wholly inside that suffix.
                    attempt["sse_event_count"] = bytes(body).count(b"data:")
                    response = httpx.Response(
                        live_response.status_code,
                        headers=live_response.headers,
                        content=bytes(body),
                        request=live_response.request,
                    )
            else:
                response = await active_client.post(
                    settings.chat_completions_url,
                    headers=headers,
                    json=active_payload,
                    timeout=timeout,
                )
                elapsed_ms = round((time.perf_counter() - attempt_started) * 1000, 2)
                attempt.update(
                    {
                        "response_headers_ms": elapsed_ms,
                        "status_code": response.status_code,
                        "first_body_byte_ms": elapsed_ms if response.content else None,
                        "last_body_byte_ms": elapsed_ms if response.content else None,
                        "response_bytes": len(response.content),
                        "sse_event_count": response.text.count("data:"),
                    }
                )
                upstream_id = request_id(response)
                if upstream_id:
                    attempt["request_id"] = upstream_id
                if first_sse_event is not None and b"data:" in response.content:
                    first_sse_event.set()
            attempt["elapsed_ms"] = round((time.perf_counter() - attempt_started) * 1000, 2)
            attempt["outcome"] = "completed"
            return response
        except BaseException as exc:
            attempt["outcome"] = "failed"
            attempt["error_type"] = type(exc).__name__
            attempt["elapsed_ms"] = round((time.perf_counter() - attempt_started) * 1000, 2)
            raise

    async def send(active_client: httpx.AsyncClient) -> httpx.Response:
        response = await send_once(active_client, payload)
        if response.status_code == 400 and "response_format" in response.text:
            telemetry["response_format_compatibility_retry"] = True
            compatible_payload = dict(payload)
            compatible_payload.pop("response_format", None)
            response = await send_once(active_client, compatible_payload)
        response.raise_for_status()
        return response

    async def execute() -> httpx.Response:
        if client is None:
            async with httpx.AsyncClient(timeout=timeout) as owned_client:
                return await send(owned_client)
        return await send(client)

    try:
        if total_timeout_seconds is not None and total_timeout_seconds > 0:
            try:
                async with asyncio.timeout(total_timeout_seconds):
                    response = await execute()
            except TimeoutError as exc:
                telemetry["timeout_phase"] = "total"
                attempts = telemetry.get("http_attempts") or []
                if attempts:
                    attempts[-1]["outcome"] = "failed"
                    attempts[-1]["error_type"] = "TotalTimeout"
                    attempts[-1]["timeout_phase"] = "total"
                total_error = httpx.ReadTimeout(
                    f"请求总耗时超过 {total_timeout_seconds:.0f} 秒"
                )
                setattr(total_error, "timeout_phase", "total")
                raise total_error from exc
        else:
            response = await execute()
        content = completion_content(response)
        telemetry["outcome"] = "completed"
        telemetry["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        attempts = telemetry.get("http_attempts") or []
        if attempts:
            last_attempt = attempts[-1]
            telemetry["request_id"] = last_attempt.get("request_id")
            telemetry["first_body_byte_ms"] = last_attempt.get("first_body_byte_ms")
            telemetry["first_sse_event_ms"] = last_attempt.get("first_sse_event_ms")
            telemetry["response_bytes"] = last_attempt.get("response_bytes")
        return content
    except BaseException as exc:
        telemetry["outcome"] = "failed"
        telemetry["error_type"] = type(exc).__name__
        telemetry["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        raise
