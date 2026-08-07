import asyncio
import json
from pathlib import Path

from app.schemas import AnalysisResultRecord, Conversation, Message, PetContext
from app.config import ModelSettings
from app.structured_response import (
    FakeStructuredResponseAdapter,
    OpenAICompatibleStructuredResponseAdapter,
    StructuredResponseService,
    build_structured_context,
    compact_analysis,
    validate_structured_response,
)


def _home_result() -> dict:
    return {
        "report_meta": {
            "category": "dental",
            "category_name": "牙科评估",
            "test_date": "2026-07-28 12:00",
            "media": {"url": "private-file.jpg", "thumbnail_url": "private-thumb.jpg"},
        },
        "ai_summary": {"severity": "中度", "severity_color": "orange", "summary": "发现牙结石需关注。"},
        "dimensions": [
            {
                "title": "牙结石评估",
                "status_label": "中度牙结石",
                "ui_color": "orange",
                "ai_analysis": "犬齿根部可见黄褐色沉积。",
                "suggestion": "建议进行专业口腔检查。",
            }
        ],
        "health_suggestions": [
            {"ui_label": "PRIORITY_高", "ui_color": "blue", "title": "口腔检查", "content": "预约兽医进行口腔检查。"},
            {"ui_label": "PRIORITY_中", "ui_color": "blue", "title": "日常刷牙", "content": "循序渐进建立刷牙习惯。"},
        ],
        "disclaimer": "仅供参考。",
    }


def test_context_is_normalized_and_drops_media_rendering_fields() -> None:
    record = AnalysisResultRecord(
        result_id="result_1",
        task_id="task_1",
        source_type="home_check",
        skill_name="home-health-check-dental",
        conversation_id="conv_1",
        result=_home_result(),
    )
    conversation = Conversation(
        conversation_id="conv_1",
        user_id="user_1",
        pet=PetContext(pet_name="警长", breed="英短"),
        title="警长的宠物管家",
        mode="fake",
    )
    history = [
        Message(message_id="m1", conversation_id="conv_1", role="user", text="之前的问题"),
        Message(message_id="m2", conversation_id="conv_1", role="user", text="这严重吗？"),
    ]
    context = build_structured_context(record, conversation, "这严重吗？", history, [record])
    assert context["source_type"] == "home_check"
    assert context["pet_profile"] == {"pet_name": "警长", "breed": "英短"}
    assert context["conversation_history"] == [{"role": "user", "content": "之前的问题"}]
    serialized = json.dumps(context, ensure_ascii=False)
    assert "private-file.jpg" not in serialized
    assert "thumbnail_url" not in serialized


def test_fake_structured_response_strictly_matches_skill_schema() -> None:
    root = Path(__file__).resolve().parents[1]
    service = StructuredResponseService(root)
    context = {
        "source_type": "home_check",
        "analysis_json": compact_analysis(_home_result(), "home_check"),
        "user_question": "牙结石严重吗？",
        "pet_profile": {"pet_name": "警长"},
        "history_summary": [],
        "conversation_history": [],
    }
    payload = asyncio.run(service.generate(context))
    assert validate_structured_response(payload, service.schema) == []
    reply = payload["reply"]
    titles = [item["content"] for item in reply["segments"] if item["type"] == "section_title"]
    assert titles == ["🤗 暖心开场", "🔍 主要发现", "📚 简单解释", "💡 意见与建议", "🌈 温暖结语"]
    assert 1 <= len(reply["suggested_questions"]) <= 2


def test_service_rejects_invalid_adapter_output() -> None:
    root = Path(__file__).resolve().parents[1]
    service = StructuredResponseService(root)

    class InvalidAdapter(FakeStructuredResponseAdapter):
        async def generate(self, context, skill):  # type: ignore[no-untyped-def]
            del context, skill
            return {"reply": {"emotion": "cold", "segments": [], "suggested_questions": []}}

    service.adapter = InvalidAdapter()
    context = {
        "source_type": "home_check",
        "analysis_json": compact_analysis(_home_result(), "home_check"),
        "user_question": "怎么看？",
        "pet_profile": {},
        "history_summary": [],
        "conversation_history": [],
    }
    try:
        asyncio.run(service.generate(context))
    except ValueError as exc:
        assert "warm_empathy" in str(exc) or "标题" in str(exc)
    else:
        raise AssertionError("无效结构化回答不应通过")


def test_compact_analysis_keeps_all_items_beyond_legacy_eight_item_cutoff() -> None:
    result = _home_result()
    result["dimensions"] = [
        {
            "title": f"检测项{index}",
            "status_label": "正常",
            "ui_color": "green",
            "ai_analysis": "这是该检测项的完整观察说明。",
            "suggestion": "继续观察。",
        }
        for index in range(12)
    ]
    compact = compact_analysis(result, "home_check")
    assert len(compact["items"]) == 12
    assert compact["items"][-1]["title"] == "检测项11"
    assert compact["items"][-1]["ai_analysis"] == "这是该检测项的完整观察说明。"
    assert compact["items"][-1]["suggestion"] == "继续观察。"


def test_structured_quality_gate_rejects_short_body_and_incomplete_advice() -> None:
    root = Path(__file__).resolve().parents[1]
    service = StructuredResponseService(root)
    context = {
        "source_type": "home_check",
        "analysis_json": compact_analysis(_home_result(), "home_check"),
        "user_question": "牙结石严重吗？",
        "pet_profile": {"pet_name": "警长"},
        "history_summary": [],
        "conversation_history": [],
    }
    context["analysis_json"]["health_suggestions"].append(
        {"ui_label": "PRIORITY_低", "ui_color": "blue", "title": "定期复查", "content": "记录变化并复查。"}
    )
    payload = asyncio.run(service.adapter.generate(context, service.skill))
    segments = payload["reply"]["segments"]
    finding = next(item for item in segments if item["type"] == "list_item")
    finding["content"] = "[[牙结石]]为[[中度]]。"
    finding["highlights"] = ["牙结石", "中度"]
    suggestions = [item for item in segments if item["type"] == "suggestion_item"]
    segments.remove(suggestions[-1])
    errors = validate_structured_response(payload, service.schema, context)
    assert any("内容过短" in error for error in errors)
    assert any("3 条分级建议" in error for error in errors)


def test_structured_repair_keeps_original_analysis_context() -> None:
    root = Path(__file__).resolve().parents[1]
    service = StructuredResponseService(root)
    context = {
        "source_type": "home_check",
        "analysis_json": compact_analysis(_home_result(), "home_check"),
        "user_question": "牙结石严重吗？",
        "pet_profile": {"pet_name": "警长"},
        "history_summary": [],
        "conversation_history": [],
    }
    valid = asyncio.run(FakeStructuredResponseAdapter().generate(context, service.skill))
    adapter = OpenAICompatibleStructuredResponseAdapter(
        ModelSettings("test-model", "https://model.example/v1", "secret"),
        service.schema,
    )
    users: list[str] = []

    async def fake_request(_system: str, user: str) -> dict:
        users.append(user)
        return {"reply": {"emotion": "cold", "segments": [], "suggested_questions": []}} if len(users) == 1 else valid

    adapter._request = fake_request  # type: ignore[method-assign]
    result = asyncio.run(adapter.generate(context, service.skill))

    assert result == valid
    assert len(users) == 2
    assert "原始规范化输入" in users[1]
    assert "犬齿根部可见黄褐色沉积" in users[1]
