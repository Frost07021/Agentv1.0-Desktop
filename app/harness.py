from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import model_settings_from_environment
from .identity import sanitize_model_payload
from .home_check_workflow import HomeCheckWorkflow
from .media import MediaArtifact, MediaProcessor
from .model_adapter import (
    FakeVisionAdapter,
    OpenAICompatibleVisionAdapter,
)
from .schemas import StepTrace, TaskRequest, TaskResponse
from .skill_loader import SkillRegistry
from .validators import (
    OutputValidationError,
    make_gait_inconclusive_result,
    stabilize_repaired_result,
    validate_result,
    validate_result_against_evidence,
)


class AnalysisExecutionError(RuntimeError):
    def __init__(self, error: Exception, traces: list[StepTrace], failure_file: Path):
        self.error = error
        self.traces = traces
        self.failure_file = failure_file
        stage = traces[-1].step_id if traces else "initialization"
        super().__init__(f"分析在 {stage} 阶段失败：{type(error).__name__}: {error}")


class Harness:
    ANALYSIS_CACHE_VERSION = "validated-home-check-v3-native-step1"

    def __init__(self, workspace_root: Path, output_dir: Path | None = None):
        self.workspace_root = workspace_root.resolve()
        self.registry = SkillRegistry(self.workspace_root / "skill-definitions")
        self.output_root = (output_dir or self.workspace_root / "output").resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        runtime_dir = self.workspace_root / "runtime"
        self.media_processor = MediaProcessor(runtime_dir)
        raw_native_concurrency = os.getenv("AGENT_VIDEO_MAX_CONCURRENCY", "1")
        try:
            native_concurrency = int(raw_native_concurrency)
        except ValueError as exc:
            raise ValueError("AGENT_VIDEO_MAX_CONCURRENCY 必须是正整数") from exc
        if native_concurrency <= 0:
            raise ValueError("AGENT_VIDEO_MAX_CONCURRENCY 必须是正整数")
        self.native_video_semaphore = asyncio.Semaphore(native_concurrency)
        self.dense_video_semaphore = asyncio.Semaphore(1)

    async def execute(self, request: TaskRequest) -> TaskResponse:
        task_id = uuid.uuid4().hex
        traces: list[StepTrace] = []
        try:
            return await self._execute(request, task_id, traces)
        except Exception as exc:
            failure_file = self._save_failure(request, task_id, traces, exc)
            raise AnalysisExecutionError(exc, traces, failure_file) from exc

    async def _execute(self, request: TaskRequest, task_id: str, traces: list[StepTrace]) -> TaskResponse:
        skill = await self._step(traces, "load_skill", lambda: self._async_value(self.registry.get(request.skill_name)))
        media_path = self._resolve_media_path(request.media_path)
        media = await self._step(traces, "prepare_media", lambda: self._async_value(self.media_processor.prepare(media_path)))
        self._assert_media_matches_skill(request.skill_name, media)

        is_home_check = request.skill_name.startswith("home-health-check-")
        workflow: HomeCheckWorkflow | None = None
        home_evidence: dict[str, Any] | None = None
        analysis_cache_key: str | None = None
        analysis_cache_hit = False
        if request.mode == "fake":
            adapter = FakeVisionAdapter()
        else:
            settings = model_settings_from_environment(self.workspace_root)
            adapter = OpenAICompatibleVisionAdapter(settings)
        if request.mode == "real" and is_home_check:
            analysis_cache_key = self._analysis_cache_key(request, media, skill.content, settings.model)
            cached = await self._step(
                traces,
                "home.cache.lookup",
                lambda: self._async_value(self._load_analysis_cache(analysis_cache_key)),
            )
            if cached is not None:
                result, home_evidence = cached
                analysis_cache_hit = True
            else:
                workflow = HomeCheckWorkflow(
                    self.workspace_root,
                    settings,
                    self.media_processor,
                    native_video_semaphore=self.native_video_semaphore,
                    dense_video_semaphore=self.dense_video_semaphore,
                )

                async def run_home_step(step_id: str, operation: Callable[[], Awaitable[Any]]) -> Any:
                    return await self._step(traces, step_id, operation)

                result = await workflow.execute(skill, media, request.pet, run_home_step)
                home_evidence = result.pop("_workflow_evidence", None)
        else:
            result = await self._step(traces, "model_analysis", lambda: adapter.analyze(skill, media, request.pet))
        result = await self._step(
            traces,
            "normalize_result",
            lambda: self._async_value(self._normalize_result(request, media, result)),
        )
        if request.mode == "real" and is_home_check:
            # Apply deterministic UI/schema reconciliation before validation.
            # This changes no visual finding, but prevents a second model call
            # when (for example) a red gait dimension is paired with a stale
            # summary color or an otherwise valid sentence is slightly short.
            result = await self._step(
                traces,
                "stabilize_output",
                lambda: self._async_value(stabilize_repaired_result(result)),
            )
            if analysis_cache_hit:
                result.setdefault("report_meta", {}).setdefault("analysis_runtime", {})[
                    "result_cache_hit"
                ] = True
        try:
            await self._step(
                traces,
                "validate_output",
                lambda: self._async_value(
                    self._validate_output(request.skill_name, result, home_evidence)
                ),
            )
        except OutputValidationError as exc:
            if request.mode != "real" or not isinstance(adapter, OpenAICompatibleVisionAdapter):
                raise
            original_result = result
            inconclusive = make_gait_inconclusive_result(result, str(exc))
            locally_recovered = False
            if inconclusive is not None:
                try:
                    result = await self._step(
                        traces,
                        "stabilize_inconclusive_output",
                        lambda: self._async_value(stabilize_repaired_result(inconclusive)),
                    )
                    await self._step(
                        traces,
                        "validate_inconclusive_output",
                        lambda: self._async_value(
                            self._validate_output(request.skill_name, result, home_evidence)
                        ),
                    )
                except OutputValidationError:
                    result = original_result
                else:
                    locally_recovered = True
            if not locally_recovered:
                result = original_result
            if not locally_recovered:
                runtime_meta = (result.get("report_meta") or {}).get("analysis_runtime")
                if workflow is not None and home_evidence is not None:
                    result = await self._step(
                        traces,
                        "repair_output_with_evidence",
                        lambda: workflow.repair_result(
                            skill, media, request.pet, result, str(exc), home_evidence
                        ),
                    )
                else:
                    result = await self._step(
                        traces,
                        "repair_output_with_media",
                        lambda: adapter.repair_result(skill, result, str(exc), request.pet, media),
                    )
                if runtime_meta:
                    result.setdefault("report_meta", {}).setdefault("analysis_runtime", runtime_meta)
                result = await self._step(
                    traces,
                    "normalize_repaired_result",
                    lambda: self._async_value(self._normalize_result(request, media, result)),
                )
                result = await self._step(
                    traces,
                    "stabilize_repaired_output",
                    lambda: self._async_value(stabilize_repaired_result(result)),
                )
                await self._step(
                    traces,
                    "validate_repaired_output",
                    lambda: self._async_value(
                        self._validate_output(request.skill_name, result, home_evidence)
                    ),
                )
        if (
            analysis_cache_key is not None
            and not analysis_cache_hit
            and self._cacheable_analysis_result(media, result)
        ):
            await self._step(
                traces,
                "home.cache.store",
                lambda: self._async_value(
                    self._save_analysis_cache(analysis_cache_key, result, home_evidence)
                ),
            )
        output_file = await self._step(
            traces,
            "save_result",
            lambda: self._async_value(self._save_result(request, result, task_id)),
        )
        return TaskResponse(
            task_id=task_id,
            status="completed",
            skill_name=request.skill_name,
            mode=request.mode,
            output_file=str(output_file),
            result=result,
            traces=traces,
        )

    def _analysis_cache_key(
        self,
        request: TaskRequest,
        media: MediaArtifact,
        skill_content: str,
        model_name: str,
    ) -> str:
        """Fingerprint every input that can change a validated analysis."""
        digest = hashlib.sha256()
        digest.update(self.ANALYSIS_CACHE_VERSION.encode("utf-8"))
        digest.update(request.skill_name.encode("utf-8"))
        digest.update(model_name.encode("utf-8"))
        clinical_pet_context = request.pet.model_dump(
            exclude={"pet_id", "pet_name", "avatar"},
            exclude_none=True,
        )
        digest.update(
            json.dumps(clinical_pet_context, ensure_ascii=False, sort_keys=True).encode("utf-8")
        )
        digest.update(skill_content.encode("utf-8"))
        config_path = self.workspace_root / "config" / "home-check-plugins.yaml"
        digest.update(config_path.read_bytes())
        for relative in (
            "app/home_check_workflow.py",
            "app/model_http.py",
            "app/validators.py",
            "app/skill_prompt.py",
        ):
            digest.update((self.workspace_root / relative).read_bytes())
        for name in (
            "AGENT_MODEL_THINKING",
            "AGENT_MODEL_THINKING_BUDGET",
            "AGENT_VIDEO_PROVIDER",
            "AGENT_VIDEO_FPS",
            "AGENT_VIDEO_PUBLIC_ROOT",
            "AGENT_VIDEO_PUBLIC_URL_PREFIX",
        ):
            digest.update(f"{name}={os.getenv(name, '')}".encode("utf-8"))
        with media.path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _load_analysis_cache(
        self,
        cache_key: str,
    ) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
        path = self.workspace_root / "runtime" / "analysis-cache" / f"{cache_key}.json"
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if payload.get("version") != self.ANALYSIS_CACHE_VERSION:
            return None
        result = payload.get("result")
        evidence = payload.get("evidence")
        if not isinstance(result, dict) or (evidence is not None and not isinstance(evidence, dict)):
            return None
        return result, evidence

    def _save_analysis_cache(
        self,
        cache_key: str,
        result: dict[str, Any],
        evidence: dict[str, Any] | None,
    ) -> Path:
        cache_dir = self.workspace_root / "runtime" / "analysis-cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        output = cache_dir / f"{cache_key}.json"
        temporary = cache_dir / f"{cache_key}.{uuid.uuid4().hex}.tmp"
        payload = {
            "version": self.ANALYSIS_CACHE_VERSION,
            "result": result,
            "evidence": evidence,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(output)
        return output

    @staticmethod
    def _cacheable_analysis_result(media: MediaArtifact, result: dict[str, Any]) -> bool:
        if media.type != "video":
            return True
        meta = result.get("report_meta") or {}
        runtime = meta.get("analysis_runtime") or {}
        summary = result.get("ai_summary") or {}
        # Never freeze a degraded or normal video conclusion. Validated red
        # results from native video or the planned high-density timeline path
        # are safe to reuse for byte-identical reuploads.
        return runtime.get("analysis_quality") in {"full_video", "high_density_timeline"} and (
            summary.get("severity_color") == "red"
        )

    def _save_failure(
        self,
        request: TaskRequest,
        task_id: str,
        traces: list[StepTrace],
        error: Exception,
    ) -> Path:
        failure_dir = self.workspace_root / "runtime" / "failed-runs"
        failure_dir.mkdir(parents=True, exist_ok=True)
        output = failure_dir / f"{task_id[:8]}.json"
        payload = {
            "task_id": task_id,
            "skill_name": request.skill_name,
            "mode": request.mode,
            "media_path": request.media_path,
            "error_type": type(error).__name__,
            "error_code": getattr(error, "code", None),
            "error": str(error),
            "traces": [trace.model_dump(mode="json") for trace in traces],
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return output

    def _resolve_media_path(self, value: str) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = self.workspace_root / path
        return path

    @staticmethod
    def _assert_media_matches_skill(skill_name: str, media: MediaArtifact) -> None:
        if skill_name in {"pet-report-analysis", "home-health-check-xray"}:
            if media.type not in {"image", "pdf"}:
                raise ValueError(f"{skill_name} 需要 image/pdf，当前为 {media.type}")
            return
        image_skills = {
            "home-health-check-dental",
            "home-health-check-stool",
        }
        expected = "image" if skill_name in image_skills else "video"
        if media.type != expected:
            raise ValueError(f"{skill_name} 需要 {expected}，当前为 {media.type}")
        if expected == "video" and not media.keyframes:
            raise ValueError("视频分析必须完成关键帧提取")

    def _save_result(self, request: TaskRequest, result: dict[str, Any], task_id: str) -> Path:
        pet_name = request.pet.pet_name or "unknown"
        safe_name = "".join(char for char in pet_name if char not in '<>:"/\\|?*') or "unknown"
        date = datetime.now().strftime("%Y-%m-%d")
        suffixes = {
            "pet-report-analysis": "report",
            "home-health-check-dental": "dental",
            "home-health-check-stool": "stool",
            "home-health-check-gait": "gait",
            "home-health-check-behavior": "behavior",
            "home-health-check-xray": "xray",
        }
        suffix = suffixes[request.skill_name]
        output_dir = self.output_root / request.mode
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / f"{safe_name}_{date}_{suffix}_{task_id[:8]}.json"
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return output

    @staticmethod
    def _normalize_result(request: TaskRequest, media: MediaArtifact, result: dict[str, Any]) -> dict[str, Any]:
        """覆盖模型不应决定的系统字段，避免 Skill 示例数据泄漏到结果。"""
        runtime_meta = (result.get("report_meta") or {}).get("analysis_runtime")
        result = sanitize_model_payload(result)
        meta = result.setdefault("report_meta", {})
        if isinstance(runtime_meta, dict):
            # Runtime data is generated by the workflow, not by the model. Keep only
            # product-safe quality fields and omit provider/model identity details.
            allowed_runtime_fields = {
                "native_video",
                "analysis_quality",
                "fps",
                "keyframe_timestamps",
                "storyboard_frame_count",
                "timeline_fps",
                "timeline_retry_triggered",
                "timeline_attempt_diagnostics",
                "native_attempts",
                "native_attempt_diagnostics",
                "native_max_pixels",
                "native_input_bytes",
                "native_expected_frames",
                "native_estimated_pixels",
                "native_input_mode",
                "native_payload_recompressed",
                "fallback_reason_code",
                "execution_strategy",
                "native_first_sse_observed",
                "native_total_timeout_seconds",
                "normality_review_timeout_seconds",
                "normality_review_triggered",
                "normality_review_changed",
                "normality_review_frame_count",
                "normality_review_channels",
                "normality_review_elapsed_ms",
                "normality_consistency",
                "orientation_normalized",
                "skill_prompt_source",
                "skill_steps_completed",
                "result_cache_hit",
            }
            meta["analysis_runtime"] = {
                key: value for key, value in runtime_meta.items() if key in allowed_runtime_fields
            }
        meta["pet"] = request.pet.model_dump()
        if request.skill_name == "pet-report-analysis":
            meta["raw_images"] = [str(path) for path in media.keyframes] if media.type == "pdf" else [str(media.path)]
        else:
            categories = {
                "home-health-check-dental": ("dental", "牙科评估"),
                "home-health-check-stool": ("stool", "便便分析"),
                "home-health-check-gait": ("gait", "步态分析"),
                "home-health-check-behavior": ("behavior", "行为评估"),
                "home-health-check-xray": ("xray", "X光片解读"),
            }
            category, category_name = categories[request.skill_name]
            meta["category"] = category
            meta["category_name"] = category_name
            meta["test_date"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            meta["media"] = {
                "type": media.type,
                "url": str(media.path),
                "thumbnail_url": str(media.keyframes[0] if media.keyframes else media.path),
                "duration": round(media.duration, 2) if media.duration is not None else None,
            }
        return result

    @staticmethod
    def _validate_output(
        skill_name: str,
        result: dict[str, Any],
        evidence: dict[str, Any] | None,
    ) -> None:
        validate_result(skill_name, result)
        validate_result_against_evidence(skill_name, result, evidence)

    @staticmethod
    async def _async_value(value: Any) -> Any:
        return value

    @staticmethod
    async def _step(
        traces: list[StepTrace],
        step_id: str,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        started = time.perf_counter()
        try:
            result = await operation()
            traces.append(StepTrace(step_id=step_id, status="completed", elapsed_ms=round((time.perf_counter() - started) * 1000, 2)))
            return result
        except Exception as exc:
            error_code = getattr(exc, "code", None)
            detail = f"{type(exc).__name__}{f'[{error_code}]' if error_code else ''}: {exc}"
            traces.append(
                StepTrace(
                    step_id=step_id,
                    status="failed",
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
                    detail=detail,
                )
            )
            raise
