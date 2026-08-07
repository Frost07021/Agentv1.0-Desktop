from __future__ import annotations

import asyncio
import base64
import io
import json
import mimetypes
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
import yaml
from PIL import Image

from .config import ModelSettings
from .identity import identity_system_prompt, sanitize_model_payload
from .media import MediaArtifact, MediaProcessor
from .schemas import PetContext
from .skill_loader import SkillDefinition
from .skill_prompt import runtime_skill_contract


StepRunner = Callable[[str, Callable[[], Awaitable[Any]]], Awaitable[Any]]

OUTPUT_QUALITY_RULES = (
    "\n质量下限：summary 建议50-120字；每个 ai_analysis 建议45-120字并引用位置、特征、时间或频次证据；"
    "每个 suggestion 和 health_suggestions.content 建议25-100字并给出具体动作、观察重点或复查条件。"
    "所有字符串字段必须单行且最多150字；证据不足时明确局限，禁止编造不可见事实。"
)


def extract_visual_prompt(skill: SkillDefinition) -> str:
    """从 Skill 第三节读取插件必须使用的原始视觉 Prompt。"""
    markers = list(re.finditer(r"\*\*视觉识别 Prompt 指令\*\*[^\n]*\n", skill.content))
    if not markers:
        raise ValueError(f"{skill.name} 缺少视觉识别 Prompt 指令")
    marker = markers[-1]
    fenced = re.search(r"```(?:text)?\s*\n(.*?)\n```", skill.content[marker.end() :], re.DOTALL)
    if not fenced:
        raise ValueError(f"{skill.name} 的视觉识别 Prompt 未使用代码块定义")
    prompt = fenced.group(1).strip()
    if not prompt:
        raise ValueError(f"{skill.name} 的视觉识别 Prompt 为空")
    return prompt


