from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModelSettings:
    model: str
    base_url: str
    api_key: str

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"


def _extract(pattern: str, text: str, label: str) -> str:
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        raise ValueError(f"无法从配置文件读取{label}")
    return match.group(1).strip()


def load_model_settings(config_path: Path) -> ModelSettings:
    """读取用户提供的兼容接口配置；不会把密钥复制到项目文件。"""
    text = config_path.read_bytes().decode("utf-8", errors="replace")
    model = _extract(r"(qwen[A-Za-z0-9._-]+)", text, "模型名")
    base_url = _extract(r"(https?://[^\s\ufffd]+)", text, "接口地址").rstrip("/；;，,")
    api_key = _extract(r"key[^A-Za-z0-9_-]*([A-Za-z0-9_-]{16,})", text, "API Key")
    return ModelSettings(model=model, base_url=base_url, api_key=api_key)


def default_workspace_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_local_environment(path: Path) -> None:
    """加载未提交的本地环境变量文件；已有进程环境变量始终优先。"""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(name, value)


def _load_external_environment_pointer(root: Path) -> None:
    """Load a user-selected env file without copying its secrets into the project."""
    pointer = root / ".runtime" / "model-config.path"
    if not pointer.is_file():
        return
    configured_path = pointer.read_text(encoding="utf-8").strip().strip('"').strip("'")
    if not configured_path:
        return
    external = Path(configured_path).expanduser()
    if not external.is_file():
        raise ValueError(f"真实模型配置文件不存在，请重新配置: {external}")
    _load_local_environment(external)


def model_settings_from_environment(workspace_root: Path | None = None) -> ModelSettings:
    root = workspace_root or default_workspace_root()
    _load_local_environment(root / ".env")
    _load_external_environment_pointer(root)
    env_url = os.getenv("AGENT_MODEL_BASE_URL")
    env_key = os.getenv("AGENT_MODEL_API_KEY")
    env_model = os.getenv("AGENT_MODEL_NAME")
    if env_url and env_key and env_model:
        return ModelSettings(model=env_model, base_url=env_url, api_key=env_key)

    config_path = os.getenv("AGENT_MODEL_CONFIG")
    if config_path:
        return load_model_settings(Path(config_path))
    missing = [
        name
        for name, value in (
            ("AGENT_MODEL_BASE_URL", env_url),
            ("AGENT_MODEL_API_KEY", env_key),
            ("AGENT_MODEL_NAME", env_model),
        )
        if not value
    ]
    raise ValueError(f"真实模型配置不完整，缺少环境变量: {', '.join(missing)}")


def configured_video_fps(default_fps: float) -> float:
    """Return the single effective Qwen sampling FPS for video workflows.

    Qwen's OpenAI-compatible video input accepts 0.1-10 FPS. Keeping the
    environment override in one place prevents cache keys, legacy adapters and
    the HomeCheck workflow from disagreeing about the actual request.
    """
    raw = os.getenv("AGENT_VIDEO_FPS")
    try:
        fps = float(raw) if raw is not None and raw.strip() else float(default_fps)
    except ValueError as exc:
        raise ValueError("AGENT_VIDEO_FPS 必须是 0.1 到 10 之间的数字") from exc
    if not 0.1 <= fps <= 10.0:
        raise ValueError("AGENT_VIDEO_FPS 必须在 0.1 到 10 之间")
    return fps
