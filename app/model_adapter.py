from __future__ import annotations

import base64
import io
import json
import mimetypes
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
from PIL import Image

from .config import ModelSettings, configured_video_fps
from .identity import identity_system_prompt, sanitize_model_payload
from .media import MediaArtifact
from .model_http import request_chat_completion, with_reasoning
from .schemas import PetContext
from .skill_loader import SkillDefinition
from .skill_prompt import runtime_skill_contract


OUTPUT_QUALITY_RULES = (
    "\n质量下限：不得用一句泛泛结论代替字段内容。summary 建议50-120字；"
    "每个 ai_analysis 或 popular_science 建议45-120字，必须写明素材中的位置、数值、状态或时间证据；"
    "每个 suggestion、item_advice 和 health_suggestions.content 建议25-100字，必须给出具体动作、观察重点或复查条件。"
    "所有字符串字段仍须单行且最多150字；证据不足时说明局限，禁止为满足长度编造事实。"
)


class ModelAdapter(Protocol):
    async def analyze(self, skill: SkillDefinition, media: MediaArtifact, pet: PetContext) -> dict[str, Any]: ...


def _data_url(path: Path) -> str:
    suffix = path.suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    with Image.open(path) as image:
        image = image.convert("RGB")
        image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=85, optimize=True)
    mime = "image/jpeg"
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _extract_json(content: str) -> dict[str, Any]:
    cleaned = content.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("模型响应中未找到 JSON 对象")
        return json.loads(cleaned[start : end + 1])