def _image_data_url(path: Path) -> str:
    with Image.open(path) as image:
        image = image.convert("RGB")
        image.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=88, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _video_data_url(path: Path, max_size_mb: int) -> str:
    size = path.stat().st_size
    if size > max_size_mb * 1024 * 1024:
        raise ValueError(f"原生视频输入最大支持 {max_size_mb}MB，当前为 {size / 1024 / 1024:.1f}MB")
    mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _extract_json(content: Any) -> dict[str, Any]:
    if isinstance(content, list):
        content = "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    cleaned = re.sub(r"^```(?:json)?\s*", "", str(content or "").strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("模型响应中未找到 JSON 对象")
        value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("模型响应必须是 JSON 对象")
    return value


class QwenJsonClient:
    def __init__(self, settings: ModelSettings):
        self.settings = settings

    async def complete(
        self,
        content: str | list[dict[str, Any]],
        *,
        system: str,
        timeout_seconds: float,
        max_tokens: int = 6000,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [{"role": "system", "content": identity_system_prompt(system)}, {"role": "user", "content": content}],
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "enable_thinking": False,
        }
        headers = {"Authorization": f"Bearer {self.settings.api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds, connect=20.0)) as client:
            response = await client.post(self.settings.chat_completions_url, headers=headers, json=payload)
            if response.status_code == 400 and "response_format" in response.text:
                payload.pop("response_format", None)
                response = await client.post(self.settings.chat_completions_url, headers=headers, json=payload)
            response.raise_for_status()
        choices = response.json().get("choices") or []
        if not choices:
            raise ValueError("模型响应缺少 choices")
        return sanitize_model_payload(
            _extract_json(choices[0].get("message", {}).get("content")),
            self.settings.model,
        )


class HomeCheckWorkflow:
    """严格执行各居家检测 Skill 中声明的多模态插件步骤。"""

    def __init__(self, workspace_root: Path, settings: ModelSettings, media_processor: MediaProcessor):
        config_path = workspace_root / "config" / "home-check-plugins.yaml"
        self.config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        self.client = QwenJsonClient(settings)
        self.media_processor = media_processor

    async def execute(
        self,
        skill: SkillDefinition,
        media: MediaArtifact,
        pet: PetContext,
        run_step: StepRunner,
    ) -> dict[str, Any]:
        profile = self.config["skills"].get(skill.name)
        if not profile:
            raise ValueError(f"居家检测插件配置不存在: {skill.name}")
        if media.type not in profile["media"]:
            raise ValueError(f"{skill.name} 插件不接受 {media.type}")
        prompt = extract_visual_prompt(skill)

        if media.type == "video":
            return await self._execute_video(skill, media, pet, profile, prompt, run_step)
        if media.type == "pdf":
            observations = await run_step(
                "home.step1.pdf_page_understanding",
                lambda: self._observe_images(skill, media.keyframes, prompt, pet, page_mode=True),
            )
            result = await run_step(
                "home.step2.result_composition",
                lambda: self._compose(skill, media, pet, {"pdf_page_observations": observations}),
            )
            result["_workflow_evidence"] = {"pdf_page_observations": observations}
            return result
        if "result_composer" in profile.get("workflow", []):
            observations = await run_step(
                "home.step1.image_understanding",
                lambda: self._observe_images(skill, [media.path], prompt, pet),
            )
            result = await run_step(
                "home.step2.result_composition",
                lambda: self._compose(skill, media, pet, {"image_observations": observations}),
            )
            result["_workflow_evidence"] = {"image_observations": observations}
            return result
        result = await run_step(
            "home.step1.image_understanding",
            lambda: self._analyze_single_image(skill, media.path, prompt, pet),
        )
        result["_workflow_evidence"] = {"direct_image_analysis": True}
        return result

    async def _execute_video(
        self,
        skill: SkillDefinition,
        media: MediaArtifact,
        pet: PetContext,
        profile: dict[str, Any],
        prompt: str,
        run_step: StepRunner,
    ) -> dict[str, Any]:
        video_observation = await run_step(
            "home.step1.video_understanding",
            lambda: self._understand_video(skill, media, pet, prompt, float(profile["video_fps"])),
        )
        timestamps = self._select_timestamps(video_observation, media.duration or 0.0)
        frames = await run_step(
            "home.step2.evidence_frame_extraction",
            lambda: asyncio.to_thread(self.media_processor.extract_frames, media, timestamps, "evidence"),
        )
        media.keyframes = frames
        frame_observations = await run_step(
            "home.step2.keyframe_understanding",
            lambda: self._observe_images(skill, frames, prompt, pet, timestamps=timestamps),
        )
        evidence = {
            "video_observation": video_observation,
            "keyframe_observations": frame_observations,
            "selected_timestamps": timestamps,
        }
        result = await run_step(
            "home.step3.result_composition",
            lambda: self._compose(skill, media, pet, evidence),
        )
        runtime_provider = video_observation.get("runtime_provider", "qwen_native_video")
        native_video = runtime_provider == "qwen_native_video"
        result.setdefault("report_meta", {})["analysis_runtime"] = {
            "video_provider": runtime_provider,
            "native_video": native_video,
            "analysis_quality": "full_video" if native_video else "degraded_dense_storyboard",
            "fps": float(profile["video_fps"]),
            "keyframe_timestamps": timestamps,
            "storyboard_frame_count": video_observation.get("storyboard_frame_count", 0),
            "native_attempts": video_observation.get("native_attempts", 1),
            "fallback_reason_code": video_observation.get("fallback_reason_code"),
            "skill_prompt_source": "skill.visual_recognition_prompt",
            "skill_steps_completed": [1, 2, 3],
        }
        result["_workflow_evidence"] = evidence
        return result

    async def repair_result(
        self,
        skill: SkillDefinition,
        media: MediaArtifact,
        pet: PetContext,
        candidate: dict[str, Any],
        validation_error: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """Repair a rejected result while keeping the original multimodal evidence in context."""
        instruction = (
            "候选结果未通过 Fura-AI宠物管家校验。请依据同一素材与工作流证据修复完整 JSON；"
            "不得新增证据中不存在的医学事实，不得改变有明确视觉依据的数值、位置或时间。"
            f"\n完整 Skill 执行契约：\n{runtime_skill_contract(skill)}"
            + OUTPUT_QUALITY_RULES
            + f"\n宠物上下文：{pet.model_dump_json()}"
            + f"\n校验错误：{validation_error}"
            + f"\n工作流证据：{json.dumps(evidence, ensure_ascii=False)}"
            + f"\n候选 JSON：{json.dumps(candidate, ensure_ascii=False)}"
            + "\n修复时以工作流证据为唯一事实源，Skill 中的格式说明不得覆盖真实观察。"
        )
        paths = media.keyframes if media.type in {"video", "pdf"} else [media.path]
        content: list[dict[str, Any]] = [{"type": "text", "text": instruction}]
        for index, path in enumerate(paths, start=1):
            content.append({"type": "text", "text": f"原始证据图 {index}："})
            content.append({"type": "image_url", "image_url": {"url": _image_data_url(path)}})
        return await self.client.complete(
            content,
            system="你是 Fura-AI宠物管家的多模态结构化结果修复器，必须保持素材、证据与最终字段闭环。",
            timeout_seconds=float(self.config["plugins"]["result_composer"]["timeout_seconds"]),
        )

    async def _understand_video(
        self,
        skill: SkillDefinition,
        media: MediaArtifact,
        pet: PetContext,
        prompt: str,
        fps: float,
    ) -> dict[str, Any]:
        settings = self.config["plugins"]["video_understanding"]
        native_timeout = float(settings.get("native_timeout_seconds", 150))
        native_max_attempts = max(1, int(settings.get("native_max_attempts", 2)))
        fallback_timeout = float(settings.get("fallback_timeout_seconds", 90))
        contract = (
            "\n\n这是 Skill Step 1。只返回 JSON："
            '{"overall_description":"","timeline":[{"start_seconds":0,"end_seconds":0,'
            '"signal":"","evidence":"","confidence":"low|medium|high"}],' 
            '"keyframe_timestamps":[0.0,0.0,0.0],"limitations":[]}。'
            "timeline 必须按时间顺序覆盖视频开头至结尾，正常与异常片段都要记录；"
            "overall_description 必须基于连续视频，不得只复述关键帧；"
            "keyframe_timestamps 必须给出 2-3 个最值得在 Step 2 复核的时间点。"
            f"\n宠物上下文：{pet.model_dump_json()}"
            f"\n视频时长：{media.duration:.2f} 秒；分辨率：{media.width}x{media.height}。"
        )
        native_content = [
            {
                "type": "video_url",
                "video_url": {
                    "url": _video_data_url(media.path, int(settings["max_size_mb"])),
                    "fps": fps,
                },
                "min_pixels": 65536,
                "max_pixels": 262144,
                "total_pixels": 33554432,
            },
            {"type": "text", "text": prompt + contract},
        ]
        native_errors: list[Exception] = []
        for attempt in range(1, native_max_attempts + 1):
            try:
                result = await self.client.complete(
                    native_content,
                    system="你是 Fura-AI宠物管家的视频理解插件，只执行 Skill Step 1，不生成最终报告。",
                    timeout_seconds=native_timeout,
                    max_tokens=2000,
                )
                result["runtime_provider"] = "qwen_native_video"
                result["native_attempts"] = attempt
                return result
            except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as exc:
                native_errors.append(exc)
                # Native video responses can be transiently slow even for the same input.
                # Retry every supported transport/timeout/response failure once before
                # falling back, so a single read timeout cannot silently lower quality.
                if attempt >= native_max_attempts:
                    break
                await asyncio.sleep(0.5 * attempt)

        last_error = native_errors[-1]
        fallback_count = max(6, int(settings.get("fallback_frame_count", 12)))
        storyboard_timestamps = self.media_processor.uniform_timestamps(media.duration or 0.0, fallback_count)
        storyboard_frames = await asyncio.to_thread(
            self.media_processor.extract_frames,
            media,
            storyboard_timestamps,
            "storyboard",
        )
        storyboard: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    prompt
                    + contract
                    + f"\n原生视频理解不可用。以下 {len(storyboard_frames)} 帧按时间顺序覆盖整段视频，"
                    "必须结合相邻帧变化分析时序，不得把单帧姿态当成持续动态结论。"
                ),
            }
        ]
        for index, (timestamp, frame) in enumerate(zip(storyboard_timestamps, storyboard_frames), start=1):
            storyboard.append({"type": "text", "text": f"顺序帧 {index}，原视频 {timestamp:.3f} 秒："})
            storyboard.append({"type": "image_url", "image_url": {"url": _image_data_url(frame)}})
        result = await self.client.complete(
            storyboard,
            system="你是 Fura-AI宠物管家的视频密集顺序帧降级理解插件，只执行 Skill Step 1。",
            timeout_seconds=fallback_timeout,
            max_tokens=2400,
        )
        reason_code = self._native_failure_code(last_error)
        result["runtime_provider"] = "ffmpeg_dense_storyboard"
        result["native_attempts"] = len(native_errors)
        result["fallback_reason_code"] = reason_code
        result["storyboard_frame_count"] = len(storyboard_frames)
        result["storyboard_timestamps"] = storyboard_timestamps
        result.setdefault("limitations", []).append(
            f"完整视频理解未完成，已使用覆盖全时段的 {len(storyboard_frames)} 帧顺序分析（{reason_code}）。"
        )
        return result

    @staticmethod
    def _native_failure_code(error: Exception) -> str:
        if isinstance(error, httpx.TimeoutException):
            return "native_timeout"
        if isinstance(error, httpx.HTTPStatusError):
            return f"native_http_{error.response.status_code}"
        if isinstance(error, httpx.HTTPError):
            return "native_transport_error"
        return "native_response_error"

    async def _observe_images(
        self,
        skill: SkillDefinition,
        paths: list[Path],
        prompt: str,
        pet: PetContext,
        *,
        timestamps: list[float] | None = None,
        page_mode: bool = False,
    ) -> list[dict[str, Any]]:
        if not paths:
            raise ValueError("关键帧/分页图片为空，不能跳过图像复核")
        results: list[dict[str, Any]] = []
        retries = int(self.config["plugins"]["image_understanding"]["retries"])
        for index, path in enumerate(paths):
            if page_mode:
                label = f"PDF 第 {index + 1} 页"
            elif timestamps is not None:
                label = f"原视频 {timestamps[index]:.3f} 秒关键帧"
            else:
                label = f"原始图片 {index + 1}"
            last_error: Exception | None = None
            for attempt in range(retries + 1):
                try:
                    results.append(await self._observe_image(path, prompt, pet, label, skill.name))
                    break
                except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as exc:
                    last_error = exc
                    if attempt >= retries:
                        raise
            if last_error is not None and len(results) <= index:
                raise last_error
        return results

    async def _observe_image(
        self, path: Path, prompt: str, pet: PetContext, label: str, skill_name: str
    ) -> dict[str, Any]:
        suffix = (
            f"\n\n当前素材：{label}。宠物上下文：{pet.model_dump_json()}。"
            "这是证据复核步骤，只描述画面可见事实；动态指标若无法由单帧确认，必须写入 limitations。"
            "只返回 JSON："
            '{"source":"","observations":[{"finding":"","visual_evidence":"",'
            '"confidence":"low|medium|high"}],"limitations":[]}。'
        )
        content = [
            {"type": "image_url", "image_url": {"url": _image_data_url(path)}},
            {"type": "text", "text": prompt + suffix},
        ]
        result = await self.client.complete(
            content,
            system=f"你是 Fura-AI宠物管家的 {skill_name} 图像理解插件。",
            timeout_seconds=float(self.config["plugins"]["image_understanding"]["timeout_seconds"]),
            max_tokens=3500,
        )
        result.setdefault("source", label)
        return result

    async def _analyze_single_image(
        self, skill: SkillDefinition, path: Path, prompt: str, pet: PetContext
    ) -> dict[str, Any]:
        instruction = (
            "严格执行以下 Skill 契约，只返回一个合法 JSON 对象，不要返回 Markdown。"
            + f"\n完整 Skill 执行契约：\n{runtime_skill_contract(skill)}"
            + OUTPUT_QUALITY_RULES
            + "\n\n本次视觉识别任务必须逐项执行：\n"
            + prompt
            + "\n将视觉分析直接映射为 Skill 要求的最终 JSON，不经过脱离素材的中间扩写。"
            + f"\n宠物上下文：{pet.model_dump_json()}"
            + "\n最终结论必须以当前图片中的可见证据为唯一事实源。"
        )
        content = [
            {"type": "image_url", "image_url": {"url": _image_data_url(path)}},
            {"type": "text", "text": instruction},
        ]
        return await self.client.complete(
            content,
            system="你是 Fura-AI宠物管家的居家图片检测插件，必须直接生成 Skill 规定的结果 JSON。",
            timeout_seconds=float(self.config["plugins"]["image_understanding"]["timeout_seconds"]),
        )

    async def _compose(
        self,
        skill: SkillDefinition,
        media: MediaArtifact,
        pet: PetContext,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        prompt = (
            "这是 Skill 的最终综合步骤。仅使用给定多模态证据生成结果；证据冲突时降低确定性，"
            "不得编造不可见的精确数值。只返回一个合法 JSON 对象，不要返回 Markdown。"
            f"\n完整 Skill 执行契约：\n{runtime_skill_contract(skill)}"
            + OUTPUT_QUALITY_RULES
            + f"\n宠物上下文：{pet.model_dump_json()}"
            + f"\n媒体：type={media.type}, duration={media.duration}"
            + f"\n多模态证据：{json.dumps(evidence, ensure_ascii=False)}"
            + "\n证据使用规则：视频时序结论以 video_observation 为准，关键帧只复核静态细节；"
            + "不得因只有2-3张复核帧而声称未分析完整视频。降级时必须依据密集顺序帧的完整时间轴并明确局限。"
        )
        return await self.client.complete(
            prompt,
            system="你是 Fura-AI宠物管家的证据汇总与结构化结果插件。",
            timeout_seconds=float(self.config["plugins"]["result_composer"]["timeout_seconds"]),
        )

    @staticmethod
    def _select_timestamps(observation: dict[str, Any], duration: float) -> list[float]:
        values = observation.get("keyframe_timestamps") or []
        selected: list[float] = []
        end = max(0.0, duration - 0.05)
        for value in values:
            try:
                timestamp = round(max(0.0, min(end, float(value))), 3)
            except (TypeError, ValueError):
                continue
            if timestamp not in selected:
                selected.append(timestamp)
            if len(selected) == 3:
                break
        for ratio in (0.2, 0.5, 0.8):
            if len(selected) >= 3:
                break
            fallback = round(min(end, max(0.0, duration * ratio)), 3)
            if fallback not in selected:
                selected.append(fallback)
        if len(selected) < 2:
            selected.append(round(end, 3))
        return selected[:3]
