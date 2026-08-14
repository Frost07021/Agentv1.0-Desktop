from app.chat_adapter import _stream_content_delta


def test_ignores_stream_usage_event_with_empty_choices() -> None:
    assert _stream_content_delta({"choices": [], "usage": {"total_tokens": 42}}) is None


def test_reads_regular_and_segmented_stream_content() -> None:
    assert _stream_content_delta({"choices": [{"delta": {"content": "你好"}}]}) == "你好"
    assert _stream_content_delta(
        {"choices": [{"delta": {"content": [{"type": "text", "text": "警长"}]}}]}
    ) == "警长"
