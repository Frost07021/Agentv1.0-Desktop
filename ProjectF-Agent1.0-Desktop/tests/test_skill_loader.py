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
