from pathlib import Path

import pytest

from app.config import configured_video_fps, model_settings_from_environment


def test_video_fps_override_uses_qwen_supported_range(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_VIDEO_FPS", "8.5")
    assert configured_video_fps(10.0) == 8.5


@pytest.mark.parametrize("value", ["0.09", "10.01", "not-a-number"])
def test_video_fps_override_rejects_unsupported_values(monkeypatch, value: str) -> None:
    monkeypatch.setenv("AGENT_VIDEO_FPS", value)
    with pytest.raises(ValueError, match="AGENT_VIDEO_FPS"):
        configured_video_fps(10.0)


def test_loads_ignored_local_environment(monkeypatch, tmp_path: Path) -> None:
    for name in ("AGENT_MODEL_BASE_URL", "AGENT_MODEL_API_KEY", "AGENT_MODEL_NAME", "AGENT_MODEL_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text(
        "AGENT_MODEL_BASE_URL=https://model.example/v1\n"
        "AGENT_MODEL_API_KEY=test-secret-not-real\n"
        "AGENT_MODEL_NAME=qwen-test\n",
        encoding="utf-8",
    )
    settings = model_settings_from_environment(tmp_path)
    assert settings.model == "qwen-test"
    assert settings.chat_completions_url == "https://model.example/v1/chat/completions"
    assert settings.api_key == "test-secret-not-real"


def test_loads_external_env_pointer_without_copying_secrets(monkeypatch, tmp_path: Path) -> None:
    for name in ("AGENT_MODEL_BASE_URL", "AGENT_MODEL_API_KEY", "AGENT_MODEL_NAME", "AGENT_MODEL_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    external = tmp_path / "outside" / "env"
    external.parent.mkdir()
    external.write_text(
        "AGENT_MODEL_BASE_URL=https://external.example/v1\n"
        "AGENT_MODEL_API_KEY=external-secret-not-real\n"
        "AGENT_MODEL_NAME=qwen3.7-plus\n",
        encoding="utf-8",
    )
    pointer = tmp_path / ".runtime" / "model-config.path"
    pointer.parent.mkdir()
    pointer.write_text(str(external), encoding="utf-8")

    settings = model_settings_from_environment(tmp_path)

    assert settings.base_url == "https://external.example/v1"
    assert settings.api_key == "external-secret-not-real"
    assert settings.model == "qwen3.7-plus"
    assert not (tmp_path / ".env").exists()


def test_missing_external_env_pointer_target_has_actionable_error(monkeypatch, tmp_path: Path) -> None:
    for name in ("AGENT_MODEL_BASE_URL", "AGENT_MODEL_API_KEY", "AGENT_MODEL_NAME", "AGENT_MODEL_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    pointer = tmp_path / ".runtime" / "model-config.path"
    pointer.parent.mkdir()
    pointer.write_text(str(tmp_path / "missing-env"), encoding="utf-8")

    try:
        model_settings_from_environment(tmp_path)
    except ValueError as exc:
        assert "配置文件不存在" in str(exc)
    else:
        raise AssertionError("missing external configuration must fail")
