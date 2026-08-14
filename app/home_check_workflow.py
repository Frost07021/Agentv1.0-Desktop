from __future__ import annotations

import asyncio
import base64
import copy
import io
import json
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlsplit

import httpx
import yaml
from PIL import Image

from .config import ModelSettings, configured_video_fps
from .identity import identity_system_prompt, sanitize_model_payload
from .media import MediaArtifact, MediaProcessor
from .model_http import request_chat_completion, with_reasoning
from .schemas import PetContext
from .skill_loader import SkillDefinition
from .skill_prompt import runtime_skill_contract


StepRunner = Callable[[str, Callable[[], Awaitable[Any]]], Awaitable[Any]]

OUTPUT_QUALITY_RULES = (
    "\n质量下限：summary 建议50-120字；每个 ai_analysis 建议45-120字并引用位置、特征、时间或频次证据；"
    "每个 suggestion 和 health_suggestions.content 建议25-100字并给出具体动作、观察重点或复查条件。"
    "当各维度均正常或未发现异常时，只给出日常观察、环境安全、体重管理或复测建议；"
    "不得在缺少异常证据和兽医意见时主动推荐药物、营养品、保健品或补充剂。"
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


def _video_source_url(path: Path, max_size_mb: int) -> tuple[str, str]:
    """Resolve the Qwen ``video_url`` without coupling analysis to storage.

    The desktop default remains an inline data URL. Deployments that already
    expose a local directory through HTTPS can opt into a small request body by
    setting both ``AGENT_VIDEO_PUBLIC_ROOT`` and
    ``AGENT_VIDEO_PUBLIC_URL_PREFIX``. This deliberately does not upload or
    publish files on its own.
    """
    size = path.stat().st_size
    if size > max_size_mb * 1024 * 1024:
        raise ValueError(f"原生视频输入最大支持 {max_size_mb}MB，当前为 {size / 1024 / 1024:.1f}MB")

    public_root_raw = os.getenv("AGENT_VIDEO_PUBLIC_ROOT", "").strip()
    public_prefix = os.getenv("AGENT_VIDEO_PUBLIC_URL_PREFIX", "").strip()
    if not public_root_raw and not public_prefix:
        return _video_data_url(path, max_size_mb), "base64_data_url"
    if not public_root_raw or not public_prefix:
        raise ValueError(
            "公网视频输入必须同时配置 AGENT_VIDEO_PUBLIC_ROOT 和 "
            "AGENT_VIDEO_PUBLIC_URL_PREFIX"
        )

    parsed = urlsplit(public_prefix)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("AGENT_VIDEO_PUBLIC_URL_PREFIX 必须是无查询参数的 HTTP(S) 地址")
    public_root = Path(public_root_raw).expanduser().resolve()
    source = path.resolve()
    try:
        relative = source.relative_to(public_root)
    except ValueError as exc:
        raise ValueError(f"原生视频不在公网映射目录内: {source}") from exc
    public_url = public_prefix.rstrip("/") + "/" + quote(relative.as_posix(), safe="/")
    return public_url, "public_url"


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
        self._http_client: httpx.AsyncClient | None = None
        self._last_request_diagnostics: dict[str, Any] = {}

    async def __aenter__(self) -> QwenJsonClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=None,
                limits=httpx.Limits(
                    max_connections=8,
                    max_keepalive_connections=4,
                    keepalive_expiry=60.0,
                ),
            )
        return self

    async def __aexit__(self, *_args: Any) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def complete(
        self,
        content: str | list[dict[str, Any]],
        *,
        system: str,
        timeout_seconds: float,
        max_tokens: int = 6000,
        reasoning_budget: int = 4096,
        connect_timeout_seconds: float = 20.0,
        write_timeout_seconds: float | None = None,
        pool_timeout_seconds: float = 20.0,
        total_timeout_seconds: float | None = None,
        request_diagnostics: dict[str, Any] | None = None,
        first_sse_event: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [{"role": "system", "content": identity_system_prompt(system)}, {"role": "user", "content": content}],
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        payload = with_reasoning(payload, self.settings, default_budget=reasoning_budget)
        diagnostics = copy.deepcopy(request_diagnostics or {})
        self._last_request_diagnostics = diagnostics
        try:
            response_content = await request_chat_completion(
                self.settings,
                payload,
                timeout_seconds=timeout_seconds,
                client=self._http_client,
                connect_timeout_seconds=connect_timeout_seconds,
                write_timeout_seconds=write_timeout_seconds,
                pool_timeout_seconds=pool_timeout_seconds,
                total_timeout_seconds=total_timeout_seconds,
                diagnostics=diagnostics,
                first_sse_event=first_sse_event,
            )
        finally:
            if request_diagnostics is not None:
                request_diagnostics.clear()
                request_diagnostics.update(copy.deepcopy(diagnostics))
        return sanitize_model_payload(
            _extract_json(response_content),
            self.settings.model,
        )

    @property
    def last_request_diagnostics(self) -> dict[str, Any]:
        return copy.deepcopy(self._last_request_diagnostics)


class HomeCheckWorkflow:
    """严格执行各居家检测 Skill 中声明的多模态插件步骤。"""

    def __init__(
        self,
        workspace_root: Path,
        settings: ModelSettings,
        media_processor: MediaProcessor,
        native_video_semaphore: asyncio.Semaphore | None = None,
        dense_video_semaphore: asyncio.Semaphore | None = None,
    ):
        config_path = workspace_root / "config" / "home-check-plugins.yaml"
        self.config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        self.client = QwenJsonClient(settings)
        self.media_processor = media_processor
        self.native_video_semaphore = native_video_semaphore
        self.dense_video_semaphore = dense_video_semaphore

    async def execute(
        self,
        skill: SkillDefinition,
        media: MediaArtifact,
        pet: PetContext,
        run_step: StepRunner,
    ) -> dict[str, Any]:
        # The production client owns one keep-alive pool for the complete
        # multi-stage run. Test clients remain simple duck-typed replacements.
        if isinstance(self.client, QwenJsonClient):
            async with self.client:
                return await self._execute_with_client(skill, media, pet, run_step)
        return await self._execute_with_client(skill, media, pet, run_step)

    async def _execute_with_client(
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
        effective_fps = configured_video_fps(float(profile["video_fps"]))
        video_observation = await run_step(
            "home.step1.video_understanding",
            lambda: self._understand_video(
                skill,
                media,
                pet,
                prompt,
                effective_fps,
            ),
        )
        timestamps = self._select_timestamps(video_observation, media.duration or 0.0)
        frames = await run_step(
            "home.step2.evidence_frame_extraction",
            lambda: asyncio.to_thread(self.media_processor.extract_frames, media, timestamps, "evidence"),
        )
        media.keyframes = frames
        frame_observations = await run_step(
            "home.step2.keyframe_understanding",
            lambda: self._observe_images(
                skill,
                frames,
                prompt,
                pet,
                timestamps=timestamps,
                video_observation=video_observation,
            ),
        )
        selected_step1 = copy.deepcopy(video_observation)
        initial_step1 = selected_step1.pop("normality_review_initial", None)
        selected_step1.pop("normality_review_observation", None)
        step1_observations = (
            [initial_step1, selected_step1]
            if isinstance(initial_step1, dict)
            else [selected_step1]
        )
        evidence = {
            "video_observation": video_observation,
            "step1_observations": step1_observations,
            "keyframe_observations": frame_observations,
            "selected_timestamps": timestamps,
            "step2_evidence": {
                "keyframe_observations": frame_observations,
                "selected_timestamps": timestamps,
            },
            "consistency": {
                "status": video_observation.get("normality_consistency", "single_pass"),
                "initial_result": "normal"
                if isinstance(initial_step1, dict)
                and not self._abnormal_candidates(initial_step1)
                else "not_available",
                "review_result": "abnormal"
                if video_observation.get("normality_consistency") == "conflict"
                else "normal"
                if video_observation.get("normality_consistency") == "consistent_normal"
                else "incomplete"
                if video_observation.get("normality_consistency") == "review_incomplete"
                else "native_unavailable"
                if video_observation.get("normality_consistency") == "dense_normal_unconfirmed"
                else "not_run",
            },
        }
        result = await run_step(
            "home.step3.result_composition",
            lambda: self._compose(skill, media, pet, evidence),
        )
        if skill.name == "home-health-check-gait":
            result = self._apply_gait_uncertainty(result, evidence)
        runtime_provider = video_observation.get("runtime_provider", "qwen_native_video")
        native_video = runtime_provider == "qwen_native_video"
        dense_timeline = runtime_provider == "ffmpeg_dense_timeline"
        execution_strategy = video_observation.get("execution_strategy", "native_only")
        result.setdefault("report_meta", {})["analysis_runtime"] = {
            "video_provider": runtime_provider,
            "native_video": native_video,
            "analysis_quality": (
                "full_video"
                if native_video
                else "high_density_timeline"
                if dense_timeline
                else "degraded_dense_storyboard"
            ),
            "fps": effective_fps,
            "keyframe_timestamps": timestamps,
            "storyboard_frame_count": video_observation.get("storyboard_frame_count", 0),
            "timeline_fps": video_observation.get("timeline_fps"),
            "timeline_retry_triggered": video_observation.get("timeline_retry_triggered", False),
            "timeline_attempt_diagnostics": video_observation.get("timeline_attempt_diagnostics", []),
            "native_attempts": video_observation.get("native_attempts", 1),
            "native_attempt_diagnostics": video_observation.get("native_attempt_diagnostics", []),
            "native_max_pixels": video_observation.get("native_max_pixels"),
            "native_input_bytes": video_observation.get("native_input_bytes"),
            "native_expected_frames": video_observation.get("native_expected_frames"),
            "native_estimated_pixels": video_observation.get("native_estimated_pixels"),
            "native_input_mode": video_observation.get("native_input_mode"),
            "native_payload_recompressed": video_observation.get("native_payload_recompressed", False),
            "fallback_reason_code": video_observation.get("fallback_reason_code"),
            "execution_strategy": execution_strategy,
            "native_first_sse_observed": video_observation.get(
                "native_first_sse_observed",
                False,
            ),
            "native_total_timeout_seconds": float(
                self.config["plugins"]["video_understanding"].get(
                    "native_total_timeout_seconds",
                    300,
                )
            ),
            "normality_review_timeout_seconds": float(
                self.config["plugins"]["video_understanding"].get(
                    "normality_review_total_timeout_seconds",
                    90,
                )
            ),
            "normality_review_triggered": video_observation.get("normality_review_triggered", False),
            "normality_review_changed": video_observation.get("normality_review_changed", False),
            "normality_review_frame_count": video_observation.get("normality_review_frame_count", 0),
            "normality_review_channels": video_observation.get("normality_review_channels", []),
            "normality_review_elapsed_ms": video_observation.get("normality_review_elapsed_ms"),
            "normality_consistency": video_observation.get("normality_consistency", "single_pass"),
            "orientation_normalized": video_observation.get("orientation_normalized", False),
            "skill_prompt_source": "skill.visual_recognition_prompt",
            "skill_steps_completed": [1, 2, 3],
        }
        result["_workflow_evidence"] = evidence
        return result

    @staticmethod
    def _apply_gait_uncertainty(
        result: dict[str, Any],
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """Make a within-run Step 1 conflict visible without inventing certainty."""
        consistency = str((evidence.get("consistency") or {}).get("status") or "")
        if consistency not in {
            "conflict",
            "review_incomplete",
            "dense_normal_unconfirmed",
        }:
            return result

        summary = result.setdefault("ai_summary", {})
        existing_summary = re.sub(r"\s+", " ", str(summary.get("summary") or "")).strip()
        if consistency == "conflict":
            prefix = (
                "本次视频整体分析与独立复核结论不一致，可能受拍摄视角、动作覆盖或画面清晰度影响。"
                "管家已结合两次完整视频分析与关键帧证据，尽力为你给出以下建议"
            )
        elif consistency == "review_incomplete":
            prefix = (
                "本次视频的独立复核未在限定时间内完成，当前素材仍存在无法准确判断的内容。"
                "管家已结合已完成的视频与关键帧证据，尽力为你给出以下建议"
            )
        else:
            prefix = (
                "本次原生完整视频分析未能使用，当前正常倾向仅来自密集时间轴，无法准确确认整体步态正常。"
                "管家已结合现有时间轴与关键帧证据，尽力为你给出以下建议"
            )
        available = max(0, 150 - len(prefix) - 1)
        summary["assessment_status"] = "inconclusive"
        summary["severity"] = "中度"
        summary["severity_color"] = "orange"
        summary["summary"] = f"{prefix}：{existing_summary[:available]}"[:150]
        result.setdefault("report_meta", {})["analysis_status"] = "inconclusive"

        dimensions = result.get("dimensions")
        if isinstance(dimensions, list):
            orange_seen = False
            for index, dimension in enumerate(dimensions):
                if not isinstance(dimension, dict):
                    continue
                if dimension.get("ui_color") == "red":
                    dimension["ui_color"] = "orange"
                    dimension["status_label"] = (
                        "疑似异常，建议复测" if index == 2 else "需复核"
                    )
                    original_analysis = re.sub(
                        r"\s+", " ", str(dimension.get("ai_analysis") or "")
                    ).strip()
                    reason = (
                        "两次视频整体分析对该维度的判断不一致"
                        if consistency == "conflict"
                        else "独立完整视频复核未完成，该维度无法准确判断"
                        if consistency == "review_incomplete"
                        else "原生完整视频分析未完成，仅凭密集时间轴无法准确确认该维度"
                    )
                    dimension["ai_analysis"] = f"{reason}；{original_analysis}"[:150]
                    orange_seen = True
                elif dimension.get("ui_color") == "orange":
                    orange_seen = True
            if not orange_seen and len(dimensions) >= 3 and isinstance(dimensions[2], dict):
                uncertain = dimensions[2]
                uncertain["ui_color"] = "orange"
                uncertain["status_label"] = (
                    "疑似异常，建议复测"
                    if consistency == "conflict"
                    else "无法准确判断"
                )
                original_analysis = re.sub(
                    r"\s+", " ", str(uncertain.get("ai_analysis") or "")
                ).strip()
                reason = (
                    "两次视频整体分析对异常信号的判断不一致"
                    if consistency == "conflict"
                    else "独立完整视频复核未完成，异常信号无法准确判断"
                    if consistency == "review_incomplete"
                    else "原生完整视频分析未完成，密集时间轴的正常倾向无法排除遗漏"
                )
                uncertain["ai_analysis"] = f"{reason}；{original_analysis}"[:150]
                uncertain["suggestion"] = (
                    "建议按拍摄指南补充侧面低机位连续行走视频后复测；若日常已出现明显跛行、拖行或不承重，请及时咨询兽医。"
                )

        suggestions = result.get("health_suggestions")
        if isinstance(suggestions, list) and suggestions and isinstance(suggestions[0], dict):
            suggestions[0]["ui_label"] = "PRIORITY_高"
            suggestions[0]["ui_color"] = "blue"
            suggestions[0]["title"] = "按拍摄指南复测"
            suggestions[0]["content"] = (
                "请使用侧面低机位录制5–10秒连续行走视频，确保四肢完整入镜；如已出现明显疼痛或不承重，请优先咨询兽医。"
            )
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
            reasoning_budget=int(
                self.config["plugins"]["result_composer"].get("repair_thinking_budget", 4096)
            ),
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
        plugin_role = (
            "你是一位拥有20年临床经验的资深宠物骨科与运动健康专家。"
            if skill.name == "home-health-check-gait"
            else "你是一位资深宠物行为观察专家。"
        )
        native_timeout = float(settings.get("native_timeout_seconds", 150))
        native_connect_timeout = float(settings.get("native_connect_timeout_seconds", 20))
        native_write_timeout = float(settings.get("native_write_timeout_seconds", native_timeout))
        native_pool_timeout = float(settings.get("native_pool_timeout_seconds", 20))
        configured_total_timeout = float(settings.get("native_total_timeout_seconds", 0))
        native_total_timeout = configured_total_timeout if configured_total_timeout > 0 else None
        native_max_attempts = max(1, int(settings.get("native_max_attempts", 2)))
        native_retry_backoff = max(0.0, float(settings.get("native_retry_backoff_seconds", 1.0)))
        configured_max_pixels = int(settings.get("max_pixels", 655360))
        total_pixels = int(settings.get("total_pixels", 67108864))
        native_max_pixels = configured_max_pixels
        native_frame_count = max(1, int((media.duration or 0.0) * fps + 0.999))
        estimated_pixels = media.width * media.height * native_frame_count
        extreme_timeline_ratio = max(
            1.0,
            float(settings.get("extreme_timeline_total_pixels_ratio", 1.0)),
        )
        direct_dense_timeline = skill.name == "home-health-check-gait" and (
            media.width > 0
            and media.height > 0
            and estimated_pixels > int(total_pixels * extreme_timeline_ratio)
        )
        pet_context = json.dumps(pet.model_dump(exclude_none=True), ensure_ascii=False)
        species_instruction = (
            f"宠物物种已知为 {pet.species}。"
            if pet.species
            else "宠物档案未提供物种；必须先从视频画面识别猫/狗等物种，再按该物种的正常步态基线判断，不得因字段为空降低异常敏感度。"
        )
        gait_cycle_contract = (
            "\n步态专项时序要求：必须先完成 cycle_assessments，再生成 overall_description 和 abnormal_candidates。"
            "至少跟踪可见的连续 3 个步态周期；对左右后足分别检查向前摆至髋下、"
            "离地净空、掌垫承重和向后蹬地四个阶段，并同步检查骨盆高度/摇摆、后肢轨迹、左右步幅及前肢牵拉代偿。"
            "重点识别反复或持续的蹬地不足、足尖擦地/拖行、后躯低位、外展画圈、交叉、兔跳、步幅缩短、"
            "骨盆侧摆或前肢拉动身体；异常可以间歇出现，某一步外观正常不能抵消多个周期重复出现的异常。"
            "后方或俯视角度看不清足背时，必须依据后足轨迹、落点、骨盆和躯干的连续变化判断，不能因此默认正常。"
            "只有连续画面直接显示并在多个相邻动作或步态周期重复时才标 high；短暂可疑且受遮挡时标 medium/low，"
            "不得把正常摆动相的单帧姿态当作异常。"
            "特别防止双侧漏检：四肢依次落地或左右后肢相对对称，只能说明节律尚存，不能证明步态健康；"
            "若双侧后肢都呈步幅缩短、僵硬低抬、蹬地减弱、近同步前摆（兔跳样）、后躯低位或外展，仍属于异常。"
            "必须比较后肢相对前肢的推进幅度和骨盆移动，不能把肥胖、短腿、慢走或光滑地面自动解释为正常。"
            "只有明确观察到左右后足在至少3个周期均完成充分前摆、离地、承重和蹬地，且骨盆高度稳定、无前肢拉动代偿，"
            "才可下正常结论；看不清时应写 limitations，禁止用‘整体流畅/交替协调’替代逐项证据。"
            if skill.name == "home-health-check-gait"
            else ""
        )
        contract = (
            "\n\n这是 Skill Step 1。只返回 JSON："
            '{"overall_description":"","timeline":[{"start_seconds":0,"end_seconds":0,'
            '"signal":"","evidence":"","confidence":"low|medium|high"}],'
            '"cycle_assessments":[{"start_seconds":0,"end_seconds":0,"view":"side|rear|overhead|mixed",'
            '"left_hind":"前摆/离地/承重/蹬地的可见事实","right_hind":"前摆/离地/承重/蹬地的可见事实",'
            '"pelvis_and_trunk":"","fore_hind_relation":"","verdict":"normal|attention|abnormal","evidence":""}],'
            '"screening_flags":[{"code":"bilateral_hind_hypometria|reduced_hind_propulsion|low_toe_clearance|'
            'low_pelvis_or_sway|bunny_hop|forelimb_pull_compensation|unilateral_weight_avoidance|splay_cross_circumduction|'
            'knuckling_or_dorsal_contact|stiff_or_reduced_joint_excursion","status":"present|suspected|absent",'
            '"repeated_cycles":0,"start_seconds":0,"end_seconds":0,"timestamp":0,"evidence":""}],'
            '"abnormal_candidates":[{"start_seconds":0,"end_seconds":0,"timestamp":0,'
            '"signal":"","evidence":"","confidence":"low|medium|high"}],' 
            '"keyframe_timestamps":[0.0,0.0,0.0],"limitations":[]}。'
            "timeline 必须按时间顺序覆盖视频开头至结尾，正常与异常片段都要记录；"
            "cycle_assessments 至少3项（不足3个可见周期时逐项列出全部周期），每项必须写画面事实，禁止只写‘协调/正常’；"
            "screening_flags 必须逐项筛查列出的10类体征；视觉体征是否存在与病因是否确定是两回事。"
            "同一机械异常被连续画面直接观察到且跨至少2个周期重复时，status 必须为 present，并同步写入 abnormal_candidates；"
            "不得仅因左右对称、病因未知、宠物肥胖或地面光滑而把已重复观察到的体征降为 absent。"
            "只有可疑但没有跨周期重复的信号才标 suspected；确实未见才标 absent。"
            "overall_description 必须基于连续视频，不得只复述关键帧；"
            "abnormal_candidates 只记录画面直接支持的异常候选；没有异常时必须为空数组，不得为了完成任务制造异常；"
            "keyframe_timestamps 必须优先取 abnormal_candidates 中信号最明显的 2-3 个时间点，"
            "只有 abnormal_candidates 为空时才使用覆盖开头、中段、结尾的正常检查点。"
            + gait_cycle_contract
            + f"\n宠物上下文：{pet_context}。{species_instruction}"
            + f"\n视频时长：{media.duration:.2f} 秒；分辨率：{media.width}x{media.height}。"
        )
        step1_system = (
            "你是 Fura-AI宠物管家的视频理解插件。"
            + plugin_role
            + "你现在只执行 Skill Step 1，不生成最终报告。"
            + contract
        )

        if direct_dense_timeline:
            dense_result = await self._run_dense_timeline_once(
                media,
                prompt,
                plugin_role,
                contract,
                route_reason="native_hard_budget_exceeded",
                native_frame_count=native_frame_count,
                estimated_pixels=estimated_pixels,
                native_max_pixels=native_max_pixels,
                orientation_normalized=round(abs(media.rotation_degrees)) % 360 != 0,
                native_input_mode="dense_timeline",
                native_input_bytes=0,
                execution_strategy="dense_only_hard_limit",
            )
            dense_result = self._mark_dense_normal_unconfirmed(dense_result)
            dense_result["normality_review_triggered"] = False
            dense_result["normality_review_changed"] = False
            dense_result["normality_review_channels"] = []
            dense_result["native_first_sse_observed"] = False
            return dense_result

        analysis_video_path = await asyncio.to_thread(
            self.media_processor.normalize_video_orientation,
            media,
        )
        orientation_normalized = analysis_video_path.resolve() != media.path.resolve()
        native_input_bytes = analysis_video_path.stat().st_size
        native_video_url, native_input_mode = _video_source_url(
            analysis_video_path,
            int(settings["max_size_mb"]),
        )
        native_payload_recompressed = False
        native_content = [
            {
                "type": "video_url",
                "video_url": {
                    "url": native_video_url,
                    "fps": fps,
                },
                "min_pixels": int(settings.get("min_pixels", 65536)),
                "max_pixels": native_max_pixels,
                "total_pixels": total_pixels,
            },
            {"type": "text", "text": prompt},
        ]
        native_errors: list[Exception] = []
        attempts_started = 0
        native_attempt_diagnostics: list[dict[str, Any]] = []
        native_first_sse_event = asyncio.Event()

        attempt_limit = native_max_attempts
        for attempt in range(1, attempt_limit + 1):
            attempts_started += 1
            attempt_started = time.perf_counter()
            try:
                request_diagnostics = {
                    "input_mode": native_input_mode,
                    "requested_fps": fps,
                    "expected_frames": native_frame_count,
                    "input_bytes": native_input_bytes,
                    "estimated_base64_bytes": (
                        ((native_input_bytes + 2) // 3) * 4
                        if native_input_mode == "base64_data_url"
                        else 0
                    ),
                    "max_pixels": native_max_pixels,
                    "total_pixels": total_pixels,
                }

                async def complete_native() -> dict[str, Any]:
                    if self.native_video_semaphore is None:
                        request_diagnostics["queue_wait_ms"] = 0.0
                        return await self.client.complete(
                            native_content,
                            system=step1_system,
                            timeout_seconds=native_timeout,
                            max_tokens=3200,
                            reasoning_budget=int(settings.get("thinking_budget", 4096)),
                            connect_timeout_seconds=native_connect_timeout,
                            write_timeout_seconds=native_write_timeout,
                            pool_timeout_seconds=native_pool_timeout,
                            total_timeout_seconds=native_total_timeout,
                            request_diagnostics=request_diagnostics,
                            first_sse_event=native_first_sse_event,
                        )
                    queue_started = time.perf_counter()
                    async with self.native_video_semaphore:
                        request_diagnostics["queue_wait_ms"] = round(
                            (time.perf_counter() - queue_started) * 1000,
                            2,
                        )
                        return await self.client.complete(
                            native_content,
                            system=step1_system,
                            timeout_seconds=native_timeout,
                            max_tokens=3200,
                            reasoning_budget=int(settings.get("thinking_budget", 4096)),
                            connect_timeout_seconds=native_connect_timeout,
                            write_timeout_seconds=native_write_timeout,
                            pool_timeout_seconds=native_pool_timeout,
                            total_timeout_seconds=native_total_timeout,
                            request_diagnostics=request_diagnostics,
                            first_sse_event=native_first_sse_event,
                        )

                successful = await complete_native()
            except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as exc:
                elapsed_ms = round((time.perf_counter() - attempt_started) * 1000, 2)
                reason_code = self._native_failure_code(exc)
                native_errors.append(exc)
                attempt_diagnostic: dict[str, Any] = {
                        "attempt": attempt,
                        "outcome": "failed",
                        "reason_code": reason_code,
                        "elapsed_ms": elapsed_ms,
                    }
                if request_diagnostics:
                    attempt_diagnostic["transport"] = copy.deepcopy(request_diagnostics)
                native_attempt_diagnostics.append(attempt_diagnostic)
                if (
                    isinstance(exc, httpx.HTTPStatusError)
                    and exc.response.status_code == 413
                    and attempt < native_max_attempts
                ):
                    retry_target_bytes = int(float(settings.get("native_payload_retry_mb", 7.0)) * 1024 * 1024)
                    analysis_video_path = await asyncio.to_thread(
                        self.media_processor.normalize_video_orientation,
                        media,
                        retry_target_bytes,
                    )
                    native_input_bytes = analysis_video_path.stat().st_size
                    native_payload_recompressed = True
                    native_attempt_diagnostics[-1]["recovery"] = "payload_recompressed"
                    retry_video_url, native_input_mode = _video_source_url(
                        analysis_video_path,
                        int(settings["max_size_mb"]),
                    )
                    native_content[0]["video_url"]["url"] = retry_video_url
                    continue
                # A read/write timeout has already consumed the full request
                # window. Re-sending the same large video only adds load and
                # increases end-to-end latency. Fast transport/response faults
                # are still retried once because they have not paid that cost.
                if attempt >= native_max_attempts or not self._native_failure_is_fast_retryable(exc):
                    break
                if native_retry_backoff:
                    await asyncio.sleep(native_retry_backoff * attempt)
            else:
                attempt_diagnostic = {
                        "attempt": attempt,
                        "outcome": "completed",
                        "elapsed_ms": round((time.perf_counter() - attempt_started) * 1000, 2),
                    }
                if request_diagnostics:
                    attempt_diagnostic["transport"] = copy.deepcopy(request_diagnostics)
                native_attempt_diagnostics.append(attempt_diagnostic)
                successful["runtime_provider"] = "qwen_native_video"
                successful["native_attempts"] = attempts_started
                successful["native_attempt_diagnostics"] = native_attempt_diagnostics
                successful["native_max_pixels"] = native_max_pixels
                successful["native_input_bytes"] = native_input_bytes
                successful["native_expected_frames"] = native_frame_count
                successful["native_estimated_pixels"] = estimated_pixels
                successful["native_input_mode"] = native_input_mode
                successful["native_payload_recompressed"] = native_payload_recompressed
                successful["orientation_normalized"] = orientation_normalized
                successful["execution_strategy"] = "native_only"
                successful["native_first_sse_observed"] = native_first_sse_event.is_set()
                return await self._review_normal_video_if_needed(
                    successful,
                    native_content,
                    media,
                    skill.name,
                    plugin_role,
                    prompt,
                    contract,
                    native_timeout,
                )

        last_error = native_errors[-1] if native_errors else None
        native_reason = self._native_failure_code(last_error)
        native_elapsed_ms = round(
            sum(float(item.get("elapsed_ms") or 0) for item in native_attempt_diagnostics),
            2,
        )
        error = RuntimeError(
            f"NATIVE_VIDEO_UNAVAILABLE: reason={native_reason} "
            f"attempts={attempts_started} elapsed_ms={native_elapsed_ms}"
        )
        error.code = "NATIVE_VIDEO_UNAVAILABLE"  # type: ignore[attr-defined]
        raise error from last_error

    async def _run_dense_timeline_once(
        self,
        media: MediaArtifact,
        prompt: str,
        plugin_role: str,
        contract: str,
        *,
        route_reason: str,
        native_frame_count: int,
        estimated_pixels: int,
        native_max_pixels: int,
        orientation_normalized: bool,
        native_input_mode: str,
        native_input_bytes: int,
        execution_strategy: str,
    ) -> dict[str, Any]:
        """Run one full-range dense timeline for an input over the hard limit."""
        settings = self.config["plugins"]["video_understanding"]
        frame_count = min(
            max(24, int(settings.get("fallback_max_frames", 72))),
            max(
                24,
                int(settings.get("fallback_frame_count", 24)),
                int((media.duration or 0.0) * float(settings.get("fallback_fps", 6.0)) + 0.999),
            ),
        )
        timestamps = self.media_processor.uniform_timestamps(media.duration or 0.0, frame_count)
        extraction_name = "storyboard-hard-limit"
        frames = await asyncio.to_thread(
            self.media_processor.extract_frames,
            media,
            timestamps,
            extraction_name,
        )
        timeline_fps = self._sampled_frame_fps(timestamps)
        content: list[dict[str, Any]] = [
            {
                "type": "video",
                "video": [_image_data_url(frame) for frame in frames],
                "fps": timeline_fps,
                "min_pixels": int(settings.get("min_pixels", 65536)),
                "max_pixels": int(settings.get("max_pixels", 655360)),
                "total_pixels": int(settings.get("total_pixels", 67108864)),
            },
            {"type": "text", "text": prompt},
        ]
        system = (
            "你是 Fura-AI宠物管家的高负载视频密集时间轴理解插件。"
            + plugin_role
            + "你现在只执行 Skill Step 1。"
            + contract
            + f"\n以下 {len(frames)} 帧按时间顺序覆盖完整视频，必须逐周期、逐侧分析。"
            "必须结合相邻帧变化，不得把单帧姿态当成持续动态结论。"
        )
        request_diagnostics: dict[str, Any] = {
            "input_mode": "dense_frame_list",
            "frame_count": len(frames),
            "timeline_fps": timeline_fps,
            "estimated_pixels": estimated_pixels,
            "max_pixels": native_max_pixels,
        }

        async def complete_dense() -> dict[str, Any]:
            if self.dense_video_semaphore is None:
                request_diagnostics["queue_wait_ms"] = 0.0
                return await self.client.complete(
                    content,
                    system=system,
                    timeout_seconds=float(settings.get("dense_timeline_timeout_seconds", 240)),
                    max_tokens=2400,
                    reasoning_budget=int(settings.get("thinking_budget", 4096)),
                    total_timeout_seconds=float(
                        settings.get("dense_timeline_total_timeout_seconds", 320)
                    ),
                    request_diagnostics=request_diagnostics,
                )
            queue_started = time.perf_counter()
            async with self.dense_video_semaphore:
                request_diagnostics["queue_wait_ms"] = round(
                    (time.perf_counter() - queue_started) * 1000,
                    2,
                )
                return await self.client.complete(
                    content,
                    system=system,
                    timeout_seconds=float(settings.get("dense_timeline_timeout_seconds", 240)),
                    max_tokens=2400,
                    reasoning_budget=int(settings.get("thinking_budget", 4096)),
                    total_timeout_seconds=float(
                        settings.get("dense_timeline_total_timeout_seconds", 320)
                    ),
                    request_diagnostics=request_diagnostics,
                )

        started = time.perf_counter()
        try:
            result = await complete_dense()
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as exc:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            reason_code = self._native_failure_code(exc)
            error = RuntimeError(
                f"DENSE_TIMELINE_FAILED: frames={len(frames)} reason={reason_code} "
                f"elapsed_ms={elapsed_ms}"
            )
            error.code = "DENSE_TIMELINE_FAILED"  # type: ignore[attr-defined]
            error.reason_code = reason_code  # type: ignore[attr-defined]
            raise error from exc

        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        timeline_diagnostic: dict[str, Any] = {
            "attempt": 1,
            "frame_count": len(frames),
            "outcome": "completed",
            "elapsed_ms": elapsed_ms,
        }
        if request_diagnostics:
            timeline_diagnostic["transport"] = copy.deepcopy(request_diagnostics)
        result["runtime_provider"] = "ffmpeg_dense_timeline"
        result["native_attempts"] = 0
        result["native_attempt_diagnostics"] = []
        result["native_max_pixels"] = native_max_pixels
        result["native_input_bytes"] = native_input_bytes
        result["native_expected_frames"] = native_frame_count
        result["native_estimated_pixels"] = estimated_pixels
        result["native_input_mode"] = native_input_mode
        result["native_payload_recompressed"] = False
        result["fallback_reason_code"] = route_reason
        result["storyboard_frame_count"] = len(frames)
        result["storyboard_timestamps"] = timestamps
        result["timeline_fps"] = timeline_fps
        result["timeline_retry_triggered"] = False
        result["timeline_attempt_diagnostics"] = [timeline_diagnostic]
        result["orientation_normalized"] = orientation_normalized
        result["execution_strategy"] = execution_strategy
        result.setdefault("limitations", []).append(
            (
                f"原生10 FPS预计像素量超过硬预算，已使用覆盖全时段的 {len(frames)} 帧密集时间轴。"
                if execution_strategy == "dense_only_hard_limit"
                else f"该结果来自覆盖全时段的 {len(frames)} 帧密集时间轴。"
            )
        )
        return result

    async def _review_normal_video_if_needed(
        self,
        initial: dict[str, Any],
        original_content: list[dict[str, Any]],
        media: MediaArtifact,
        skill_name: str,
        plugin_role: str,
        prompt: str,
        contract: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        """Run one blind Step 1 repeat before accepting a normal video result.

        The repeat deliberately receives neither the first result nor an extra
        FFmpeg frame-list representation.  This keeps it independent from the
        first conclusion and removes the previous two-channel review, which
        added a third Step 1 call while still anchoring both reviews on the
        initial normal JSON.
        """
        if self._abnormal_candidates(initial):
            initial["normality_review_triggered"] = False
            initial["normality_review_changed"] = False
            initial["normality_review_channels"] = []
            return initial

        review_focus = (
            "逐侧跟踪至少3个完整步态周期，重点寻找蹬地不足、足尖擦地/拖行、后躯低位、外展/交叉、"
            "步幅不对称、骨盆/躯干摇摆和前肢拉动代偿。某一步外观正常不能抵消多个周期重复异常；"
            "后方或俯视看不清足背时，应结合后足轨迹、落点与骨盆连续运动，不得默认正常。"
            if skill_name == "home-health-check-gait"
            else "重点寻找持续僵硬、躲避、过度舔舐、异常呼吸、重复刻板动作和明显压力信号。"
        )
        review_instruction = (
            contract
            + "\n这是一次独立、盲态的 Step 1 复核。你不知道也不得推测此前分析结论。"
            "请从零开始逐秒复核完整时序，必须重做 cycle_assessments，"
            "并检查开头、中段、最后四分之一及相邻步态周期。"
            + review_focus
            + "只有相邻帧或连续动作直接支持时才写入 abnormal_candidates，并给出最清楚的 timestamp；"
            "静态姿态、光滑地面或拍摄角度不足以证明异常时应降低置信度。若仍无直接证据，保持 abnormal_candidates 为空。"
        )
        review_content = [dict(item) for item in original_content]
        review_frame_count = 0
        review_timeout = max(
            1.0,
            min(
                timeout_seconds,
                float(
                    self.config["plugins"]["video_understanding"].get(
                        "normality_review_total_timeout_seconds",
                        90,
                    )
                ),
            ),
        )
        runtime_keys = (
            "runtime_provider",
            "native_attempts",
            "native_attempt_diagnostics",
            "native_max_pixels",
            "native_input_bytes",
            "native_expected_frames",
            "native_estimated_pixels",
            "native_input_mode",
            "native_payload_recompressed",
            "fallback_reason_code",
            "storyboard_frame_count",
            "storyboard_timestamps",
            "timeline_fps",
            "timeline_retry_triggered",
            "timeline_attempt_diagnostics",
            "orientation_normalized",
            "execution_strategy",
            "native_first_sse_observed",
        )
        review_started = time.perf_counter()
        try:
            reviewed = await self.client.complete(
                review_content,
                system=(
                    "你是 Fura-AI宠物管家视频理解插件的独立盲检复核器。"
                    + plugin_role
                    + "你仍只执行 Skill Step 1。"
                    + review_instruction
                ),
                timeout_seconds=review_timeout,
                total_timeout_seconds=review_timeout,
                max_tokens=2400,
                reasoning_budget=int(
                    self.config["plugins"]["video_understanding"].get(
                        "normality_review_thinking_budget", 4096
                    )
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reviewed = copy.deepcopy(initial)
            reviewed.setdefault("limitations", []).append(
                "独立完整视频复核未在限定时间内完成，当前正常结论仍需结合复测确认。"
            )
            reviewed["normality_consistency"] = "review_incomplete"
            reviewed["normality_review_error"] = self._native_failure_code(exc)
        else:
            reviewed["normality_consistency"] = (
                "conflict"
                if self._abnormal_candidates(reviewed)
                else "consistent_normal"
            )

        for key in runtime_keys:
            if key in initial:
                reviewed[key] = initial[key]
        reviewed["normality_review_initial"] = copy.deepcopy(initial)
        reviewed["normality_review_triggered"] = True
        reviewed["normality_review_changed"] = reviewed["normality_consistency"] == "conflict"
        reviewed["normality_review_frame_count"] = review_frame_count
        reviewed["normality_review_channels"] = ["native_blind_repeat"]
        reviewed["normality_review_elapsed_ms"] = round(
            (time.perf_counter() - review_started) * 1000,
            2,
        )
        return reviewed

    @classmethod
    def _mark_dense_normal_unconfirmed(
        cls,
        observation: dict[str, Any],
    ) -> dict[str, Any]:
        """Never present a frame-only normal tendency as confirmed normal."""
        if cls._abnormal_candidates(observation):
            return observation
        observation.setdefault("limitations", []).append(
            "本次未使用原生完整视频分析；密集时间轴未见明确异常，但无法据此准确确认整体步态正常。"
        )
        observation["normality_consistency"] = "dense_normal_unconfirmed"
        observation["normality_review_triggered"] = False
        observation["normality_review_changed"] = False
        observation["normality_review_channels"] = []
        return observation

    @staticmethod
    def _abnormal_candidates(observation: dict[str, Any]) -> list[dict[str, Any]]:
        values = observation.get("abnormal_candidates")
        candidates = (
            [value for value in values if isinstance(value, dict) and str(value.get("signal") or "").strip()]
            if isinstance(values, list)
            else []
        )
        flags = observation.get("screening_flags")
        if not isinstance(flags, list):
            return candidates
        for flag in flags:
            if not isinstance(flag, dict) or str(flag.get("status") or "").lower() != "present":
                continue
            try:
                repeated_cycles = int(flag.get("repeated_cycles") or 0)
            except (TypeError, ValueError):
                continue
            evidence = str(flag.get("evidence") or "").strip()
            code = str(flag.get("code") or "").strip()
            if repeated_cycles < 2 or not code or not evidence:
                continue
            derived = {
                "start_seconds": flag.get("start_seconds", 0),
                "end_seconds": flag.get("end_seconds", flag.get("start_seconds", 0)),
                "timestamp": flag.get("timestamp", flag.get("start_seconds", 0)),
                "signal": code,
                "evidence": evidence,
                "confidence": "medium",
                "source": "structured_screening_flag",
            }
            if not any(
                str(item.get("signal") or "") == code and item.get("timestamp") == derived["timestamp"]
                for item in candidates
            ):
                candidates.append(derived)
        return candidates

    @staticmethod
    def _sampled_frame_fps(timestamps: list[float]) -> float:
        if len(timestamps) < 2 or timestamps[-1] <= timestamps[0]:
            return 1.0
        return round((len(timestamps) - 1) / (timestamps[-1] - timestamps[0]), 3)

    @staticmethod
    def _native_failure_code(error: Exception) -> str:
        if getattr(error, "timeout_phase", None) == "total":
            return "native_total_timeout"
        if isinstance(error, httpx.ConnectTimeout):
            return "native_connect_timeout"
        if isinstance(error, httpx.WriteTimeout):
            return "native_write_timeout"
        if isinstance(error, httpx.ReadTimeout):
            return "native_read_timeout"
        if isinstance(error, httpx.PoolTimeout):
            return "native_pool_timeout"
        if isinstance(error, httpx.TimeoutException):
            return "native_timeout"
        if isinstance(error, httpx.HTTPStatusError):
            return f"native_http_{error.response.status_code}"
        if isinstance(error, httpx.HTTPError):
            return "native_transport_error"
        return "native_response_error"

    @staticmethod
    def _native_failure_is_fast_retryable(error: Exception) -> bool:
        if isinstance(error, (httpx.ReadTimeout, httpx.WriteTimeout)):
            return False
        if isinstance(error, httpx.HTTPStatusError):
            status = error.response.status_code
            return status in {408, 409, 425, 429} or status >= 500
        return isinstance(
            error,
            (httpx.ConnectTimeout, httpx.PoolTimeout, httpx.TransportError, ValueError, KeyError, json.JSONDecodeError),
        )

    async def _observe_images(
        self,
        skill: SkillDefinition,
        paths: list[Path],
        prompt: str,
        pet: PetContext,
        *,
        timestamps: list[float] | None = None,
        page_mode: bool = False,
        video_observation: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if not paths:
            raise ValueError("关键帧/分页图片为空，不能跳过图像复核")
        retries = int(self.config["plugins"]["image_understanding"]["retries"])
        configured_concurrency = max(
            1,
            int(self.config["plugins"]["image_understanding"].get("max_concurrency", 1)),
        )
        semaphore = asyncio.Semaphore(min(configured_concurrency, len(paths)))

        async def observe_one(index: int, path: Path) -> dict[str, Any]:
            if page_mode:
                label = f"PDF 第 {index + 1} 页"
            elif timestamps is not None:
                label = f"原视频 {timestamps[index]:.3f} 秒关键帧"
            else:
                label = f"原始图片 {index + 1}"
            last_error: Exception | None = None
            for attempt in range(retries + 1):
                try:
                    async with semaphore:
                        return await self._observe_image(
                            path,
                            prompt,
                            pet,
                            label,
                            skill,
                            video_observation=video_observation,
                        )
                except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as exc:
                    last_error = exc
                    if attempt >= retries:
                        break
                    # Stagger retries to avoid a parallel rate-limit retry storm.
                    await asyncio.sleep(min(1.5, 0.2 * (2**attempt) + 0.05 * index))
            assert last_error is not None
            raise last_error

        gathered = await asyncio.gather(
            *(observe_one(index, path) for index, path in enumerate(paths)),
            return_exceptions=True,
        )
        results: list[dict[str, Any]] = []
        for index, value in enumerate(gathered):
            if isinstance(value, dict):
                results.append(value)
                continue
            if not isinstance(value, (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError)):
                raise value
            # Quality-first fallback: a frame that exhausted its concurrent
            # retries gets one isolated final attempt instead of being dropped.
            if page_mode:
                label = f"PDF 第 {index + 1} 页"
            elif timestamps is not None:
                label = f"原视频 {timestamps[index]:.3f} 秒关键帧"
            else:
                label = f"原始图片 {index + 1}"
            results.append(
                await self._observe_image(
                    paths[index],
                    prompt,
                    pet,
                    label,
                    skill,
                    video_observation=video_observation,
                )
            )
        return results

    async def _observe_image(
        self,
        path: Path,
        prompt: str,
        pet: PetContext,
        label: str,
        skill: SkillDefinition,
        *,
        video_observation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        candidate_context = ""
        observation_contract = (
            '{"source":"","assessment":"normal|attention|abnormal",'
            '"observations":[{"finding":"","visual_evidence":"",'
            '"confidence":"low|medium|high"}],"limitations":[]}'
        )
        if skill.name == "home-health-check-gait":
            candidates = (
                self._abnormal_candidates(video_observation)
                if isinstance(video_observation, dict)
                else []
            )
            candidate_context = (
                "这是独立的步态漏检复核。必须放大检查后足是掌垫着地还是足背着地/翻爪，并检查跗关节高度、"
                "后肢外展/交叉、左右承重、骨盆/躯干代偿和前肢代偿。assessment 必须根据当前帧直接证据填写："
                "明确异常为 abnormal，仅有可疑信号为 attention，无异常为 normal；不得因为 Step 1 未定位异常就默认正常。"
                "同时必须区分正常步态相位：孤立单帧的后肢屈曲、抬腿、足部尚未完全伸展或躯干瞬时倾斜，"
                "不能独立证明动态异常。若当前帧清楚显示异常落点、后躯低位、明显外展/交叉、承重塌陷、足背接触，"
                "或其姿态与 Step 1 多周期连续异常相互吻合，可标 abnormal；运动模糊或无法排除正常相位时标 attention。"
                "关键帧用于定位和细节复核，不能因单帧未捕捉到短暂动态而否定连续视频中重复出现的异常。"
            )
            if candidates:
                candidate_context += (
                    "Step 1 连续视频已把当前帧附近列为异常复核区，候选如下："
                    + json.dumps(candidates, ensure_ascii=False)
                    + "。候选只用于定位，不是必须接受的答案；若单帧直接支持或反驳候选，必须明确写出。"
                )
        suffix = (
            f"\n\n当前素材：{label}。宠物上下文：{pet.model_dump_json()}。"
            "这是证据复核步骤，只描述画面可见事实；动态指标若无法由单帧确认，必须写入 limitations。"
            + candidate_context
            + "只返回 JSON："
            + observation_contract
            + "。"
        )
        content = [
            {"type": "image_url", "image_url": {"url": _image_data_url(path)}},
            {"type": "text", "text": prompt},
        ]
        image_settings = self.config["plugins"]["image_understanding"]
        reasoning_budget = int(image_settings.get("thinking_budget", 4096))
        request_kwargs = {
            "system": (
                f"你是 Fura-AI宠物管家的 {skill.name} 图像理解插件。"
                + (
                    "你是一位拥有20年临床经验的资深宠物骨科与运动健康专家。"
                    if skill.name == "home-health-check-gait"
                    else "请严格依据画面可见事实分析。"
                )
                + suffix
            ),
            "timeout_seconds": float(image_settings["timeout_seconds"]),
            "max_tokens": 3500,
        }
        result = await self.client.complete(
            content,
            **request_kwargs,
            reasoning_budget=reasoning_budget,
        )
        # A smaller reasoning cap is only a fast path. Missing assessment or
        # concrete visual evidence is retried once at the established full
        # budget before the outer transport retry policy is involved.
        if (
            isinstance(self.client, QwenJsonClient)
            and reasoning_budget < 4096
            and not self._complete_frame_observation(result)
        ):
            result = await self.client.complete(
                content,
                **request_kwargs,
                reasoning_budget=4096,
            )
        result.setdefault("source", label)
        return result

    @staticmethod
    def _complete_frame_observation(result: dict[str, Any]) -> bool:
        if str(result.get("assessment") or "").lower() not in {"normal", "attention", "abnormal"}:
            return False
        observations = result.get("observations")
        if not isinstance(observations, list) or not observations:
            return False
        for observation in observations:
            if not isinstance(observation, dict):
                return False
            if not str(observation.get("finding") or "").strip():
                return False
            if not str(observation.get("visual_evidence") or "").strip():
                return False
            if str(observation.get("confidence") or "").lower() not in {"low", "medium", "high"}:
                return False
        return True

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
            reasoning_budget=int(
                self.config["plugins"]["image_understanding"].get("direct_thinking_budget", 4096)
            ),
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
            + "\n证据使用规则：video_observation 负责连续时间线、持续时间与频次，关键帧负责复核动作类型、"
            + "肢体位置、着地姿态和画面细节；两者不是主从覆盖关系。若文字时间线与关键帧在动作类型或姿态上冲突，"
            + "必须指出冲突、降低确定性，并以可直接核验的证据修正不受支持的描述，禁止照抄上游结论。"
            + "步态属于时序任务：Step 1 在多个周期直接观察到的 high-confidence 连续异常，本身可以支持异常信号，"
            + "或 cycle_assessments 中至少两个带直接 evidence 的周期均为 abnormal，也属于重复动态异常证据；"
            + "screening_flags 中 status=present、repeated_cycles>=2 且有直接 evidence 的体征同样是重复动态证据，"
            + "其视觉存在性不得因病因尚不确定、双侧对称或关键帧模糊而被删除；"
            + "关键帧模糊、遮挡或未捕捉到瞬时动态时不得反向否决；关键帧若清晰矛盾，应明确冲突并降低具体动作描述的确定性。"
            + "若完整时间线及正常复核均未发现异常，单张关键帧中的屈曲、抬腿、着地面积小或瞬时倾斜不得独立生成 red/严重；"
            + "仅有疑似/可能/可疑信号且没有重复动态或独立帧支持时应保留不确定性，不能升级为确证异常。"
            + "不得因只有2-3张复核帧而声称未分析完整视频。降级时必须依据密集顺序帧的完整时间轴并明确局限。"
            + "若 consistency.status=conflict，表示同一次检测的两份独立 Step 1 JSON 分别为正常和异常。"
            + "此时必须综合 step1_observations 与 step2_evidence，以温暖、尽力而为的管家语言输出；"
            + "ai_summary 必须为中度/orange、assessment_status=inconclusive，开头说明视频整体分析在独立复核后结论不一致，"
            + "可能受拍摄视角、动作覆盖或清晰度影响，并说明管家已结合两次视频分析与关键帧尽力给出建议。"
            + "冲突中的疑似异常不得写成确诊，相关维度使用 orange 和‘需复核/疑似异常，建议复测’。"
            + "若 consistency.status=review_incomplete，必须正常输出结果，说明独立复核未完成及当前无法准确判断的内容，"
            + "不得因证据不足省略字段或假装已经确认正常。"
            + "若 consistency.status=dense_normal_unconfirmed，表示原生完整视频不可用且密集时间轴仅得到正常倾向；"
            + "最终必须输出中度/orange、assessment_status=inconclusive，说明仅凭密集时间轴无法准确确认整体正常，"
            + "不得输出green或把关键帧未见异常写成完整视频已经正常。"
        )
        # Follow the Skill exactly: Step 4 consumes the Step 1 description and
        # Step 3 frame descriptions, not a second independent media-analysis path.
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        if media.type != "video":
            paths = media.keyframes if media.type == "pdf" else [media.path]
            label = "PDF 页面" if media.type == "pdf" else "原始图片"
            for index, path in enumerate(paths, start=1):
                content.append({"type": "text", "text": f"最终汇总可复核的{label} {index}："})
                content.append({"type": "image_url", "image_url": {"url": _image_data_url(path)}})
        return await self.client.complete(
            content,
            system="你是 Fura-AI宠物管家的证据汇总与结构化结果插件。",
            timeout_seconds=float(self.config["plugins"]["result_composer"]["timeout_seconds"]),
            max_tokens=6000,
            reasoning_budget=int(
                self.config["plugins"]["result_composer"].get("thinking_budget", 4096)
            ),
        )

    @staticmethod
    def _select_timestamps(observation: dict[str, Any], duration: float) -> list[float]:
        values: list[Any] = []
        for candidate in HomeCheckWorkflow._abnormal_candidates(observation):
            timestamp = candidate.get("timestamp")
            if timestamp is None:
                try:
                    timestamp = (float(candidate.get("start_seconds")) + float(candidate.get("end_seconds"))) / 2
                except (TypeError, ValueError):
                    continue
            values.append(timestamp)
        values.extend(observation.get("keyframe_timestamps") or [])
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
