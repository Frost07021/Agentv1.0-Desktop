from pathlib import Path

from app.config import model_settings_from_environment


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
