from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
from jsonschema import Draft202012Validator

from .config import ModelSettings
from .identity import identity_system_prompt, sanitize_model_payload
from .schemas import AnalysisResultRecord, Conversation, Message
from .skill_loader import SkillDefinition, SkillRegistry


SECTION_TITLES = [
    "🤗 暖心开场",
    "🔍 主要发现",
    "📚 简单解释",
    "💡 意见与建议",
    "🌈 温暖结语",
]
BODY_TYPES = {
    SECTION_TITLES[0]: "text",
    SECTION_TITLES[1]: "list_item",
    SECTION_TITLES[2]: "text",
    SECTION_TITLES[3]: "suggestion_item",
    SECTION_TITLES[4]: "text",
}
MARKER_RE = re.compile(r"\[\[([^\[\]]+)\]\]")


class StructuredResponseValidationError(ValueError):
    pass


def _extract_json(content: Any) -> dict[str, Any]:
    if isinstance(content, list):
        content = "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    cleaned = re.sub(r"^```(?:json)?\s*", "", str(content or "").strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("结构化追问响应中未找到 JSON")
        payload = json.loads(cleaned[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("结构化追问响应必须为 JSON 对象")
    return payload


def validate_structured_response(
    payload: dict[str, Any],
    schema: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> list[str]:
    errors = [error.message for error in Draft202012Validator(schema).iter_errors(payload)]
    reply = payload.get("reply")
    if not isinstance(reply, dict):
        return errors
    segments = reply.get("segments")
    if not isinstance(segments, list):
        return errors

    titles: list[str] = []
    current_title: str | None = None
    body_counts = {title: 0 for title in SECTION_TITLES}
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            continue
        kind = segment.get("type")
        content = segment.get("content")
        highlights = segment.get("highlights")
        if kind == "section_title":
            if isinstance(content, str):
                titles.append(content)
                current_title = content
            if highlights != []:
                errors.append(f"segments[{index}] 标题 highlights 必须为空")
            continue
        if current_title in BODY_TYPES:
            body_counts[current_title] += 1
            if kind != BODY_TYPES[current_title]:
                errors.append(f"segments[{index}] 在 {current_title} 下类型不正确")
        if isinstance(content, str) and isinstance(highlights, list):
            markers = MARKER_RE.findall(content)
            if len(markers) != len(set(markers)) or set(markers) != set(highlights):
                errors.append(f"segments[{index}] highlights 与正文标记不一致")
            if content.count("[[") != len(markers) or content.count("]]" ) != len(markers):
                errors.append(f"segments[{index}] 包含损坏的高亮标记")
            if current_title == SECTION_TITLES[1] and len(highlights) < 2:
                errors.append(f"segments[{index}] 主要发现必须同时标记医学项与状态结论")
            if current_title == SECTION_TITLES[2] and not highlights:
                errors.append(f"segments[{index}] 简单解释必须标记相关医学或健康名词")
            if current_title == SECTION_TITLES[3] and not highlights:
                errors.append(f"segments[{index}] 建议必须标记建议小标题")
    if titles != SECTION_TITLES:
        errors.append("五个固定标题缺失、重复或顺序不正确")
    for title, count in body_counts.items():
        if count == 0:
            errors.append(f"{title} 缺少正文")
    minimum_lengths = {
        SECTION_TITLES[0]: 20,
        SECTION_TITLES[1]: 24,
        SECTION_TITLES[2]: 30,
        SECTION_TITLES[3]: 24,
        SECTION_TITLES[4]: 20,
    }
    current_title = None
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            continue
        if segment.get("type") == "section_title":
            current_title = segment.get("content")
            continue
        content = segment.get("content")
        if current_title in minimum_lengths and isinstance(content, str):
            plain_content = MARKER_RE.sub(r"\1", content).strip()
            if len(plain_content) < minimum_lengths[current_title]:
                errors.append(f"segments[{index}] 在 {current_title} 下内容过短")
            if "\n" in content or "\r" in content:
                errors.append(f"segments[{index}] content 必须是单行文本")
    if context:
        analysis = context.get("analysis_json") or {}
        item_count = len(analysis.get("items") or [])
        suggestion_count = len(analysis.get("health_suggestions") or [])
        if item_count >= 2 and body_counts[SECTION_TITLES[1]] < 2:
            errors.append("主要发现至少需要覆盖 2 个与问题相关的检测项")
        if suggestion_count >= 3 and body_counts[SECTION_TITLES[3]] < 3:
            errors.append("意见与建议必须完整覆盖 3 条分级建议")
    return list(dict.fromkeys(errors))


def structured_reply_to_text(payload: dict[str, Any]) -> str:
    reply = payload.get("reply") or {}
    segments = reply.get("segments") or []
    lines = [MARKER_RE.sub(r"\1", str(item.get("content", ""))) for item in segments if item.get("content")]
    return "\n".join(lines)


def _priority(item: dict[str, Any]) -> int:
    color = str(item.get("ui_color", "")).lower()
    status = str(item.get("status_label") or item.get("deviation") or "")
    if color == "red" or any(word in status for word in ("严重", "异常", "偏高", "偏低", "危急")):
        return 0
    if color in {"orange", "yellow"} or any(word in status for word in ("关注", "中度", "临界")):
        return 1
    if color == "blue" or "轻微" in status:
        return 2
    return 3


def compact_analysis(result: dict[str, Any], source_type: str) -> dict[str, Any]:
    meta = result.get("report_meta") or {}
    compact_meta = {
        key: meta.get(key)
        for key in ("category", "category_name", "report_type", "test_date", "hospital")
        if meta.get(key) is not None
    }
    source_items = result.get("indicators") if source_type == "report" else result.get("dimensions")
    items = [item for item in (source_items or []) if isinstance(item, dict)]
    items = sorted(items, key=_priority)
    allowed = (
        ("full_display", "ui_label", "ref_range", "ui_color", "deviation", "popular_science", "item_advice")
        if source_type == "report"
        else ("title", "status_label", "ui_color", "ai_analysis", "suggestion")
    )
    compact_items = [
        {key: item.get(key) for key in allowed if item.get(key) is not None}
        for item in items
    ]
    return {
        "report_meta": compact_meta,
        "ai_summary": result.get("ai_summary") or {},
        "items": compact_items,
        "health_suggestions": (result.get("health_suggestions") or [])[:3],
        "disclaimer": result.get("disclaimer"),
    }


def build_structured_context(
    record: AnalysisResultRecord,
    conversation: Conversation,
    user_question: str,
    history: list[Message],
    recent_results: list[AnalysisResultRecord],
) -> dict[str, Any]:
    pet = conversation.pet.model_dump(exclude_none=True)
    pet_profile = {
        "pet_name": pet.get("pet_name"),
        "breed": pet.get("breed"),
        "age": pet.get("age_years"),
        "weight": pet.get("weight_kg"),
        "gender": pet.get("sex"),
    }
    historical = []
    for item in recent_results:
        if item.result_id == record.result_id:
            continue
        meta = item.result.get("report_meta") or {}
        historical.append(
            {
                "date": meta.get("test_date"),
                "category": meta.get("category_name") or meta.get("report_type"),
                "summary": (item.result.get("ai_summary") or {}).get("summary"),
            }
        )
    conversation_history = [
        {"role": item.role, "content": item.text[:1000]}
        for item in history[-10:]
        if not (item.role == "user" and item.text == user_question)
    ]
    return {
        "source_type": record.source_type,
        "analysis_json": compact_analysis(record.result, record.source_type),
        "user_question": user_question,
        "pet_profile": {key: value for key, value in pet_profile.items() if value is not None},
        "history_summary": historical[-3:],
        "conversation_history": conversation_history,
    }


def _clean_term(value: Any, fallback: str, limit: int = 40) -> str:
    text = str(value or fallback).replace("[[", "").replace("]]", "").replace("\n", " ").strip()
    return text[:limit] or fallback


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").replace("\n", " ").strip()
    return text[:limit]


def _section(title: str) -> dict[str, Any]:
    return {"type": "section_title", "content": title, "highlights": []}


class FakeStructuredResponseAdapter:
    async def generate(self, context: dict[str, Any], _skill: SkillDefinition) -> dict[str, Any]:
        analysis = context["analysis_json"]
        pet_name = _clean_term(context.get("pet_profile", {}).get("pet_name"), "宝贝", 20)
        items = analysis.get("items") or []
        source_type = context["source_type"]
        findings: list[dict[str, Any]] = []
        focus_name = "本次结果"
        focus_status = _clean_term((analysis.get("ai_summary") or {}).get("severity"), "需结合观察", 20)

        for item_index, item in enumerate(items[:2]):
            if source_type == "report":
                name = _clean_term(item.get("full_display"), "检测指标")
                color = str(item.get("ui_color", "")).lower()
                status = _clean_term(
                    "异常" if color == "red" else "需关注" if color in {"yellow", "orange"} else "正常",
                    "已记录",
                )
                detail = "；".join(
                    part
                    for part in (
                        f"结果 {_clip(item.get('ui_label'), 35)}" if item.get("ui_label") else "",
                        f"参考范围 {_clip(item.get('ref_range'), 35)}" if item.get("ref_range") else "",
                        _clip(item.get("deviation") or item.get("popular_science"), 70),
                    )
                    if part
                )
            else:
                name = _clean_term(item.get("title"), "检测维度")
                status = _clean_term(item.get("status_label"), "已记录", 30)
                detail = _clip(item.get("ai_analysis"), 105)
            if item_index == 0:
                focus_name, focus_status = name, status
            content = (
                f"[[{name}]] 的状态已在结果中记录"
                if name == status
                else f"[[{name}]] 的状态为 [[{status}]]"
            )
            if detail:
                content += f"；{detail}"
            if len(MARKER_RE.sub(r"\1", content)) < 24:
                content += "；当前结果未记录额外异常证据，建议结合日常状态持续观察。"
            findings.append(
                {
                    "type": "list_item",
                    "content": content[:150],
                    "highlights": [name] if name == status else [name, status],
                }
            )

        if not findings:
            status = "未见异常"
            findings.append(
                {
                    "type": "list_item",
                    "content": f"现有分析中[[{status}]]，但仍建议结合日常状态持续观察。",
                    "highlights": [status],
                }
            )
            focus_status = status

        suggestions: list[dict[str, Any]] = []
        for item in (analysis.get("health_suggestions") or [])[:3]:
            title = _clean_term(item.get("title"), "持续观察", 35)
            content = f"[[{title}]]：{_clip(item.get('content'), 120)}"
            if len(MARKER_RE.sub(r"\1", content)) < 24:
                content += "请结合本次结果记录执行情况和后续变化。"
            suggestions.append({"type": "suggestion_item", "content": content[:150], "highlights": [title]})
        if not suggestions:
            title = "持续观察"
            suggestions.append(
                {
                    "type": "suggestion_item",
                    "content": f"[[{title}]]：记录精神、食欲和症状变化；若明显加重，请及时联系兽医。",
                    "highlights": [title],
                }
            )

        explanation = (
            f"可以把[[{focus_name}]]理解为本次检测的一个观察窗口；这一状态仍需结合日常表现和兽医检查理解。"
            if focus_name == focus_status
            else f"可以把[[{focus_name}]]理解为本次检测的一个观察窗口；当前结论是[[{focus_status}]]，仍需结合日常表现和兽医检查理解。"
        )
        explanation_highlights = [focus_name] if focus_name == focus_status else [focus_name, focus_status]
        segments = [
            _section(SECTION_TITLES[0]),
            {
                "type": "text",
                "content": f"你愿意继续了解{pet_name}的检测结果，已经是在很认真地照顾它了，我们按现有资料一起梳理。",
                "highlights": [],
            },
            _section(SECTION_TITLES[1]),
            *findings,
            _section(SECTION_TITLES[2]),
            {
                "type": "text",
                "content": explanation,
                "highlights": explanation_highlights,
            },
            _section(SECTION_TITLES[3]),
            *suggestions,
            _section(SECTION_TITLES[4]),
            {
                "type": "text",
                "content": "先按结果中的建议逐项观察即可；若情况加重或你仍不放心，请让兽医结合临床检查确认 🐾",
                "highlights": [],
            },
        ]
        return {
            "reply": {
                "emotion": "warm_empathy",
                "segments": segments,
                "suggested_questions": [
                    f"{focus_name}接下来重点观察什么？",
                    "什么变化出现时需要尽快就医？",
                ],
            }
        }


class OpenAICompatibleStructuredResponseAdapter:
    def __init__(self, settings: ModelSettings, schema: dict[str, Any]):
        self.settings = settings
        self.schema = schema

    async def generate(self, context: dict[str, Any], skill: SkillDefinition) -> dict[str, Any]:
        system = identity_system_prompt(
            (
            "你是 Fura-AI宠物管家检测结果追问执行器。必须严格执行下面的 structured-response Skill，"
            "只解释输入 analysis_json 中已有的事实，只返回合法 JSON。每个字符串字段最多150字且必须单行。"
            "回答要充分但不重复：若输入至少有2个检测项，主要发现至少写2条；"
            "若有3条分级建议，意见与建议必须完整写3条；每条都要包含输入中的具体依据或可执行动作。\n\n"
            + skill.content
            + "\n\n必须满足的 JSON Schema：\n"
            + json.dumps(self.schema, ensure_ascii=False)
            )
        )
        user = "请根据以下规范化输入生成结构化回答：\n" + json.dumps(context, ensure_ascii=False)
        candidate = await self._request(system, user)
        errors = validate_structured_response(candidate, self.schema, context)
        if errors:
            repair = (
                "上一个候选未通过校验。请只返回修正后的完整 JSON，不得增加输入中不存在的事实。"
                f"\n原始规范化输入：{json.dumps(context, ensure_ascii=False)}"
                f"\n校验错误：{json.dumps(errors, ensure_ascii=False)}"
                f"\n候选：{json.dumps(candidate, ensure_ascii=False)}"
            )
            candidate = await self._request(system, repair)
            errors = validate_structured_response(candidate, self.schema, context)
        if errors:
            raise StructuredResponseValidationError("；".join(errors[:8]))
        return candidate

    async def _request(self, system: str, user: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.2,
            "max_tokens": 5000,
            "response_format": {"type": "json_object"},
            "enable_thinking": False,
        }
        headers = {"Authorization": f"Bearer {self.settings.api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=20.0)) as client:
            response = await client.post(self.settings.chat_completions_url, headers=headers, json=payload)
            if response.status_code == 400 and "response_format" in response.text:
                payload.pop("response_format", None)
                response = await client.post(self.settings.chat_completions_url, headers=headers, json=payload)
            response.raise_for_status()
        choices = response.json().get("choices") or []
        if not choices:
            raise ValueError("结构化追问模型响应缺少 choices")
        return sanitize_model_payload(
            _extract_json(choices[0].get("message", {}).get("content")),
            self.settings.model,
        )


class StructuredResponseService:
    def __init__(self, workspace_root: Path, settings: ModelSettings | None = None):
        self.skill = SkillRegistry(workspace_root / "skill-definitions").get("structured-response")
        schema_path = self.skill.path.parent / "references" / "output-schema.json"
        self.schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.adapter = (
            OpenAICompatibleStructuredResponseAdapter(settings, self.schema)
            if settings is not None
            else FakeStructuredResponseAdapter()
        )

    async def generate(self, context: dict[str, Any]) -> dict[str, Any]:
        payload = await self.adapter.generate(context, self.skill)
        errors = validate_structured_response(payload, self.schema, context)
        if errors:
            raise StructuredResponseValidationError("；".join(errors[:8]))
        return payload
