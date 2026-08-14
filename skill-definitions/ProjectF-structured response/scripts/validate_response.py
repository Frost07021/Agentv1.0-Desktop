#!/usr/bin/env python3
"""Validate ProjectF structured-response JSON with Python's standard library."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


SECTION_TITLES = [
    "🤗 暖心开场",
    "🔍 主要发现",
    "📚 简单解释",
    "💡 意见与建议",
    "🌈 温暖结语",
]

EXPECTED_BODY_TYPES = {
    SECTION_TITLES[0]: {"text"},
    SECTION_TITLES[1]: {"list_item"},
    SECTION_TITLES[2]: {"text"},
    SECTION_TITLES[3]: {"suggestion_item"},
    SECTION_TITLES[4]: {"text"},
}

ALLOWED_TYPES = {"section_title", "text", "list_item", "suggestion_item"}
MARKER_RE = re.compile(r"\[\[([^\[\]]+)\]\]")


def load_payload(path: str | None) -> Any:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    return json.load(sys.stdin)


def validate(payload: Any) -> list[str]:
    errors: list[str] = []

    if not isinstance(payload, dict):
        return ["top level must be an object"]
    if set(payload) != {"reply"}:
        errors.append("top level must contain only 'reply'")

    reply = payload.get("reply")
    if not isinstance(reply, dict):
        return errors + ["reply must be an object"]
    if set(reply) != {"emotion", "segments", "suggested_questions"}:
        errors.append("reply must contain only emotion, segments, suggested_questions")
    if reply.get("emotion") != "warm_empathy":
        errors.append("reply.emotion must equal 'warm_empathy'")

    segments = reply.get("segments")
    if not isinstance(segments, list):
        return errors + ["reply.segments must be an array"]

    actual_titles: list[str] = []
    current_section: str | None = None
    body_counts = {title: 0 for title in SECTION_TITLES}

    for index, segment in enumerate(segments):
        prefix = f"segments[{index}]"
        if not isinstance(segment, dict):
            errors.append(f"{prefix} must be an object")
            continue
        if set(segment) != {"type", "content", "highlights"}:
            errors.append(f"{prefix} must contain only type, content, highlights")

        segment_type = segment.get("type")
        content = segment.get("content")
        highlights = segment.get("highlights")

        if segment_type not in ALLOWED_TYPES:
            errors.append(f"{prefix}.type is invalid")
        if not isinstance(content, str) or not content:
            errors.append(f"{prefix}.content must be a non-empty string")
            content = ""
        elif len(content) > 200:
            errors.append(f"{prefix}.content exceeds 200 characters")

        if not isinstance(highlights, list) or any(
            not isinstance(item, str) or not item for item in highlights
        ):
            errors.append(f"{prefix}.highlights must be an array of non-empty strings")
            highlights = []
        elif len(highlights) != len(set(highlights)):
            errors.append(f"{prefix}.highlights contains duplicates")

        if segment_type == "section_title":
            actual_titles.append(content)
            current_section = content
            if highlights != []:
                errors.append(f"{prefix}.highlights must be [] for section_title")
            if MARKER_RE.search(content):
                errors.append(f"{prefix}.content must not contain highlight markers")
            continue

        if current_section is None:
            errors.append(f"{prefix} appears before the first section_title")
        elif current_section in EXPECTED_BODY_TYPES:
            body_counts[current_section] += 1
            if segment_type not in EXPECTED_BODY_TYPES[current_section]:
                expected = ", ".join(sorted(EXPECTED_BODY_TYPES[current_section]))
                errors.append(f"{prefix}.type must be {expected} under {current_section}")

        markers = MARKER_RE.findall(content)
        if len(markers) != len(set(markers)):
            errors.append(f"{prefix}.content contains duplicate highlight markers")
        if set(markers) != set(highlights):
            errors.append(
                f"{prefix} highlight mismatch: markers={markers!r}, highlights={highlights!r}"
            )
        if content.count("[[") != len(markers) or content.count("]]" ) != len(markers):
            errors.append(f"{prefix}.content contains malformed highlight markers")

    if actual_titles != SECTION_TITLES:
        errors.append(
            "section titles must appear exactly once in this order: "
            + " -> ".join(SECTION_TITLES)
        )
    for title, count in body_counts.items():
        if count == 0:
            errors.append(f"section {title} must contain at least one body segment")

    questions = reply.get("suggested_questions")
    if not isinstance(questions, list) or not 1 <= len(questions) <= 2:
        errors.append("reply.suggested_questions must contain 1 or 2 items")
    elif any(not isinstance(item, str) or not item or len(item) > 200 for item in questions):
        errors.append("each suggested question must be a non-empty string up to 200 characters")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate ProjectF structured-response JSON. Read stdin when FILE is omitted."
    )
    parser.add_argument("file", nargs="?", help="UTF-8 JSON file")
    args = parser.parse_args()

    try:
        payload = load_payload(args.file)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 2

    errors = validate(payload)
    if errors:
        print("INVALID", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
