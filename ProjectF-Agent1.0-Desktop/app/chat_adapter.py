from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Protocol

import httpx

from .config import ModelSettings
from .identity import identity_system_prompt
from .schemas import Conversation, Message


class ChatAdapter(Protocol):
    async def stream_reply(
        self, conversation: Conversation, history: list[Message], user_text: str, summary: str | None = None
    ) -> AsyncIterator[str]: ...


def _chunks(text: str, size: int = 12) -> list[str]:
    return [text[index : index + size] for index in range(0, len(text), size)]


def _stream_content_delta(event: dict) -> str | None:
    """兼容包含空 choices 的 usage 事件及多段 content。"""
    choices = event.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return None
    delta = choices[0].get("delta") or {}
    content = delta.get("content")
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        parts = [item.get("text", "") for item in content if isinstance(item, dict)]
        joined = "".join(str(part) for part in parts)
        return joined or None
    return None


class FakeCaretakerAdapter:
    """稳定的本地宠物管家，用于 UI 联调和安全规则测试。"""

    EMERGENCY_TERMS = {
        "呼吸困难",
        "抽搐",
        "昏迷",
        "中毒",
        "大出血",
        "无法排尿",
        "站不起来",
    }

    async def stream_reply(
        self, conversation: Conversation, history: list[Message], user_text: str, summary: str | None = None
    ) -> AsyncIterator[str]:
        del history, summary
        pet_name = conversation.pet.pet_name or "宝贝"
        if any(term in user_text for term in self.EMERGENCY_TERMS):
            reply = (
                f"{pet_name}现在的情况可能属于急症。请立即联系附近宠物医院或急诊，并在确保安全的前提下尽快就医；"
                "不要自行喂药或强行喂水。途中记录症状开始时间、可能接触的物品和既往病史，便于兽医判断。"
            )
        elif any(term in user_text for term in ("报告", "化验单", "血常规", "生化")):
            reply = (
                f"我可以陪你一起看{pet_name}的报告。为了避免把普通图片问答当成专业检测，请从“报告检测”入口选择分类并上传清晰完整的报告；"
                "完成后可以在这个会话里继续追问指标含义。AI 解读仅供参考，不能替代兽医诊断。"
            )
        elif any(term in user_text for term in ("走路", "跛", "步态", "一瘸一拐")):
            reply = (
                f"先减少{pet_name}的奔跑、跳跃和爬楼，并观察哪条腿异常、是否疼痛、是否突然发生。"
                "可以从“居家检测—步态分析”入口上传侧面和正面的连续行走视频；若无法承重、明显疼痛或伴随外伤，请尽快就医。"
            )
        else:
            profile = ""
            if conversation.pet.breed:
                profile = f"结合{pet_name}是{conversation.pet.breed}这一点，"
            reply = (
                f"我在，咱们一起看看{pet_name}。{profile}请告诉我症状从什么时候开始、精神和食欲如何、饮水排便有没有变化，"
                "以及最近是否更换食物、药物或生活环境。我会帮你整理需要观察的重点；如果症状快速加重，请及时联系兽医。"
            )
        for chunk in _chunks(reply):
            yield chunk


class OpenAICompatibleChatAdapter:
    def __init__(self, settings: ModelSettings):
        self.settings = settings

    async def stream_reply(
        self, conversation: Conversation, history: list[Message], user_text: str, summary: str | None = None
    ) -> AsyncIterator[str]:
        pet = conversation.pet.model_dump(exclude_none=True)
        system = identity_system_prompt(
            (
            "你是管家：温暖、谨慎、实用。你可以提供养宠信息和观察建议，但不得下确定医疗诊断，"
            "不得编造检查结果。出现呼吸困难、抽搐、昏迷、中毒、大出血、无法排尿等急症信号时，优先建议立即就医。"
            "如果用户想分析报告、牙齿、便便、步态、行为或 X 光，只推荐对应产品入口，不自行执行专业检测。"
            "最终回答必须是单段纯文本，不得换行，总长度不得超过150个中文字符。"
            "在该长度内优先给出“当前判断/已知依据/下一步”三个信息，直接回应用户问题；"
            "避免泛泛安慰、重复免责声明或只说‘观察一下’。若有已完成检测，只能解释其已知结论并说明证据边界。"
            f"当前宠物档案：{json.dumps(pet, ensure_ascii=False)}"
            )
        )
        if summary:
            system += f"\n会话摘要与可信检测上下文：{summary}"
        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        for message in history[-12:]:
            messages.append({"role": message.role, "content": message.text[:2000]})
        if not history or history[-1].text != user_text:
            messages.append({"role": "user", "content": user_text})
        payload = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": 360,
            "stream": True,
        }
        headers = {"Authorization": f"Bearer {self.settings.api_key}"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=20.0)) as client:
            async with client.stream(
                "POST", self.settings.chat_completions_url, headers=headers, json=payload
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    event = json.loads(data)
                    delta = _stream_content_delta(event)
                    if delta:
                        yield str(delta)
