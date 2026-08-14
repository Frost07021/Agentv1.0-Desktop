import asyncio
import base64
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
import pytest
from PIL import Image

from app.config import ModelSettings
from app.home_check_workflow import HomeCheckWorkflow, _video_source_url, extract_visual_prompt
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


def test_frame_fast_path_requires_concrete_visual_evidence() -> None:
    assert HomeCheckWorkflow._complete_frame_observation(
        {
            "assessment": "abnormal",
            "observations": [
                {
                    "finding": "后足低抬",
                    "visual_evidence": "跗关节接近地面且足尖净空较低",
                    "confidence": "high",
                }
            ],
        }
    )
    assert not HomeCheckWorkflow._complete_frame_observation(
        {"assessment": "normal", "observations": []}
    )


def test_video_workflow_executes_all_skill_steps_and_reuses_skill_prompt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AGENT_VIDEO_FPS", "8")
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
    systems: list[str] = []
    reasoning_budgets: list[int] = []

    class FakeClient:
        async def complete(self, content: Any, **_kwargs: Any) -> dict[str, Any]:
            calls.append(content)
            systems.append(str(_kwargs.get("system") or ""))
            reasoning_budgets.append(int(_kwargs.get("reasoning_budget") or 0))
            if isinstance(content, list) and content and content[0].get("type") == "video_url":
                return {
                    "overall_description": "步态观察",
                    "timeline": [],
                    "abnormal_candidates": [
                        {"start_seconds": 7, "end_seconds": 9, "timestamp": 8, "signal": "步幅异常", "evidence": "连续动作", "confidence": "medium"}
                    ],
                    "keyframe_timestamps": [1, 5, 8],
                }
            if isinstance(content, list) and content and "最终综合步骤" in content[0].get("text", ""):
                return {"report_meta": {}, "dimensions": [], "ai_summary": {}, "health_suggestions": [], "disclaimer": ""}
            if isinstance(content, list):
                return {"observations": [], "limitations": []}
            raise AssertionError("unexpected non-multimodal composition request")

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
    assert reasoning_budgets == [4096, 3072, 3072, 3072, 2048]
    exact_prompt = extract_visual_prompt(_registry().get("home-health-check-gait"))
    assert calls[0][1]["text"] == exact_prompt
    assert calls[0][0]["video_url"]["fps"] == 8.0
    assert "连续 3 个步态周期" in systems[0]
    assert all(call[1]["text"] == exact_prompt for call in calls[1:4])
    assert all("Step 1 连续视频已把当前帧附近列为异常复核区" in system for system in systems[1:4])
    assert all("足背着地/翻爪" in system for system in systems[1:4])
    assert all('"assessment":"normal|attention|abnormal"' in system for system in systems[1:4])
    assert calls[4][0]["type"] == "text"
    assert sum(item.get("type") == "image_url" for item in calls[4]) == 0
    final_prompt = calls[4][0]["text"]
    assert "禁止照抄上游结论" in final_prompt
    assert result["report_meta"]["analysis_runtime"]["skill_steps_completed"] == [1, 2, 3]
    assert result["report_meta"]["analysis_runtime"]["keyframe_timestamps"] == [8.0, 1.0, 5.0]
    assert result["report_meta"]["analysis_runtime"]["normality_review_triggered"] is False
    assert result["report_meta"]["analysis_runtime"]["fps"] == 8.0


def test_video_source_can_use_an_explicit_public_mapping(tmp_path: Path, monkeypatch) -> None:
    public_root = tmp_path / "served"
    video = public_root / "pet clips" / "步态.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    monkeypatch.setenv("AGENT_VIDEO_PUBLIC_ROOT", str(public_root))
    monkeypatch.setenv("AGENT_VIDEO_PUBLIC_URL_PREFIX", "https://media.example/videos")

    url, mode = _video_source_url(video, 50)

    assert mode == "public_url"
    assert url == "https://media.example/videos/pet%20clips/%E6%AD%A5%E6%80%81.mp4"


