from pathlib import Path

from app.skill_loader import SkillRegistry
from app.skill_prompt import runtime_skill_contract


ROOT = Path(__file__).resolve().parents[1]


def test_home_runtime_contracts_keep_rules_but_drop_complete_sample_answers() -> None:
    registry = SkillRegistry(ROOT / "skill-definitions")
    for name in (
        "home-health-check-dental",
        "home-health-check-stool",
        "home-health-check-gait",
        "home-health-check-behavior",
        "home-health-check-xray",
    ):
        skill = registry.get(name)
        contract = runtime_skill_contract(skill)
        assert "视觉识别 Prompt 指令" in contract
        assert "状态标签颜色规范" in contract
        assert "## 六、完整 JSON 示例" not in contract
        assert "https://cdn.fura.example" not in contract


def test_report_runtime_contract_drops_example_values_but_keeps_field_rules() -> None:
    skill = SkillRegistry(ROOT / "skill-definitions").get("pet-report-analysis")
    contract = runtime_skill_contract(skill)
    assert "### `indicators`" in contract
    assert "报告类型自动识别" in contract
    assert '"WBC(白细胞计数)"' not in contract
    assert "https://xxx/report_page1.jpg" not in contract
