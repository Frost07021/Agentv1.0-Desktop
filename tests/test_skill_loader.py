from pathlib import Path

from app.skill_loader import SkillRegistry


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_loads_all_mvp_skill_definitions() -> None:
    registry = SkillRegistry(PROJECT_ROOT / "skill-definitions")
    names = {item["name"] for item in registry.list()}
    assert names == {
        "pet-report-analysis",
        "home-health-check-behavior",
        "home-health-check-dental",
        "home-health-check-gait",
        "home-health-check-stool",
        "home-health-check-xray",
        "structured-response",
    }
    assert registry.get("home-health-check-gait").reference["requires_keyframe_analysis"] is True
    assert registry.missing_required() == []


def test_reports_missing_required_skill_files(tmp_path: Path) -> None:
    registry = SkillRegistry(tmp_path)

    assert "home-health-check-gait" in registry.missing_required()
