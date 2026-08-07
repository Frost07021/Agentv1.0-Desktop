from __future__ import annotations

import re

from .skill_loader import SkillDefinition


def runtime_skill_contract(skill: SkillDefinition) -> str:
    """Return executable Skill instructions without sample answers that can bias evidence."""
    content = re.sub(r"^---\s*\n.*?\n---\s*\n", "", skill.content, count=1, flags=re.DOTALL).strip()
    content = re.sub(
        r"\n## 六、完整 JSON 示例.*?(?=\n## 七、|\Z)",
        "\n",
        content,
        flags=re.DOTALL,
    )
    if skill.name == "pet-report-analysis":
        content = re.sub(
            r"(## 输出 JSON Schema\s*\n.*?\n)```json.*?```",
            r"\1示例值不参与本次分析；字段结构与枚举以下方字段详细说明为准。",
            content,
            count=1,
            flags=re.DOTALL,
        )
    return content.strip()
