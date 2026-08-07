import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from PIL import Image

from app.config import ModelSettings
from app.home_check_workflow import HomeCheckWorkflow, extract_visual_prompt
from app.media import MediaArtifact, MediaProcessor
from app.schemas import PetContext
from app.skill_loader import SkillRegistry


def _registry() -> SkillRegistry:
    root = Path(__file__).resolve().parents[1]
    return SkillRegistry(root / "skill-definitions")


def test_each_home_skill_exposes_its_exact_plugin_prompt() -> None:
    registry = _registry()
    expectations = {
        "home-health-check-dental": "请仔细观察这张宠物口腔/牙齿照片",
        "home-health-check-stool": "请仔细观察这张宠物粪便照片",
        "home-health-check-gait": "请仔细观察这段宠物行走视频",
        "home-health-check-behavior": "请仔细观察这段宠物日常行为视频",
        "home-health-check-xray": "请仔细观察这张宠物X光片影像",
    }
    for skill_name, expected in expectations.items():
        prompt = extract_visual_prompt(registry.get(skill_name))
        assert prompt.startswith(expected)
        assert "```" not in prompt


def test_video_workflow_executes_all_skill_steps_and_reuses_skill_prompt(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    processor = MediaProcessor(tmp_path / "runtime")
    workflow = HomeCheckWorkflow(
        workspace,
        ModelSettings("qwen3.7-plus", "https://model.example/v1", "secret"),
        processor,
    )
    video_path = tmp_path / "walk.mp4"
    video_path.write_bytes(b"video")
    frame = tmp_path / "frame.jpg"
    Image.new("RGB", (32, 32), "white").save(frame)
    media = MediaArtifact(
        "video", video_path, "video/mp4", 5, 320, 240, duration=10.0, fps=25.0, keyframes=[frame]
    )
    processor.extract_frames = lambda *_args, **_kwargs: [frame, frame, frame]  # type: ignore[method-assign]
    calls: list[Any] = []

    class FakeClient:
        async def complete(self, content: Any, **_kwargs: Any) -> dict[str, Any]:
            calls.append(content)
            if isinstance(content, list) and content and content[0].get("type") == "video_url":
                return {"overall_description": "步态观察", "timeline": [], "keyframe_timestamps": [1, 5, 8]}
            if isinstance(content, list):
                return {"observations": [], "limitations": []}
            return {"report_meta": {}, "dimensions": [], "ai_summary": {}, "health_suggestions": [], "disclaimer": ""}

    workflow.client = FakeClient()  # type: ignore[assignment]
    traces: list[str] = []

    async def run_step(step_id: str, operation: Callable[[], Awaitable[Any]]) -> Any:
        traces.append(step_id)
        return await operation()

    result = asyncio.run(
        workflow.execute(
            _registry().get("home-health-check-gait"), media, PetContext(pet_name="警长"), run_step
        )
    )
    assert traces == [
        "home.step1.video_understanding",
        "home.step2.evidence_frame_extraction",
        "home.step2.keyframe_understanding",
        "home.step3.result_composition",
    ]
    assert len(calls) == 5
    exact_prompt = extract_visual_prompt(_registry().get("home-health-check-gait"))
    assert exact_prompt in calls[0][1]["text"]
    assert all(exact_prompt in call[1]["text"] for call in calls[1:4])
    assert result["report_meta"]["analysis_runtime"]["skill_steps_completed"] == [1, 2, 3]
    assert result["report_meta"]["analysis_runtime"]["keyframe_timestamps"] == [1.0, 5.0, 8.0]


def test_repair_keeps_candidate_workflow_evidence_and_original_image(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    workflow = HomeCheckWorkflow(
        workspace,
        ModelSettings("qwen3.7-plus", "https://model.example/v1", "secret"),
        MediaProcessor(tmp_path / "runtime"),
    )
    image_path = tmp_path / "dental.jpg"
    Image.new("RGB", (64, 64), "white").save(image_path)
    media = MediaArtifact("image", image_path, "image/jpeg", image_path.stat().st_size, 64, 64)
    calls: list[Any] = []

    class FakeClient:
        async def complete(self, content: Any, **_kwargs: Any) -> dict[str, Any]:
            calls.append(content)
            return {"report_meta": {}}

    workflow.client = FakeClient()  # type: ignore[assignment]
    asyncio.run(
        workflow.repair_result(
            _registry().get("home-health-check-dental"),
            media,
            PetContext(pet_name="警长"),
            {"ai_summary": {"summary": "候选结果"}},
            "ai_summary.summary 内容过短",
            {"direct_image_analysis": True},
        )
    )
    assert len(calls) == 1
    assert calls[0][0]["type"] == "text"
    assert "候选结果" in calls[0][0]["text"]
    assert "direct_image_analysis" in calls[0][0]["text"]
    assert any(item.get("type") == "image_url" for item in calls[0])


def test_xray_image_executes_visual_observation_then_result_composer(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    workflow = HomeCheckWorkflow(
        workspace,
        ModelSettings("qwen3.7-plus", "https://model.example/v1", "secret"),
        MediaProcessor(tmp_path / "runtime"),
    )
    image_path = tmp_path / "xray.jpg"
    Image.new("RGB", (64, 64), "white").save(image_path)
    media = MediaArtifact("image", image_path, "image/jpeg", image_path.stat().st_size, 64, 64)
    calls: list[Any] = []

    class FakeClient:
        async def complete(self, content: Any, **_kwargs: Any) -> dict[str, Any]:
            calls.append(content)
            if isinstance(content, list):
                return {"observations": [{"finding": "影像可见", "visual_evidence": "骨骼轮廓", "confidence": "medium"}], "limitations": []}
            return {"report_meta": {}, "dimensions": [], "ai_summary": {}, "health_suggestions": [], "disclaimer": ""}

    workflow.client = FakeClient()  # type: ignore[assignment]
    traces: list[str] = []

    async def run_step(step_id: str, operation: Callable[[], Awaitable[Any]]) -> Any:
        traces.append(step_id)
        return await operation()

    result = asyncio.run(
        workflow.execute(
            _registry().get("home-health-check-xray"), media, PetContext(pet_name="警长"), run_step
        )
    )

    assert traces == ["home.step1.image_understanding", "home.step2.result_composition"]
    assert "请仔细观察这张宠物X光片影像" in calls[0][1]["text"]
    assert "影像可见" in calls[1]
    assert "https://cdn.fura.example" not in calls[1]
    assert result["_workflow_evidence"]["image_observations"]


def test_video_native_timeout_uses_storyboard_fallback(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    workflow = HomeCheckWorkflow(
        workspace,
        ModelSettings("qwen3.7-plus", "https://model.example/v1", "secret"),
        MediaProcessor(tmp_path / "runtime"),
    )
    video_path = tmp_path / "walk.mp4"
    video_path.write_bytes(b"video")
    frame = tmp_path / "frame.jpg"
    Image.new("RGB", (32, 32), "white").save(frame)
    media = MediaArtifact("video", video_path, "video/mp4", 5, 320, 240, duration=8.0, fps=25.0, keyframes=[frame])
    extracted_timestamps: list[float] = []

    def extract_dense_frames(_media, timestamps, purpose="evidence"):
        assert purpose == "storyboard"
        extracted_timestamps.extend(timestamps)
        return [frame] * len(timestamps)

    workflow.media_processor.extract_frames = extract_dense_frames  # type: ignore[method-assign]
    calls: list[Any] = []

    class TimeoutThenStoryboardClient:
        async def complete(self, content: Any, **_kwargs: Any) -> dict[str, Any]:
            calls.append(content)
            if content[0].get("type") == "video_url":
                raise httpx.ReadTimeout("native video timeout")
            return {"overall_description": "顺序帧步态观察", "timeline": [], "keyframe_timestamps": [1, 4, 6]}

    workflow.client = TimeoutThenStoryboardClient()  # type: ignore[assignment]
    result = asyncio.run(
        workflow._understand_video(
            _registry().get("home-health-check-gait"), media, PetContext(pet_name="警长"), "测试 Prompt", 4.0
        )
    )

    assert len(calls) == 3
    assert result["runtime_provider"] == "ffmpeg_dense_storyboard"
    assert result["fallback_reason_code"] == "native_timeout"
    assert result["storyboard_frame_count"] == 12
    assert extracted_timestamps[0] == 0.0
    assert extracted_timestamps[-1] == 7.95
    assert "12 帧顺序分析" in result["limitations"][-1]


def test_native_read_timeout_is_retried_once_before_fallback(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    workflow = HomeCheckWorkflow(
        workspace,
        ModelSettings("qwen3.7-plus", "https://model.example/v1", "secret"),
        MediaProcessor(tmp_path / "runtime"),
    )
    video_path = tmp_path / "walk.mp4"
    video_path.write_bytes(b"video")
    media = MediaArtifact("video", video_path, "video/mp4", 5, 320, 240, duration=8.0, fps=25.0)
    calls = 0

    class TimeoutThenNativeClient:
        async def complete(self, content: Any, **_kwargs: Any) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.ReadTimeout("native video timeout")
            return {"overall_description": "完整视频观察", "timeline": [], "keyframe_timestamps": [1, 4, 6]}

    workflow.client = TimeoutThenNativeClient()  # type: ignore[assignment]
    result = asyncio.run(
        workflow._understand_video(
            _registry().get("home-health-check-gait"), media, PetContext(pet_name="警长"), "测试 Prompt", 4.0
        )
    )

    assert calls == 2
    assert result["runtime_provider"] == "qwen_native_video"
    assert result["native_attempts"] == 2


def test_fast_native_response_failure_gets_one_retry(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    workflow = HomeCheckWorkflow(
        workspace,
        ModelSettings("qwen3.7-plus", "https://model.example/v1", "secret"),
        MediaProcessor(tmp_path / "runtime"),
    )
    video_path = tmp_path / "walk.mp4"
    video_path.write_bytes(b"video")
    media = MediaArtifact("video", video_path, "video/mp4", 5, 320, 240, duration=8.0, fps=25.0)
    calls = 0

    class InvalidThenValidClient:
        async def complete(self, content: Any, **_kwargs: Any) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ValueError("invalid response")
            return {"overall_description": "完整视频观察", "timeline": [], "keyframe_timestamps": [1, 4, 6]}

    workflow.client = InvalidThenValidClient()  # type: ignore[assignment]
    result = asyncio.run(
        workflow._understand_video(
            _registry().get("home-health-check-gait"), media, PetContext(pet_name="警长"), "测试 Prompt", 4.0
        )
    )

    assert calls == 2
    assert result["runtime_provider"] == "qwen_native_video"
    assert result["native_attempts"] == 2