class OpenAICompatibleVisionAdapter:
    def __init__(self, settings: ModelSettings):
        self.settings = settings

    async def analyze(self, skill: SkillDefinition, media: MediaArtifact, pet: PetContext) -> dict[str, Any]:
        image_paths = [media.path] if media.type == "image" else media.keyframes
        if media.type == "video":
            observation = f"原始视频时长 {media.duration:.2f} 秒，按 20%、50%、80% 时间点抽取了 {len(image_paths)} 帧。"
        elif media.type == "pdf":
            observation = f"原始 PDF 已逐页转换为 {len(image_paths)} 张图像，首页面尺寸 {media.width}x{media.height}。必须综合全部页面识别指标。"
        else:
            observation = f"原始图片尺寸 {media.width}x{media.height}。"
        prompt = (
            f"请严格执行以下 Skill。只返回一个合法 JSON 对象，不要返回 Markdown。\n"
            f"宠物上下文：{pet.model_dump_json()}\n媒体信息：{observation}\n\n{runtime_skill_contract(skill)}"
            + OUTPUT_QUALITY_RULES
        )
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for index, path in enumerate(image_paths, start=1):
            if media.type == "video":
                timestamp_match = re.search(r"_(\d+(?:\.\d+)?)s$", path.stem)
                timestamp = timestamp_match.group(1) if timestamp_match else "未知"
                content.append({"type": "text", "text": f"关键帧 {index}，对应原视频约 {timestamp} 秒："})
            elif media.type == "pdf":
                content.append({"type": "text", "text": f"PDF 第 {index} 页："})
            content.append({"type": "image_url", "image_url": {"url": _data_url(path)}})
        payload = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": identity_system_prompt("你是宠物健康检测的结构化输出执行器。")},
                {"role": "user", "content": content},
            ],
            "temperature": 0.1,
            "max_tokens": 5000,
            "response_format": {"type": "json_object"},
        }
        payload = with_reasoning(payload, self.settings, default_budget=6144)
        message_content = await request_chat_completion(self.settings, payload, timeout_seconds=180.0)
        return sanitize_model_payload(_extract_json(str(message_content)), self.settings.model)

    async def repair_result(
        self,
        skill: SkillDefinition,
        candidate: dict[str, Any],
        validation_error: str,
        pet: PetContext,
        media: MediaArtifact | None = None,
    ) -> dict[str, Any]:
        """只在首轮输出违背 Skill 契约时，要求模型基于原结果做一次可审计修复。"""
        prompt = (
            "下面的候选结果没有通过 Fura-AI宠物管家的结构化校验。请修复后只返回一个合法 JSON 对象，"
            "不要返回 Markdown 或解释。必须保留可由素材支持的判断；不能为凑字数编造医学事实。"
            "每个文字字段最多150个字符，字段要具体、可执行，且严格遵守完整 Skill 的字段、枚举、顺序和免责声明。\n"
            f"宠物上下文：{pet.model_dump_json()}\n"
            f"校验错误：{validation_error}\n"
            f"候选 JSON：{json.dumps(candidate, ensure_ascii=False)}\n\n"
            f"完整 Skill 执行契约：\n{runtime_skill_contract(skill)}"
            + OUTPUT_QUALITY_RULES
        )
        user_content: str | list[dict[str, Any]] = prompt
        if media is not None:
            paths = media.keyframes if media.type in {"video", "pdf"} else [media.path]
            parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            for index, path in enumerate(paths, start=1):
                parts.append({"type": "text", "text": f"原始证据图 {index}："})
                parts.append({"type": "image_url", "image_url": {"url": _data_url(path)}})
            user_content = parts
        payload = {
            "model": self.settings.model,
            "messages": [
                {
                    "role": "system",
                    "content": identity_system_prompt("你是结构化输出修复器，只修复契约问题，不添加无证据的诊疗结论。"),
                },
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.05,
            "max_tokens": 6000,
            "response_format": {"type": "json_object"},
        }
        payload = with_reasoning(payload, self.settings, default_budget=4096)
        content = await request_chat_completion(self.settings, payload, timeout_seconds=180.0)
        return sanitize_model_payload(_extract_json(str(content or "")), self.settings.model)


def _video_data_url(path: Path, max_mb: int = 50) -> str:
    size = path.stat().st_size
    if size > max_mb * 1024 * 1024:
        raise ValueError(f"原生视频输入最大支持 {max_mb}MB，当前为 {size / 1024 / 1024:.1f}MB")
    mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


class QwenNativeVideoAdapter:
    """通过 OpenAI 兼容接口的 video_url 让 Qwen 原生理解完整视频。"""

    def __init__(self, settings: ModelSettings):
        self.settings = settings

    @staticmethod
    def fps_for_skill(skill_name: str) -> float:
        return configured_video_fps(
            10.0 if skill_name == "home-health-check-gait" else 4.0
        )

    def build_content(
        self, skill: SkillDefinition, media: MediaArtifact, pet: PetContext
    ) -> tuple[list[dict[str, Any]], float]:
        if media.type != "video":
            raise ValueError("QwenNativeVideoAdapter 仅接受视频")
        fps = self.fps_for_skill(skill.name)
        max_mb = int(os.getenv("AGENT_VIDEO_MAX_MB", "50"))
        prompt = (
            "请直接理解完整视频的连续时序、动作变化和异常出现时间点，并严格执行下方 Skill。"
            "不得把静态帧推断成精确动态指标；证据不足时必须降低确定性。"
            "只返回一个合法 JSON 对象，不要返回 Markdown。\n"
            f"宠物上下文：{pet.model_dump_json()}\n"
            f"视频元数据：时长 {media.duration:.2f} 秒，分辨率 {media.width}x{media.height}，帧率 {media.fps}。\n\n"
            f"{runtime_skill_contract(skill)}"
            + OUTPUT_QUALITY_RULES
        )
        video = {
            "type": "video_url",
            "video_url": {"url": _video_data_url(media.path, max_mb=max_mb), "fps": fps},
            "min_pixels": 65536,
            "max_pixels": 655360,
            "total_pixels": 67108864,
        }
        return [video, {"type": "text", "text": prompt}], fps

    async def analyze(self, skill: SkillDefinition, media: MediaArtifact, pet: PetContext) -> dict[str, Any]:
        content, fps = self.build_content(skill, media, pet)
        payload = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": identity_system_prompt("你是宠物兽医视频理解与结构化输出执行器。")},
                {"role": "user", "content": content},
            ],
            "temperature": 0.1,
            "max_tokens": 5000,
            "response_format": {"type": "json_object"},
        }
        payload = with_reasoning(payload, self.settings, default_budget=6144)
        message_content = await request_chat_completion(self.settings, payload, timeout_seconds=300.0)
        result = sanitize_model_payload(_extract_json(str(message_content or "")), self.settings.model)
        result.setdefault("report_meta", {})["analysis_runtime"] = {
            "video_provider": "qwen_native",
            "native_video": True,
            "fps": fps,
            "fallback": False,
        }
        return result


