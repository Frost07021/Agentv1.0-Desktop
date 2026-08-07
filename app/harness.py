from __future__ import annotations

import json
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
from .validators import OutputValidationError, stabilize_repaired_result, validate_result


class AnalysisExecutionError(RuntimeError):
    def __init__(self, error: Exception, traces: list[StepTrace], failure_file: Path):
        self.error = error
        self.traces = traces
        self.failure_file = failure_file
        stage = traces[-1].step_id if traces else "initialization"
        super().__init__(f"分析在 {stage} 阶段失败：{type(error).__name__}: {error}")


class Harness:
    def __init__(self, workspace_root: Path, output_dir: Path | None = None):
        self.workspace_root = workspace_root.resolve()
        self.registry = SkillRegistry(self.workspace_root / "skill-definitions")
        self.output_root = (output_dir or self.workspace_root / "output").resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        runtime_dir = self.workspace_root / "runtime"
        self.media_processor = MediaProcessor(runtime_dir)

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
        if request.mode == "fake":
            adapter = FakeVisionAdapter()
        else:
            settings = model_settings_from_environment(self.workspace_root)
            adapter = OpenAICompatibleVisionAdapter(settings)
        if request.mode == "real" and is_home_check:
            workflow = HomeCheckWorkflow(self.workspace_root, settings, self.media_processor)

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
        try:
            await self._step(
                traces,
                "validate_output",
                lambda: self._async_value(validate_result(request.skill_name, result)),
            )
        except OutputValidationError as exc:
            if request.mode != "real" or not isinstance(adapter, OpenAICompatibleVisionAdapter):
                raise
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
                lambda: self._async_value(validate_result(request.skill_name, result)),
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
                "native_attempts",
                "fallback_reason_code",
                "skill_prompt_source",
                "skill_steps_completed",
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
