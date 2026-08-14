import asyncio
from pathlib import Path

import httpx

from app.config import ModelSettings
from app.media import MediaArtifact
from app.model_adapter import FallbackVideoAdapter, QwenNativeVideoAdapter
from app.schemas import PetContext
from app.skill_loader import SkillDefinition


def _skill(tmp_path: Path, name: str = "home-health-check-gait") -> SkillDefinition:
    return SkillDefinition(name=name, description="test", content="# Test Skill", path=tmp_path / "SKILL.md", reference=None)


def _video(tmp_path: Path) -> MediaArtifact:
    path = tmp_path / "pet.mp4"
    path.write_bytes(b"fake-video-bytes")
    return MediaArtifact(
        type="video",
        path=path,
        mime_type="video/mp4",
        size_bytes=path.stat().st_size,
        width=640,
        height=480,
        duration=8.0,
        fps=30.0,
        keyframes=[],
    )


def test_native_video_payload_uses_video_url_and_category_fps(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AGENT_VIDEO_FPS", raising=False)
    adapter = QwenNativeVideoAdapter(ModelSettings("qwen3.7-plus", "https://model.example/v1", "secret"))
    content, fps = adapter.build_content(_skill(tmp_path), _video(tmp_path), PetContext(pet_name="警长"))
    assert fps == 10.0
    assert content[0]["type"] == "video_url"
    assert content[0]["video_url"]["url"].startswith("data:video/mp4;base64,")
    assert content[0]["video_url"]["fps"] == 10.0
    assert content[0]["max_pixels"] == 655360
    assert content[0]["total_pixels"] == 67108864


def test_behavior_uses_lower_default_fps(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AGENT_VIDEO_FPS", raising=False)
    adapter = QwenNativeVideoAdapter(ModelSettings("qwen3.7-plus", "https://model.example/v1", "secret"))
    _, fps = adapter.build_content(
        _skill(tmp_path, "home-health-check-behavior"), _video(tmp_path), PetContext(pet_name="警长")
    )
    assert fps == 4.0


def test_native_video_failure_falls_back_to_frames(tmp_path: Path) -> None:
    class Primary:
        async def analyze(self, skill, media, pet):
            raise httpx.HTTPError("native unavailable")

    class Fallback:
        async def analyze(self, skill, media, pet):
            return {"report_meta": {}}

    adapter = FallbackVideoAdapter(Primary(), Fallback())  # type: ignore[arg-type]
    result = asyncio.run(adapter.analyze(_skill(tmp_path), _video(tmp_path), PetContext(pet_name="警长")))
    runtime = result["report_meta"]["analysis_runtime"]
    assert runtime["video_provider"] == "ffmpeg_frames"
    assert runtime["fallback"] is True