class FallbackVideoAdapter:
    def __init__(self, primary: QwenNativeVideoAdapter, fallback: OpenAICompatibleVisionAdapter):
        self.primary = primary
        self.fallback = fallback

    async def analyze(self, skill: SkillDefinition, media: MediaArtifact, pet: PetContext) -> dict[str, Any]:
        try:
            return await self.primary.analyze(skill, media, pet)
        except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as exc:
            result = await self.fallback.analyze(skill, media, pet)
            result.setdefault("report_meta", {})["analysis_runtime"] = {
                "video_provider": "ffmpeg_frames",
                "native_video": False,
                "fps": None,
                "fallback": True,
                "fallback_reason": type(exc).__name__,
            }
            return result


class FakeVisionAdapter:
    """用于稳定自动化测试；不声称真实理解了媒体内容。"""

    async def analyze(self, skill: SkillDefinition, media: MediaArtifact, pet: PetContext) -> dict[str, Any]:
        if skill.name == "pet-report-analysis":
            return self._report(media, pet)
        if skill.name == "home-health-check-gait":
            return self._home_check("gait", media, pet)
        if skill.name == "home-health-check-behavior":
            return self._home_check("behavior", media, pet)
        if skill.name == "home-health-check-dental":
            return self._home_check("dental", media, pet)
        if skill.name == "home-health-check-stool":
            return self._home_check("stool", media, pet)
        if skill.name == "home-health-check-xray":
            return self._home_check("xray", media, pet)
        raise ValueError(f"Fake Adapter 不支持 Skill: {skill.name}")

    @staticmethod
    def _report(media: MediaArtifact, pet: PetContext) -> dict[str, Any]:
        return {
            "report_meta": {
                "report_type": "生化检查",
                "test_date": "2025-06-30",
                "hospital": "Demo Mock 宠物医院",
                "pet": pet.model_dump(),
                "raw_images": [str(media.path)],
            },
            "ai_summary": {
                "severity": "中度",
                "severity_color": "Yellow",
                "summary": "演示结果：报告图片已进入完整多模态识别链路，共模拟识别2项指标，其中总蛋白一项需要重点关注并复核。",
            },
            "indicators": [
                {
                    "full_display": "GLU(葡萄糖)", "kind": "血糖", "ui_label": "7.48 mmol/L",
                    "ref_range": "3.95-8.84", "ui_color": "Green", "deviation": None,
                    "popular_science": "葡萄糖是机体细胞的重要能量来源，其变化通常需要结合进食时间、应激状态和其他指标理解。",
                    "item_advice": "保持当前饮食和作息，后续体检时继续对比该指标的变化趋势。",
                },
                {
                    "full_display": "TP(总蛋白)", "kind": "蛋白质", "ui_label": "91 g/L",
                    "ref_range": "57-89", "ui_color": "Red", "deviation": "当前值91，高于上限89。",
                    "popular_science": "总蛋白可辅助反映营养、肝脏合成及免疫相关状态，单项变化仍需结合白蛋白等指标综合判断。",
                    "item_advice": "建议携带原始报告咨询兽医，并按医嘱复查相关生化指标和近期饮水状态。",
                },
            ],
            "health_suggestions": _suggestions("复查异常指标", "结合临床症状复查生化指标"),
            "disclaimer": "以上分析由 AI 生成，仅供参考，不构成医疗诊断。如有疑虑，请咨询专业兽医。",
        }

    @staticmethod
    def _home_check(category: str, media: MediaArtifact, pet: PetContext) -> dict[str, Any]:
        duration = round(media.duration or 0, 2)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        settings = {
            "dental": ("牙科评估", ["牙结石评估", "牙龈健康", "口腔清洁度", "洁牙建议"]),
            "stool": ("便便分析", ["颜色分析", "形态评估", "质地特征", "消化健康总评"]),
            "gait": ("步态分析", ["步伐节律", "四肢协调性", "异常信号"]),
            "behavior": ("行为评估", ["情绪状态", "压力水平", "异常行为信号", "行为健康总评"]),
            "xray": ("X光片解读", ["影像信息", "骨骼结构", "关节评估", "软组织与体腔", "综合影像解读"]),
        }
        demo_statuses = {
            "dental": [("无", "green"), ("正常", "green"), ("清洁", "green"), ("暂不需要", "green")],
            "stool": [("正常", "green"), ("正常", "green"), ("正常", "green"), ("良好", "green")],
            "gait": [("正常", "green"), ("正常", "green"), ("未发现异常", "green")],
            "behavior": [("愉悦", "green"), ("低", "green"), ("未发现异常", "green"), ("良好", "green")],
            "xray": [("", "green"), ("未见异常", "green"), ("未见异常", "green"), ("不适用", "green"), ("未见明显异常", "green")],
        }
        demo_analyses = {
            "dental": [
                "图片中牙面与犬齿区域已进入牙根沉积特征检查流程，演示模式不作医学判断。",
                "图片中牙龈颜色和边缘已进入红肿出血检查流程，演示模式不作医学判断。",
                "图片中牙齿表面污渍与食物残留已进入清洁度检查流程，演示模式不作医学判断。",
                "综合前三项牙龈与清洁度字段生成洁牙建议，演示模式不作医学判断。",
            ],
            "stool": [
                "图片中粪便颜色已进入棕黄、黑、红、绿等范围检查流程，演示模式不作医学判断。",
                "图片中粪便形态与是否成形已进入检查流程，演示模式不作医学判断。",
                "图片中表面质地、黏液、血丝和异物已进入检查流程，演示模式不作医学判断。",
                "综合颜色、形态和质地字段生成消化健康总评，演示模式不作医学判断。",
            ],
            "gait": [
                "视频全程已进入四肢落地节律与步频检查流程，演示模式不作医学判断。",
                "视频全程已进入四肢抬腿幅度、承重和协调性检查流程，演示模式不作医学判断。",
                "视频全程已进入跛行、不对称和拖曳信号复核流程，演示模式标记为未发现异常。",
            ],
            "behavior": [
                "视频全程已进入耳朵、尾巴、眼神和身体姿态等情绪信号检查流程。",
                "视频全程已进入舔鼻、哈欠、视线回避、身体僵硬和压力变化检查流程。",
                "视频全程已进入呼吸、皮肤、神经和强迫行为复核流程，演示模式标记为未发现异常。",
                "综合视频全程的情绪、压力和异常行为字段生成行为健康总评。",
            ],
            "xray": [
                "影像质量与拍摄部位已进入结构化检查流程，演示模式不作医学判断。",
                "影像中的骨骼密度与皮质骨边缘已进入检查流程，演示模式不作医学判断。",
                "影像中的关节间隙与关节面已进入检查流程，演示模式不作医学判断。",
                "影像为局部素材时软组织与体腔维度可标记为不适用，演示模式不作判断。",
                "从影像来看当前仅完成演示链路与结构化字段检查，不能替代真实影像解读和兽医判断。",
            ],
        }
        category_name, dimension_titles = settings[category]
        media_description = f"{duration}秒视频已完成关键帧抽取" if media.type == "video" else "图片已完成清晰度和格式校验"
        return {
            "report_meta": {
                "category": category, "category_name": category_name, "test_date": now,
                "pet": pet.model_dump(),
                "media": {
                    "type": media.type, "url": str(media.path),
                    "thumbnail_url": str(media.keyframes[0] if media.keyframes else media.path),
                    "duration": duration if media.type == "video" else None,
                },
            },
            "ai_summary": {
                "severity": "轻度", "severity_color": "green",
                "summary": f"演示结果：{media_description}，当前未进行真实医学判断。",
            },
            "dimensions": [
                {
                    "title": title,
                    "status_label": demo_statuses[category][index][0],
                    "ui_color": demo_statuses[category][index][1],
                    "ai_analysis": demo_analyses[category][index],
                    "suggestion": "使用 Fura-AI宠物管家服务结合专业证据复核。",
                }
                for index, title in enumerate(dimension_titles)
            ],
            "health_suggestions": _suggestions("完成真实分析", f"使用真实模式复核本次{category_name}素材"),
            "disclaimer": (
                "本解读由AI生成，仅供参考，不构成医疗诊断建议，请结合兽医专业意见综合判断"
                if category == "xray"
                else "以上分析由 AI 生成，仅供参考，不构成医疗诊断。如有疑虑，请咨询专业兽医。"
            ),
        }


def _suggestions(high_title: str, high_content: str) -> list[dict[str, str]]:
    return [
        {
            "ui_label": "PRIORITY_高",
            "ui_color": "blue",
            "title": high_title,
            "content": f"{high_content}，并结合宠物近期精神、食欲和活动变化综合判断。",
        },
        {
            "ui_label": "PRIORITY_中",
            "ui_color": "blue",
            "title": "持续观察变化",
            "content": "连续记录精神、食欲、饮水、排便和活动状态，出现明显变化时及时联系兽医。",
        },
        {
            "ui_label": "PRIORITY_低",
            "ui_color": "blue",
            "title": "安排定期复测",
            "content": "妥善保留本次结果和原始素材，按建议时间复测并与当前记录进行趋势对比。",
        },
    ]