def test_legacy_step1_deadline_does_not_cancel_independent_normal_review(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    workflow = HomeCheckWorkflow(
        workspace,
        ModelSettings("qwen3.7-plus", "https://model.example/v1", "secret"),
        MediaProcessor(tmp_path / "runtime"),
    )
    # Guard against restoring the former outer timeout that included both the
    # primary native request and its independent blind review.
    workflow.config["plugins"]["video_understanding"]["step1_total_timeout_seconds"] = 0.01
    video = tmp_path / "walk.mp4"
    video.write_bytes(b"video")
    media = MediaArtifact("video", video, "video/mp4", 5, 320, 240, duration=2.0, fps=25.0)

    calls = 0

    class SlowPrimaryThenFailedReview:
        async def complete(self, _content: Any, **_kwargs: Any) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            if calls == 1:
                await asyncio.sleep(0.02)
                return {
                    "overall_description": "未见异常",
                    "timeline": [],
                    "abnormal_candidates": [],
                    "keyframe_timestamps": [0.5, 1.0, 1.5],
                    "limitations": [],
                }
            raise httpx.ReadTimeout("blind review stalled")

    workflow.client = SlowPrimaryThenFailedReview()  # type: ignore[assignment]
    result = asyncio.run(
        workflow._understand_video(
            _registry().get("home-health-check-gait"),
            media,
            PetContext(pet_name="警长"),
            "测试 Prompt",
            10.0,
        )
    )

    assert calls == 2
    assert result["normality_consistency"] == "review_incomplete"
    assert result["normality_review_error"] == "native_read_timeout"


def test_native_video_requests_share_the_configured_concurrency_gate(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    processor = MediaProcessor(tmp_path / "runtime")
    gate = asyncio.Semaphore(1)
    workflow = HomeCheckWorkflow(
        workspace,
        ModelSettings("qwen3.7-plus", "https://model.example/v1", "secret"),
        processor,
        native_video_semaphore=gate,
    )
    video = tmp_path / "walk.mp4"
    video.write_bytes(b"video")
    media = MediaArtifact(
        "video", video, "video/mp4", video.stat().st_size, 320, 240, duration=2.0, fps=25.0
    )
    active = 0
    maximum_active = 0
    calls = 0

    class FakeClient:
        async def complete(self, _content: Any, **_kwargs: Any) -> dict[str, Any]:
            nonlocal active, maximum_active, calls
            calls += 1
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return {
                "overall_description": "连续步态异常",
                "timeline": [],
                "abnormal_candidates": [
                    {
                        "start_seconds": 0,
                        "end_seconds": 2,
                        "timestamp": 1,
                        "signal": "后肢推进不足",
                        "evidence": "连续周期重复",
                        "confidence": "high",
                    }
                ],
                "keyframe_timestamps": [0.5, 1.0, 1.5],
            }

    workflow.client = FakeClient()  # type: ignore[assignment]

    async def run() -> None:
        skill = _registry().get("home-health-check-gait")
        pet = PetContext(pet_name="警长", species="cat")
        await asyncio.gather(
            workflow._understand_video(skill, media, pet, "测试 Prompt", 10.0),
            workflow._understand_video(skill, media, pet, "测试 Prompt", 10.0),
        )

    asyncio.run(run())
    assert calls == 2
    assert maximum_active == 1


def test_native_video_uses_orientation_normalized_proxy(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    processor = MediaProcessor(tmp_path / "runtime")
    workflow = HomeCheckWorkflow(
        workspace,
        ModelSettings("qwen3.7-plus", "https://model.example/v1", "secret"),
        processor,
    )
    original = tmp_path / "rotated.mp4"
    original.write_bytes(b"original-video")
    proxy = tmp_path / "upright.mp4"
    proxy.write_bytes(b"upright-video")
    media = MediaArtifact(
        "video",
        original,
        "video/mp4",
        original.stat().st_size,
        720,
        1280,
        duration=10.67,
        fps=30.0,
        rotation_degrees=-90.0,
    )
    processor.normalize_video_orientation = lambda _media: proxy  # type: ignore[method-assign]
    calls: list[Any] = []

    class FakeClient:
        async def complete(self, content: Any, **_kwargs: Any) -> dict[str, Any]:
            calls.append(content)
            return {
                "overall_description": "连续步态异常",
                "timeline": [],
                "abnormal_candidates": [
                    {
                        "start_seconds": 1,
                        "end_seconds": 7,
                        "timestamp": 4,
                        "signal": "后肢拖行",
                        "evidence": "多个周期重复",
                        "confidence": "high",
                    }
                ],
                "keyframe_timestamps": [2, 4, 6],
            }

    workflow.client = FakeClient()  # type: ignore[assignment]
    result = asyncio.run(
        workflow._understand_video(
            _registry().get("home-health-check-gait"),
            media,
            PetContext(pet_name="警长", species="cat"),
            "测试 Prompt",
            10.0,
        )
    )

    encoded = calls[0][0]["video_url"]["url"].split(",", 1)[1]
    assert base64.b64decode(encoded) == b"upright-video"
    assert calls[0][0]["max_pixels"] == 921600
    assert calls[0][0]["total_pixels"] == 100663296
    assert result["native_max_pixels"] == 921600
    assert result["orientation_normalized"] is True


def test_native_sse_is_telemetry_and_never_starts_dense_frames(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    processor = MediaProcessor(tmp_path / "runtime")
    workflow = HomeCheckWorkflow(
        workspace,
        ModelSettings("qwen3.7-plus", "https://model.example/v1", "secret"),
        processor,
    )
    video_path = tmp_path / "walk.mp4"
    video_path.write_bytes(b"video")
    media = MediaArtifact(
        "video",
        video_path,
        "video/mp4",
        video_path.stat().st_size,
        720,
        1280,
        duration=10.67,
        fps=30.0,
        rotation_degrees=-90.0,
    )
    processor.extract_frames = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("SSE timing must never start a dense-frame Step 1")
    )
    processor.normalize_video_orientation = lambda *_args: video_path  # type: ignore[method-assign]
    calls: list[str] = []

    class NativeClient:
        async def complete(self, content: Any, **kwargs: Any) -> dict[str, Any]:
            content_type = content[0]["type"]
            calls.append(content_type)
            assert content_type == "video_url"
            assert kwargs["first_sse_event"] is not None
            await asyncio.sleep(0.02)
            return {
                "overall_description": "原生视频发现重复后肢异常",
                "timeline": [],
                "abnormal_candidates": [{"timestamp": 6, "signal": "后肢推进不足"}],
                "keyframe_timestamps": [5, 6, 7],
            }

    workflow.client = NativeClient()  # type: ignore[assignment]
    result = asyncio.run(
        workflow._understand_video(
            _registry().get("home-health-check-gait"),
            media,
            PetContext(pet_name="警长"),
            "测试 Prompt",
            10.0,
        )
    )

    assert calls == ["video_url"]
    assert result["runtime_provider"] == "qwen_native_video"
    assert result["native_attempts"] == 1
    assert result["execution_strategy"] == "native_only"


def test_extreme_workload_dense_timeline_fails_after_one_model_attempt(tmp_path: Path) -> None:
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
        "video",
        video_path,
        "video/mp4",
        video_path.stat().st_size,
        720,
        1280,
        duration=13.5,
        fps=30.0,
        rotation_degrees=-90.0,
    )
    extracted: list[tuple[str, int]] = []

    def extract_dense(_media, timestamps, purpose="evidence"):
        extracted.append((purpose, len(timestamps)))
        return [frame] * len(timestamps)

    processor.extract_frames = extract_dense  # type: ignore[method-assign]
    processor.normalize_video_orientation = lambda *_args: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("hard-limit timeline must skip native proxy preparation")
    )
    calls: list[tuple[int, float]] = []

    class DenseClient:
        async def complete(self, content: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append((len(content[0]["video"]), kwargs["timeout_seconds"]))
            raise httpx.ReadTimeout("dense timeline stalled")

    workflow.client = DenseClient()  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="DENSE_TIMELINE_FAILED") as captured:
        asyncio.run(
            workflow._understand_video(
                _registry().get("home-health-check-gait"),
                media,
                PetContext(pet_name="警长"),
                "测试 Prompt",
                10.0,
            )
        )

    assert getattr(captured.value, "code") == "DENSE_TIMELINE_FAILED"
    assert calls == [(72, 240.0)]
    assert extracted == [("storyboard-hard-limit", 72)]


def test_native_timeout_does_not_restart_step1_with_dense_frames(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    processor = MediaProcessor(tmp_path / "runtime")
    workflow = HomeCheckWorkflow(
        workspace,
        ModelSettings("qwen3.7-plus", "https://model.example/v1", "secret"),
        processor,
    )
    video_path = tmp_path / "walk.mp4"
    video_path.write_bytes(b"video")
    media = MediaArtifact(
        "video",
        video_path,
        "video/mp4",
        video_path.stat().st_size,
        540,
        960,
        duration=13.44,
        fps=30.0,
    )
    processor.extract_frames = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("native failure must not restart Step 1 with dense frames")
    )
    processor.normalize_video_orientation = lambda *_args: video_path  # type: ignore[method-assign]
    calls: list[str] = []

    class NativeTimeoutClient:
        async def complete(self, content: Any, **_kwargs: Any) -> dict[str, Any]:
            content_type = content[0]["type"]
            calls.append(content_type)
            assert content_type == "video_url"
            raise httpx.ReadTimeout("native video timeout")

    workflow.client = NativeTimeoutClient()  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="NATIVE_VIDEO_UNAVAILABLE") as captured:
        asyncio.run(
            workflow._understand_video(
                _registry().get("home-health-check-gait"),
                media,
                PetContext(pet_name="呼呼"),
                "测试 Prompt",
                10.0,
            )
        )

    assert getattr(captured.value, "code") == "NATIVE_VIDEO_UNAVAILABLE"
    assert calls == ["video_url"]


