from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    description: str
    content: str
    path: Path
    reference: dict[str, Any] | None


class SkillRegistry:
    def __init__(self, root: Path):
        self.root = root
        self._skills: dict[str, SkillDefinition] = {}
        self.reload()

    @staticmethod
    def _parse_front_matter(content: str) -> dict[str, Any]:
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        if not match:
            raise ValueError("SKILL.md 缺少 YAML Front Matter")
        metadata = yaml.safe_load(match.group(1)) or {}
        if not metadata.get("name"):
            raise ValueError("SKILL.md 缺少 name")
        return metadata

    def reload(self) -> None:
        loaded: dict[str, SkillDefinition] = {}
        for skill_file in self.root.glob("*/SKILL.md"):
            content = skill_file.read_text(encoding="utf-8")
            metadata = self._parse_front_matter(content)
            references = list((skill_file.parent / "references").glob("*.json"))
            reference = None
            if references:
                reference = json.loads(references[0].read_text(encoding="utf-8"))
            definition = SkillDefinition(
                name=metadata["name"],
                description=str(metadata.get("description", "")).strip(),
                content=content,
                path=skill_file,
                reference=reference,
            )
            loaded[definition.name] = definition
        self._skills = loaded

    def get(self, name: str) -> SkillDefinition:
        if name not in self._skills:
            raise KeyError(f"Skill 不存在: {name}")
        return self._skills[name]

    def list(self) -> list[dict[str, str]]:
        return [
            {"name": skill.name, "description": skill.description, "path": str(skill.path)}
            for skill in self._skills.values()
        ]

