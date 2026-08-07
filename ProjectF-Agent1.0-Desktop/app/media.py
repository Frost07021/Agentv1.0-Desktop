from __future__ import annotations

import hashlib
import mimetypes
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import imageio_ffmpeg
from PIL import Image


class MediaError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class MediaArtifact:
    type: str
    path: Path
    mime_type: str
    size_bytes: int
    width: int
    height: int
    duration: float | None = None
    fps: float | None = None
    keyframes: list[Path] = field(default_factory=list)


class MediaProcessor:
    IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
    VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
    PDF_EXTENSIONS = {".pdf"}

    def __init__(self, runtime_dir: Path, max_video_seconds: float = 15.0):
        self.runtime_dir = runtime_dir
        self.max_video_seconds = max_video_seconds
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

    def prepare(self, path: Path) -> MediaArtifact:
        path = path.resolve()
        if not path.is_file():
            raise MediaError("MEDIA_NOT_FOUND", f"媒体文件不存在: {path}")
        suffix = path.suffix.lower()
        if suffix in self.IMAGE_EXTENSIONS:
            return self._prepare_image(path)
        if suffix in self.VIDEO_EXTENSIONS:
            return self._prepare_video(path)
        if suffix in self.PDF_EXTENSIONS:
            return self._prepare_pdf(path)
        raise MediaError("MEDIA_UNSUPPORTED", f"不支持的媒体格式: {suffix}")

    def _prepare_image(self, path: Path) -> MediaArtifact:
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                width, height = image.size
                detected_mime = image.get_format_mimetype()
        except Exception as exc:
            raise MediaError("MEDIA_UNREADABLE", f"图片无法读取: {exc}") from exc
        mime = detected_mime or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return MediaArtifact("image", path, mime, path.stat().st_size, width, height)

    def _prepare_video(self, path: Path) -> MediaArtifact:
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        process = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        probe = process.stderr
        duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", probe)
        video_match = re.search(r"Video:.*?(\d{2,5})x(\d{2,5}).*?(\d+(?:\.\d+)?)\s*fps", probe)
        if not duration_match or not video_match:
            raise MediaError("MEDIA_UNREADABLE", "无法读取视频时长、分辨率或帧率")
        hours, minutes, seconds = duration_match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        width, height, fps = video_match.groups()
        if duration > self.max_video_seconds:
            raise MediaError("MEDIA_TOO_LONG", f"视频时长 {duration:.2f}s 超过 {self.max_video_seconds:.0f}s 限制")

        digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:10]
        frame_dir = self.runtime_dir / "frames" / f"{path.stem}-{digest}"
        frame_dir.mkdir(parents=True, exist_ok=True)
        timestamps = self._keyframe_timestamps(duration)
        frames: list[Path] = []
        for index, timestamp in enumerate(timestamps, start=1):
            output = frame_dir / f"frame_{index}_{timestamp:.2f}s.jpg"
            command = [
                ffmpeg,
                "-y",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(output),
            ]
            result = subprocess.run(command, capture_output=True, check=False)
            if result.returncode != 0 or not output.exists():
                raise MediaError("MEDIA_FRAME_EXTRACTION_ERROR", f"第 {index} 个关键帧提取失败")
            frames.append(output)

        mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
        return MediaArtifact(
            "video", path, mime, path.stat().st_size, int(width), int(height), duration, float(fps), frames
        )

    def compress_video_to_limit(self, path: Path, target_bytes: int = 50 * 1024 * 1024) -> Path:
        """Transcode an MP4/MOV locally so the actual model input stays below its byte limit."""
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        probe = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        ).stderr
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", probe)
        if not match:
            raise MediaError("MEDIA_UNREADABLE", "无法读取视频时长，不能执行智能压缩")
        hours, minutes, seconds = match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        if duration <= 0 or duration > self.max_video_seconds:
            raise MediaError("MEDIA_TOO_LONG", f"视频时长需在 5–{self.max_video_seconds:.0f} 秒范围内")
        output = path.with_name(f"{path.stem}-compressed.mp4")
        video_bitrate = max(500_000, min(8_000_000, int((target_bytes * 8 / duration) * 0.84) - 96_000))
        last_error = ""
        for attempt in range(2):
            command = [
                ffmpeg, "-y", "-i", str(path), "-map", "0:v:0", "-c:v", "libx264", "-preset", "veryfast",
                "-b:v", str(video_bitrate), "-maxrate", str(video_bitrate), "-bufsize", str(video_bitrate * 2),
                "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart", str(output),
            ]
            result = subprocess.run(command, capture_output=True, check=False)
            if result.returncode == 0 and output.is_file() and 0 < output.stat().st_size <= target_bytes:
                return output
            last_error = result.stderr.decode("utf-8", errors="replace").strip().splitlines()[-1] if result.stderr else ""
            output.unlink(missing_ok=True)
            video_bitrate = max(500_000, int(video_bitrate * 0.72))
        detail = f"（转换器提示：{last_error[:120]}）" if last_error else ""
        raise MediaError("MEDIA_COMPRESSION_FAILED", f"视频压缩未生成可用文件，请裁剪后重试{detail}")

    def _prepare_pdf(self, path: Path) -> MediaArtifact:
        converter = shutil.which("pdftoppm")
        if not converter:
            raise MediaError("PDF_CONVERTER_MISSING", "PDF 报告分析需要 pdftoppm")
        converter_path = Path(converter)
        if converter_path.suffix.lower() in {".cmd", ".bat"}:
            bundled_exe = converter_path.resolve().parents[2] / "native" / "poppler" / "Library" / "bin" / "pdftoppm.exe"
            if bundled_exe.is_file():
                converter = str(bundled_exe)
        digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:10]
        page_dir = self.runtime_dir / "pdf_pages" / f"{path.stem}-{digest}"
        page_dir.mkdir(parents=True, exist_ok=True)
        prefix = page_dir / "page"
        result = subprocess.run(
            [converter, "-jpeg", "-r", "200", str(path), str(prefix)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        pages = sorted(page_dir.glob("page-*.jpg"))
        if result.returncode != 0 or not pages:
            raise MediaError("PDF_CONVERSION_ERROR", f"PDF 转图片失败: {result.stderr.strip()}")
        with Image.open(pages[0]) as image:
            width, height = image.size
        return MediaArtifact("pdf", path, "application/pdf", path.stat().st_size, width, height, keyframes=pages)

    def extract_frames(
        self,
        media: MediaArtifact,
        timestamps: list[float],
        purpose: str = "evidence",
    ) -> list[Path]:
        if media.type != "video":
            raise MediaError("MEDIA_TYPE_MISMATCH", "只有视频可以提取关键帧")
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        digest = hashlib.sha1(str(media.path).encode("utf-8")).hexdigest()[:10]
        frame_dir = self.runtime_dir / "frames" / f"{media.path.stem}-{digest}" / purpose
        frame_dir.mkdir(parents=True, exist_ok=True)
        frames: list[Path] = []
        for index, timestamp in enumerate(timestamps, start=1):
            output = frame_dir / f"frame_{index}_{timestamp:.3f}s.jpg"
            result = subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-ss",
                    f"{timestamp:.3f}",
                    "-i",
                    str(media.path),
                    "-frames:v",
                    "1",
                    "-q:v",
                    "2",
                    str(output),
                ],
                capture_output=True,
                check=False,
            )
            if result.returncode != 0 or not output.exists():
                raise MediaError("MEDIA_FRAME_EXTRACTION_ERROR", f"{timestamp:.3f}s 关键帧提取失败")
            frames.append(output)
        return frames

    @staticmethod
    def _keyframe_timestamps(duration: float) -> list[float]:
        if duration <= 0.3:
            return [0.0]
        end = max(0.0, duration - 0.15)
        return [round(min(end, duration * ratio), 3) for ratio in (0.2, 0.5, 0.8)]

    @staticmethod
    def uniform_timestamps(duration: float, count: int) -> list[float]:
        """Return ordered samples covering the entire playable video range."""
        if duration <= 0 or count <= 1:
            return [0.0]
        end = max(0.0, duration - 0.05)
        return [round(end * index / (count - 1), 3) for index in range(count)]