def test_repeated_present_screening_flag_routes_to_abnormal_keyframes() -> None:
    observation = {
        "abnormal_candidates": [],
        "screening_flags": [
            {
                "code": "bilateral_hind_hypometria",
                "status": "present",
                "repeated_cycles": 3,
                "start_seconds": 0.5,
                "end_seconds": 3.5,
                "timestamp": 2.0,
                "evidence": "双侧后肢连续三个周期抬腿低且步幅短",
            }
        ],
        "keyframe_timestamps": [2.0, 1.0, 3.0],
    }

    candidates = HomeCheckWorkflow._abnormal_candidates(observation)
    assert candidates[0]["signal"] == "bilateral_hind_hypometria"
    assert candidates[0]["confidence"] == "medium"
    assert HomeCheckWorkflow._select_timestamps(observation, 5.0) == [2.0, 1.0, 3.0]


def test_keyframe_observations_run_concurrently_and_keep_input_order(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    workflow = HomeCheckWorkflow(
        workspace,
        ModelSettings("qwen3.7-plus", "https://model.example/v1", "secret"),
        MediaProcessor(tmp_path / "runtime"),
    )
    paths: list[Path] = []
    for index in range(3):
        path = tmp_path / f"frame-{index}.jpg"
        Image.new("RGB", (32, 32), (index * 30, 0, 0)).save(path)
        paths.append(path)
    active = 0
    peak_active = 0

    class ConcurrentClient:
        async def complete(self, _content: Any, **_kwargs: Any) -> dict[str, Any]:
            nonlocal active, peak_active
            active += 1
            peak_active = max(peak_active, active)
            try:
                await asyncio.sleep(0.02)
                return {"assessment": "normal", "observations": [], "limitations": []}
            finally:
                active -= 1

    workflow.client = ConcurrentClient()  # type: ignore[assignment]
    results = asyncio.run(
        workflow._observe_images(
            _registry().get("home-health-check-gait"),
            paths,
            "测试 Prompt",
            PetContext(pet_name="警长", species="cat"),
            timestamps=[1.0, 2.0, 3.0],
            video_observation={"abnormal_candidates": []},
        )
    )

    assert peak_active == 3
    assert [item["source"] for item in results] == [
        "原视频 1.000 秒关键帧",
        "原视频 2.000 秒关键帧",
        "原视频 3.000 秒关键帧",
    ]


def test_failed_parallel_keyframe_gets_isolated_quality_fallback(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    workflow = HomeCheckWorkflow(
        workspace,
        ModelSettings("qwen3.7-plus", "https://model.example/v1", "secret"),
        MediaProcessor(tmp_path / "runtime"),
    )
    workflow.config["plugins"]["image_understanding"]["retries"] = 0
    paths: list[Path] = []
    for index in range(3):
        path = tmp_path / f"frame-{index}.jpg"
        Image.new("RGB", (32, 32), (index * 30, 0, 0)).save(path)
        paths.append(path)
    second_frame_calls = 0

    class FailOnceClient:
        async def complete(self, _content: Any, **kwargs: Any) -> dict[str, Any]:
            nonlocal second_frame_calls
            system = str(kwargs.get("system") or "")
            if "2.000 秒关键帧" in system:
                second_frame_calls += 1
                if second_frame_calls == 1:
                    raise ValueError("transient malformed response")
            return {"assessment": "normal", "observations": [], "limitations": []}

    workflow.client = FailOnceClient()  # type: ignore[assignment]
    results = asyncio.run(
        workflow._observe_images(
            _registry().get("home-health-check-gait"),
            paths,
            "测试 Prompt",
            PetContext(pet_name="警长", species="cat"),
            timestamps=[1.0, 2.0, 3.0],
            video_observation={"abnormal_candidates": []},
        )
    )

    assert len(results) == 3
    assert second_frame_calls == 2


def test_normality_review_runs_one_blind_native_repeat(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    workflow = HomeCheckWorkflow(
        workspace,
        ModelSettings("qwen3.7-plus", "https://model.example/v1", "secret"),
        MediaProcessor(tmp_path / "runtime"),
    )
    video_path = tmp_path / "walk.mp4"
    video_path.write_bytes(b"video")
    frame = tmp_path / "review.jpg"
    Image.new("RGB", (32, 32), "white").save(frame)
    media = MediaArtifact("video", video_path, "video/mp4", 5, 320, 240, duration=10.0, fps=25.0)
    workflow.media_processor.extract_frames = lambda _media, timestamps, _purpose: [frame] * len(timestamps)  # type: ignore[method-assign]
    active_reviews = 0
    peak_reviews = 0
    review_systems: list[str] = []

    class ReviewClient:
        async def complete(self, _content: Any, **kwargs: Any) -> dict[str, Any]:
            nonlocal active_reviews, peak_reviews
            system = str(kwargs.get("system") or "")
            if "盲检复核器" in system:
                review_systems.append(system)
                active_reviews += 1
                peak_reviews = max(peak_reviews, active_reviews)
                try:
                    await asyncio.sleep(0.02)
                finally:
                    active_reviews -= 1
            return {
                "overall_description": "未见异常",
                "timeline": [],
                "abnormal_candidates": [],
                "keyframe_timestamps": [2, 5, 8],
                "limitations": [],
            }

    workflow.client = ReviewClient()  # type: ignore[assignment]
    result = asyncio.run(
        workflow._understand_video(
            _registry().get("home-health-check-gait"),
            media,
            PetContext(pet_name="警长", species="cat"),
            "测试 Prompt",
            10.0,
        )
    )

    assert peak_reviews == 1
    assert len(review_systems) == 1
    assert "上一轮结果" not in review_systems[0]
    assert result["normality_review_channels"] == ["native_blind_repeat"]
    assert result["normality_review_frame_count"] == 0
    assert result["normality_consistency"] == "consistent_normal"


def test_step1_challenges_normal_result_and_routes_abnormal_timestamp(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    workflow = HomeCheckWorkflow(
        workspace,
        ModelSettings("qwen3.7-plus", "https://model.example/v1", "secret"),
        MediaProcessor(tmp_path / "runtime"),
    )
    video_path = tmp_path / "walk.mp4"
    video_path.write_bytes(b"video")
    media = MediaArtifact("video", video_path, "video/mp4", 5, 320, 240, duration=10.0, fps=25.0)
    frame = tmp_path / "review.jpg"
    Image.new("RGB", (32, 32), "white").save(frame)
    workflow.media_processor.extract_frames = lambda _media, timestamps, _purpose: [frame] * len(timestamps)  # type: ignore[method-assign]
    calls: list[Any] = []

    class NormalThenAbnormalClient:
        async def complete(self, content: Any, **_kwargs: Any) -> dict[str, Any]:
            calls.append(content)
            if len(calls) == 1:
                return {
                    "overall_description": "未见异常",
                    "timeline": [],
                    "abnormal_candidates": [],
                    "keyframe_timestamps": [2, 5, 8],
                    "limitations": [],
                }
            return {
                "overall_description": "末段疑似后肢异常",
                "timeline": [],
                "abnormal_candidates": [
                    {"start_seconds": 8, "end_seconds": 10, "timestamp": 9.25, "signal": "后肢拖行", "evidence": "连续两步", "confidence": "medium"}
                ],
                "keyframe_timestamps": [9.25, 8.5, 9.75],
                "limitations": [],
            }

    workflow.client = NormalThenAbnormalClient()  # type: ignore[assignment]
    result = asyncio.run(
        workflow._understand_video(
            _registry().get("home-health-check-gait"), media, PetContext(pet_name="警长"), "测试 Prompt", 6.0
        )
    )

    assert len(calls) == 2
    assert calls[0][0]["type"] == "video_url"
    assert calls[1][0]["type"] == "video_url"
    assert result["normality_review_triggered"] is True
    assert result["normality_review_changed"] is True
    assert result["normality_review_frame_count"] == 0
    assert result["normality_review_channels"] == ["native_blind_repeat"]
    assert result["normality_consistency"] == "conflict"
    assert result["normality_review_initial"]["overall_description"] == "未见异常"
    assert workflow._select_timestamps(result, 10.0) == [9.25, 8.5, 9.75]


def test_blind_normality_review_failure_becomes_inconclusive_without_fallback(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    workflow = HomeCheckWorkflow(
        workspace,
        ModelSettings("qwen3.7-plus", "https://model.example/v1", "secret"),
        MediaProcessor(tmp_path / "runtime"),
    )
    video_path = tmp_path / "walk.mp4"
    video_path.write_bytes(b"video")
    media = MediaArtifact("video", video_path, "video/mp4", 5, 320, 240, duration=10.0, fps=25.0)
    frame = tmp_path / "review.jpg"
    Image.new("RGB", (32, 32), "white").save(frame)
    workflow.media_processor.extract_frames = lambda _media, timestamps, _purpose: [frame] * len(timestamps)  # type: ignore[method-assign]
    calls: list[Any] = []

    class ReviewTimesOut:
        async def complete(self, content: Any, **_kwargs: Any) -> dict[str, Any]:
            calls.append(content)
            if len(calls) == 1:
                return {
                    "overall_description": "未见异常",
                    "timeline": [],
                    "abnormal_candidates": [],
                    "keyframe_timestamps": [2, 5, 8],
                    "limitations": [],
                }
            raise httpx.ReadTimeout("review stalled")

    workflow.client = ReviewTimesOut()  # type: ignore[assignment]
    result = asyncio.run(
        workflow._understand_video(
            _registry().get("home-health-check-gait"), media, PetContext(pet_name="警长"), "测试 Prompt", 10.0
        )
    )

    assert len(calls) == 2
    assert calls[0][0]["type"] == "video_url"
    assert calls[1][0]["type"] == "video_url"
    assert result["normality_review_changed"] is False
    assert result["normality_review_channels"] == ["native_blind_repeat"]
    assert result["normality_consistency"] == "review_incomplete"
    assert result["normality_review_error"] == "native_read_timeout"


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
            if isinstance(content, list) and content and "最终综合步骤" not in content[0].get("text", ""):
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
    assert "影像可见" in calls[1][0]["text"]
    assert "https://cdn.fura.example" not in calls[1][0]["text"]
    assert any(item.get("type") == "image_url" for item in calls[1])
    assert result["_workflow_evidence"]["image_observations"]


def test_hard_limit_dense_normal_is_marked_unconfirmed(tmp_path: Path) -> None:
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
    media = MediaArtifact(
        "video", video_path, "video/mp4", 5, 720, 1280, duration=13.5, fps=25.0
    )
    extracted: list[tuple[str, int]] = []

    def extract_dense(_media, timestamps, purpose="evidence"):
        extracted.append((purpose, len(timestamps)))
        return [frame] * len(timestamps)

    workflow.media_processor.extract_frames = extract_dense  # type: ignore[method-assign]
    calls: list[Any] = []

    class DenseNormalClient:
        async def complete(self, content: Any, **_kwargs: Any) -> dict[str, Any]:
            calls.append(content)
            return {
                "overall_description": "密集时间轴未见明确异常",
                "timeline": [],
                "abnormal_candidates": [],
                "keyframe_timestamps": [2, 6, 10],
                "limitations": [],
            }

    workflow.client = DenseNormalClient()  # type: ignore[assignment]
    result = asyncio.run(
        workflow._understand_video(
            _registry().get("home-health-check-gait"),
            media,
            PetContext(pet_name="警长"),
            "测试 Prompt",
            10.0,
        )
    )

    assert len(calls) == 1
    assert calls[0][0]["type"] == "video"
    assert extracted == [("storyboard-hard-limit", 72)]
    assert result["runtime_provider"] == "ffmpeg_dense_timeline"
    assert result["normality_consistency"] == "dense_normal_unconfirmed"
    assert "无法据此准确确认" in result["limitations"][-1]


def test_native_first_sse_is_recorded_as_telemetry_only(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    workflow = HomeCheckWorkflow(
        workspace,
        ModelSettings("qwen3.7-plus", "https://model.example/v1", "secret"),
        MediaProcessor(tmp_path / "runtime"),
    )
    video_path = tmp_path / "walk.mp4"
    video_path.write_bytes(b"video")
    media = MediaArtifact("video", video_path, "video/mp4", 5, 320, 240, duration=4.0, fps=25.0)
    workflow.media_processor.extract_frames = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("SSE telemetry must never start a dense-frame route")
    )

    class TimelySseClient:
        async def complete(self, content: Any, **kwargs: Any) -> dict[str, Any]:
            assert content[0]["type"] == "video_url"
            kwargs["first_sse_event"].set()
            await asyncio.sleep(0.02)
            return {
                "overall_description": "原生视频发现重复异常",
                "timeline": [],
                "abnormal_candidates": [{"timestamp": 2, "signal": "推进不足"}],
                "keyframe_timestamps": [1, 2, 3],
            }

    workflow.client = TimelySseClient()  # type: ignore[assignment]
    result = asyncio.run(
        workflow._understand_video(
            _registry().get("home-health-check-gait"),
            media,
            PetContext(pet_name="警长"),
            "测试 Prompt",
            10.0,
        )
    )

    assert result["runtime_provider"] == "qwen_native_video"
    assert result["native_first_sse_observed"] is True


def test_missing_first_sse_does_not_replace_native_video(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    processor = MediaProcessor(tmp_path / "runtime")
    workflow = HomeCheckWorkflow(
        workspace,
        ModelSettings("qwen3.7-plus", "https://model.example/v1", "secret"),
        processor,
    )
    video_path = tmp_path / "walk.mp4"
    video_path.write_bytes(b"video")
    media = MediaArtifact("video", video_path, "video/mp4", 5, 320, 240, duration=4.0, fps=25.0)
    processor.extract_frames = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("missing SSE must not start dense frames")
    )
    calls: list[str] = []

    class NativeWithoutSseClient:
        async def complete(self, content: Any, **_kwargs: Any) -> dict[str, Any]:
            calls.append(content[0]["type"])
            await asyncio.sleep(0.02)
            return {
                "overall_description": "原生视频发现重复异常",
                "timeline": [],
                "abnormal_candidates": [{"timestamp": 2, "signal": "推进不足"}],
                "keyframe_timestamps": [1, 2, 3],
            }

    workflow.client = NativeWithoutSseClient()  # type: ignore[assignment]
    result = asyncio.run(
        workflow._understand_video(
            _registry().get("home-health-check-gait"),
            media,
            PetContext(pet_name="警长"),
            "测试 Prompt",
            10.0,
        )
    )

    assert calls == ["video_url"]
    assert result["runtime_provider"] == "qwen_native_video"
    assert result["native_first_sse_observed"] is False


def test_behavior_native_timeout_does_not_start_dense_frames(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    workflow = HomeCheckWorkflow(
        workspace,
        ModelSettings("qwen3.7-plus", "https://model.example/v1", "secret"),
        MediaProcessor(tmp_path / "runtime"),
    )
    video_path = tmp_path / "behavior.mp4"
    video_path.write_bytes(b"video")
    media = MediaArtifact("video", video_path, "video/mp4", 5, 320, 240, duration=4.0, fps=25.0)
    workflow.media_processor.extract_frames = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("native timeout must not start dense frames")
    )

    class TimeoutClient:
        async def complete(self, _content: Any, **_kwargs: Any) -> dict[str, Any]:
            raise httpx.ReadTimeout("native timeout")

    workflow.client = TimeoutClient()  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="NATIVE_VIDEO_UNAVAILABLE"):
        asyncio.run(
            workflow._understand_video(
                _registry().get("home-health-check-behavior"),
                media,
                PetContext(pet_name="警长"),
                "测试 Prompt",
                4.0,
            )
        )


def test_native_read_timeout_fails_without_dense_attempt(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    processor = MediaProcessor(tmp_path / "runtime")
    workflow = HomeCheckWorkflow(
        workspace,
        ModelSettings("qwen3.7-plus", "https://model.example/v1", "secret"),
        processor,
    )
    video_path = tmp_path / "walk.mp4"
    video_path.write_bytes(b"video")
    media = MediaArtifact("video", video_path, "video/mp4", 5, 720, 1280, duration=7.57, fps=30.0)
    processor.extract_frames = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("native timeout must not start dense frames")
    )
    calls: list[tuple[str, int, float]] = []

    class NativeTimeoutClient:
        async def complete(self, content: Any, **kwargs: Any) -> dict[str, Any]:
            content_type = content[0]["type"]
            frame_count = len(content[0].get("video", []))
            calls.append((content_type, frame_count, kwargs["timeout_seconds"]))
            raise httpx.ReadTimeout("provider long tail")

    workflow.client = NativeTimeoutClient()  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="NATIVE_VIDEO_UNAVAILABLE") as captured:
        asyncio.run(
            workflow._understand_video(
                _registry().get("home-health-check-gait"),
                media,
                PetContext(pet_name="呼呼"),
                "测试 Prompt",
                10.0,
            )
        )

    assert getattr(captured.value, "code") == "NATIVE_VIDEO_UNAVAILABLE"
    assert calls == [("video_url", 0, 210.0)]


def test_native_connect_timeout_is_retried_once_before_success(tmp_path: Path) -> None:
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
                raise httpx.ConnectTimeout("native video connect timeout")
            return {
                "overall_description": "完整视频观察",
                "timeline": [],
                "abnormal_candidates": [{"timestamp": 4, "signal": "疑似异常"}],
                "keyframe_timestamps": [1, 4, 6],
            }

    workflow.client = TimeoutThenNativeClient()  # type: ignore[assignment]
    result = asyncio.run(
        workflow._understand_video(
            _registry().get("home-health-check-gait"), media, PetContext(pet_name="警长"), "测试 Prompt", 4.0
        )
    )

    assert calls == 2
    assert result["runtime_provider"] == "qwen_native_video"
    assert result["native_attempts"] == 2
    assert result["native_attempt_diagnostics"][0]["reason_code"] == "native_connect_timeout"
    assert result["native_attempt_diagnostics"][1]["outcome"] == "completed"


def test_native_http_413_recompresses_from_source_before_retry(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    processor = MediaProcessor(tmp_path / "runtime")
    workflow = HomeCheckWorkflow(
        workspace,
        ModelSettings("qwen3.7-plus", "https://model.example/v1", "secret"),
        processor,
    )
    source = tmp_path / "walk.mp4"
    source.write_bytes(b"source-video")
    high_quality = tmp_path / "upright-high.mp4"
    high_quality.write_bytes(b"large-proxy")
    compact = tmp_path / "upright-compact.mp4"
    compact.write_bytes(b"small")
    media = MediaArtifact(
        "video",
        source,
        "video/mp4",
        source.stat().st_size,
        720,
        1280,
        duration=10.67,
        fps=30.0,
        rotation_degrees=-90.0,
    )
    normalization_targets: list[int | None] = []

    def normalize(_media, target_bytes=None):
        normalization_targets.append(target_bytes)
        return high_quality if target_bytes is None else compact

    processor.normalize_video_orientation = normalize  # type: ignore[method-assign]
    calls: list[Any] = []

    class PayloadThenNativeClient:
        async def complete(self, content: Any, **_kwargs: Any) -> dict[str, Any]:
            calls.append(content)
            if len(calls) == 1:
                request = httpx.Request("POST", "https://model.example/v1/chat/completions")
                response = httpx.Response(413, request=request)
                raise httpx.HTTPStatusError("payload too large", request=request, response=response)
            return {
                "overall_description": "完整视频观察",
                "timeline": [],
                "abnormal_candidates": [{"timestamp": 4, "signal": "后肢推进不足"}],
                "keyframe_timestamps": [2, 4, 6],
            }

    workflow.client = PayloadThenNativeClient()  # type: ignore[assignment]
    result = asyncio.run(
        workflow._understand_video(
            _registry().get("home-health-check-gait"),
            media,
            PetContext(pet_name="警长"),
            "测试 Prompt",
            10.0,
        )
    )

    assert normalization_targets == [None, 7 * 1024 * 1024]
    assert len(calls) == 2
    assert base64.b64decode(calls[1][0]["video_url"]["url"].split(",", 1)[1]) == b"small"
    assert result["runtime_provider"] == "qwen_native_video"
    assert result["native_attempts"] == 2
    assert result["native_payload_recompressed"] is True
    assert result["native_input_bytes"] == compact.stat().st_size
    assert result["native_attempt_diagnostics"][0]["recovery"] == "payload_recompressed"


def test_slow_native_request_is_not_duplicated_or_lowered(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    processor = MediaProcessor(tmp_path / "runtime")
    workflow = HomeCheckWorkflow(
        workspace,
        ModelSettings("qwen3.7-plus", "https://model.example/v1", "secret"),
        processor,
    )
    video_path = tmp_path / "walk.mp4"
    video_path.write_bytes(b"video")
    media = MediaArtifact("video", video_path, "video/mp4", 5, 320, 240, duration=8.0, fps=25.0)
    calls: list[dict[str, Any]] = []

    class SlowThenFastClient:
        async def complete(self, _content: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            await asyncio.sleep(0.03)
            return {
                "overall_description": "连续视频发现重复后肢异常",
                "timeline": [],
                "abnormal_candidates": [
                    {
                        "timestamp": 4,
                        "signal": "推进不足",
                        "evidence": "连续三个周期均可见",
                        "confidence": "high",
                    }
                ],
                "keyframe_timestamps": [2, 4, 6],
            }

    workflow.client = SlowThenFastClient()  # type: ignore[assignment]
    result = asyncio.run(
        workflow._understand_video(
            _registry().get("home-health-check-gait"),
            media,
            PetContext(pet_name="警长"),
            "测试 Prompt",
            10.0,
        )
    )

    assert result["native_attempts"] == 1
    assert len(calls) == 1
    assert all(call["reasoning_budget"] == 4096 for call in calls)
    assert all(call["max_tokens"] == 3200 for call in calls)


def test_native_timeout_reason_codes_are_phase_specific() -> None:
    workflow = HomeCheckWorkflow.__new__(HomeCheckWorkflow)

    assert workflow._native_failure_code(httpx.ConnectTimeout("connect")) == "native_connect_timeout"
    assert workflow._native_failure_code(httpx.WriteTimeout("write")) == "native_write_timeout"
    assert workflow._native_failure_code(httpx.ReadTimeout("read")) == "native_read_timeout"
    assert workflow._native_failure_code(httpx.PoolTimeout("pool")) == "native_pool_timeout"
    total_timeout = httpx.ReadTimeout("total")
    setattr(total_timeout, "timeout_phase", "total")
    assert workflow._native_failure_code(total_timeout) == "native_total_timeout"


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
            return {
                "overall_description": "完整视频观察",
                "timeline": [],
                "abnormal_candidates": [{"timestamp": 4, "signal": "疑似异常"}],
                "keyframe_timestamps": [1, 4, 6],
            }

    workflow.client = InvalidThenValidClient()  # type: ignore[assignment]
    result = asyncio.run(
        workflow._understand_video(
            _registry().get("home-health-check-gait"), media, PetContext(pet_name="警长"), "测试 Prompt", 4.0
        )
    )

    assert calls == 2
    assert result["runtime_provider"] == "qwen_native_video"
    assert result["native_attempts"] == 2
