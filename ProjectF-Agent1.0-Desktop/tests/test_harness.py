import asyncio
import shutil
from pathlib import Path

import pytest
from PIL import Image

from app.harness import AnalysisExecutionError, Harness
from app.media import MediaArtifact
from app.schemas import PetContext, TaskRequest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_report_fake_end_to_end(tmp_path: Path) -> None:
    image_path = tmp_path / "report.png"
    Image.new("RGB", (800, 600), "white").save(image_path)
    request = TaskRequest(
        skill_name="pet-report-analysis",
        media_path=str(image_path),
        pet=PetContext(pet_id="pet_demo", pet_name="警长"),
        mode="fake",
    )
    response = asyncio.run(Harness(PROJECT_ROOT, tmp_path / "output").execute(request))
    assert response.status == "completed"
    assert response.result is not None
    assert response.result["report_meta"]["report_type"] == "生化检查"
    assert response.result["report_meta"]["raw_images"] == [str(image_path.resolve())]
    assert Path(response.output_file or "").exists()
    assert [trace.status for trace in response.traces] == ["completed"] * 6


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="pdftoppm 未安装")
def test_report_pdf_fake_end_to_end_uses_all_converted_pages(tmp_path: Path) -> None:
    pdf_path = tmp_path / "report.pdf"
    pages = [Image.new("RGB", (800, 600), color) for color in ("white", "lightgray")]
    pages[0].save(pdf_path, "PDF", save_all=True, append_images=pages[1:], resolution=100)
    request = TaskRequest(
        skill_name="pet-report-analysis",
        media_path=str(pdf_path),
        pet=PetContext(pet_id="pet_demo", pet_name="警长"),
        mode="fake",
    )

    response = asyncio.run(Harness(PROJECT_ROOT, tmp_path / "output").execute(request))

    assert response.status == "completed"
    assert response.result is not None
    raw_images = response.result["report_meta"]["raw_images"]
    assert len(raw_images) == 2
    assert all(Path(path).is_file() and Path(path).suffix == ".jpg" for path in raw_images)


def test_failed_analysis_writes_step_diagnostic(tmp_path: Path) -> None:
    harness = Harness(PROJECT_ROOT, tmp_path / "output")
    harness.workspace_root = tmp_path
    harness.registry = harness.registry.__class__(PROJECT_ROOT / "skill-definitions")
    request = TaskRequest(
        skill_name="home-health-check-gait",
        media_path="missing.mp4",
        pet=PetContext(pet_name="警长"),
        mode="fake",
    )

    with pytest.raises(AnalysisExecutionError) as raised:
        asyncio.run(harness.execute(request))

    failure_file = raised.value.failure_file
    assert failure_file.is_file()
    diagnostic = failure_file.read_text(encoding="utf-8")
    assert '"step_id": "prepare_media"' in diagnostic
    assert "MEDIA_NOT_FOUND" in diagnostic


def test_video_runtime_quality_is_preserved_without_provider_identity(tmp_path: Path) -> None:
    video_path = tmp_path / "walk.mp4"
    video_path.write_bytes(b"video")
    media = MediaArtifact("video", video_path, "video/mp4", 5, 320, 240, duration=8.0, fps=25.0)
    request = TaskRequest(
        skill_name="home-health-check-gait",
        media_path=str(video_path),
        pet=PetContext(pet_name="警长"),
        mode="real",
    )
    result = {
        "report_meta": {
            "analysis_runtime": {
                "video_provider": "private-provider-name",
                "native_video": False,
                "analysis_quality": "degraded_dense_storyboard",
                "storyboard_frame_count": 12,
                "native_attempts": 1,
                "fallback_reason_code": "native_timeout",
            }
        }
    }

    normalized = Harness._normalize_result(request, media, result)
    runtime = normalized["report_meta"]["analysis_runtime"]
    assert runtime["analysis_quality"] == "degraded_dense_storyboard"
    assert runtime["storyboard_frame_count"] == 12
    assert "video_provider" not in runtime
