import shutil
from pathlib import Path

import pytest
import imageio_ffmpeg
from PIL import Image
import subprocess

from app.media import MediaError, MediaProcessor


def test_image_is_validated_and_described(tmp_path: Path) -> None:
    image_path = tmp_path / "pet.webp"
    Image.new("RGB", (321, 456), "orange").save(image_path)
    artifact = MediaProcessor(tmp_path / "runtime").prepare(image_path)
    assert artifact.type == "image"
    assert artifact.width == 321
    assert artifact.height == 456
    assert artifact.mime_type == "image/webp"


def test_unsupported_media_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "payload.txt"
    path.write_text("not media", encoding="utf-8")
    with pytest.raises(MediaError) as error:
        MediaProcessor(tmp_path / "runtime").prepare(path)
    assert error.value.code == "MEDIA_UNSUPPORTED"


def test_uniform_video_timestamps_cover_full_duration() -> None:
    timestamps = MediaProcessor.uniform_timestamps(8.0, 5)
    assert timestamps == [0.0, 1.988, 3.975, 5.963, 7.95]


def test_video_can_be_transcoded_for_smart_upload(tmp_path: Path) -> None:
    source = tmp_path / "source.mov"
    subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x240:d=6",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    processor = MediaProcessor(tmp_path / "runtime")
    compressed = processor.compress_video_to_limit(source, target_bytes=2 * 1024 * 1024)
    assert compressed.suffix == ".mp4"
    assert compressed.is_file()
    assert compressed.stat().st_size <= 2 * 1024 * 1024


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="pdftoppm 未安装")
def test_xray_pdf_is_converted_to_page_images(tmp_path: Path) -> None:
    pdf_path = tmp_path / "xray.pdf"
    Image.new("RGB", (120, 80), "white").save(pdf_path, "PDF", resolution=100)
    artifact = MediaProcessor(tmp_path / "runtime").prepare(pdf_path)
    assert artifact.type == "pdf"
    assert artifact.mime_type == "application/pdf"
    assert len(artifact.keyframes) == 1
    assert artifact.keyframes[0].suffix == ".jpg"
