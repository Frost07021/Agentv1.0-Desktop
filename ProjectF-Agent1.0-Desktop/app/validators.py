from __future__ import annotations

import re
from typing import Any


class OutputValidationError(ValueError):
    pass


STANDARD_DISCLAIMER = "以上分析由 AI 生成，仅供参考，不构成医疗诊断。如有疑虑，请咨询专业兽医。"
XRAY_DISCLAIMER = "本解读由AI生成，仅供参考，不构成医疗诊断建议，请结合兽医专业意见综合判断"


def _stabilize_text(value: Any, minimum: int, suffix: str) -> Any:
    """只修复单行和最小长度等机械契约，不改变模型的医学判断。"""
    if not isinstance(value, str):
        return value
    text = re.sub(r"\s+", " ", value).strip()
    if len(text) < minimum:
        separator = "" if not text or text.endswith(("。", "！", "？", ";", "；")) else "。"
        text = f"{text}{separator}{suffix}"
    return text[:150]


def _ensure_video_temporal_evidence(value: Any, *, degraded: bool = False) -> Any:
    """补齐已完成视频全程与关键帧复核的表达，不添加新的医学判断。"""
    if not isinstance(value, str):
        return value
    text = re.sub(r"\s+", " ", value).strip()
    if "全程" in text or "全时段" in text or re.search(r"(?:第)?\d+(?:\.\d+)?(?:\s*[-–至到]\s*\d+(?:\.\d+)?)?秒", text):
        return text[:150]
    suffix = "已结合覆盖全时段的顺序帧及关键帧复核。" if degraded else "已结合视频全程及关键帧复核。"
    separator = "" if not text or text.endswith(("。", "！", "？", ";", "；")) else "。"
    available = 150 - len(separator) - len(suffix)
    return f"{text[:max(0, available)].rstrip('，,;；。')}{separator}{suffix}"


def stabilize_repaired_result(result: dict[str, Any]) -> dict[str, Any]:
    """稳定模型纠偏结果中的文字契约，随后仍必须通过完整 Skill 校验。"""
    summary = result.get("ai_summary")
    if isinstance(summary, dict):
        # 严重程度是模型给出的判断，颜色只是 Skill 规定的展示字段。
        # 修复轮次若保留模型写出的 Yellow/Green，会让内容正确的结果在
        # 最后一层 UI 契约校验中被拒绝；这里以严重程度统一颜色。
        severity = summary.get("severity")
        if severity in {"轻度", "中度", "严重"}:
            is_report = isinstance(result.get("indicators"), list)
            colors = (
                {"轻度": "Green", "中度": "Yellow", "严重": "Red"}
                if is_report
                else {"轻度": "green", "中度": "orange", "严重": "red"}
            )
            summary["severity_color"] = colors[severity]
        summary["summary"] = _stabilize_text(
            summary.get("summary"),
            30,
            "请结合近期精神、食欲、饮水、排便及其他检查结果综合判断。",
        )

    suggestions = result.get("health_suggestions")
    if isinstance(suggestions, list):
        for item in suggestions:
            if not isinstance(item, dict):
                continue
            item["title"] = _stabilize_text(item.get("title"), 4, "行动建议")
            item["content"] = _stabilize_text(
                item.get("content"),
                20,
                "请按建议执行并持续记录变化，异常持续或加重时咨询兽医。",
            )

    indicators = result.get("indicators")
    if isinstance(indicators, list):
        for item in indicators:
            if not isinstance(item, dict):
                continue
            item["full_display"] = _stabilize_text(item.get("full_display"), 3, "检测项目")
            item["ui_label"] = _stabilize_text(item.get("ui_label"), 2, "已识别")
            item["ref_range"] = _stabilize_text(item.get("ref_range"), 1, "未提供")
            item["popular_science"] = _stabilize_text(
                item.get("popular_science"),
                24,
                "该项目需结合其他指标、近期状态及临床表现综合理解。",
            )
            item["item_advice"] = _stabilize_text(
                item.get("item_advice"),
                12,
                "请持续记录相关变化，异常持续或加重时及时咨询兽医。",
            )
            if item.get("deviation") is not None:
                item["deviation"] = _stabilize_text(
                    item.get("deviation"),
                    12,
                    "该变化需结合参考区间与临床表现综合判断。",
                )

    meta = result.get("report_meta")
    category = meta.get("category") if isinstance(meta, dict) else None
    runtime = meta.get("analysis_runtime") if isinstance(meta, dict) else None
    degraded_video = isinstance(runtime, dict) and runtime.get("analysis_quality") == "degraded_dense_storyboard"
    dimensions = result.get("dimensions")
    if category in {"gait", "behavior"} and isinstance(dimensions, list):
        dimension_colors: list[str] = []
        for index, item in enumerate(dimensions):
            if not isinstance(item, dict):
                continue
            expected_color = _expected_status_color(category, index, item.get("status_label"))
            if expected_color is not None:
                # status_label carries the model's finding; ui_color is a deterministic
                # rendering field and must never cause an otherwise valid result to fail.
                item["ui_color"] = expected_color
                dimension_colors.append(expected_color)
            item["ai_analysis"] = _stabilize_text(
                item.get("ai_analysis"),
                28,
                "本结论基于视频全程与关键帧复核，需结合连续动作趋势理解。",
            )
            item["ai_analysis"] = _ensure_video_temporal_evidence(
                item.get("ai_analysis"), degraded=degraded_video
            )
            item["suggestion"] = _stabilize_text(
                item.get("suggestion"),
                12,
                "请持续记录变化，并在异常持续或加重时咨询兽医。",
            )
        if isinstance(summary, dict) and dimension_colors:
            severity = summary.get("severity")
            has_attention = any(color in {"orange", "red"} for color in dimension_colors)
            has_red = "red" in dimension_colors
            if severity == "轻度" and has_attention:
                summary["severity"] = "中度"
            elif severity == "严重" and not has_red:
                summary["severity"] = "中度" if has_attention else "轻度"
            summary["severity_color"] = {
                "轻度": "green",
                "中度": "orange",
                "严重": "red",
            }.get(summary.get("severity"), summary.get("severity_color"))
    return result
HOME_SPECS = {
    "home-health-check-dental": (
        "dental", {"image"}, ["牙结石评估", "牙龈健康", "口腔清洁度", "洁牙建议"], {"green", "blue", "orange", "red"},
    ),
    "home-health-check-stool": (
        "stool", {"image"}, ["颜色分析", "形态评估", "质地特征", "消化健康总评"], {"green", "blue", "orange", "red"},
    ),
    "home-health-check-gait": (
        "gait", {"video"}, ["步伐节律", "四肢协调性", "异常信号"], {"green", "orange", "red"},
    ),
    "home-health-check-behavior": (
        "behavior", {"video"}, ["情绪状态", "压力水平", "异常行为信号", "行为健康总评"], {"green", "blue", "orange", "red"},
    ),
    "home-health-check-xray": (
        "xray", {"image", "pdf"}, ["影像信息", "骨骼结构", "关节评估", "软组织与体腔", "综合影像解读"], {"green", "orange", "red"},
    ),
}
STATUS_COLORS = {
    "dental": [
        {"无": "green", "轻微": "blue", "中度": "orange", "重度": "red"},
        {"正常": "green", "轻微红肿": "blue", "明显炎症": "red"},
        {"清洁": "green", "一般": "orange", "较差": "red"},
        {"暂不需要": "green", "建议近期安排": "orange", "建议尽快就医": "red"},
    ],
    "stool": [
        {"正常": "green", "偏浅": "blue", "偏深": "blue", "异常": "red"},
        {"正常": "green", "偏硬": "orange", "偏软": "orange", "稀烂": "red", "液态": "red"},
        {"正常": "green", "含异物": "orange", "含黏液": "orange", "含血丝": "red"},
        {"良好": "green", "需关注": "orange", "建议就医": "red"},
    ],
    "gait": [
        {"正常": "green", "轻微异常": "orange", "明显异常": "red"},
        {"正常": "green", "需关注": "orange", "异常": "red"},
        None,
    ],
    "behavior": [
        {"愉悦": "green", "放松": "green", "平静": "blue", "警觉": "blue", "焦虑": "orange", "恐惧": "red", "抑郁": "red"},
        {"低": "green", "中": "orange", "高": "red"},
        None,
        {"良好": "green", "需关注": "orange", "建议干预": "red"},
    ],
    "xray": [
        {"": "green"},
        {"未见异常": "green", "需关注": "orange", "异常": "red"},
        {"未见异常": "green", "需关注": "orange", "异常": "red"},
        {"未见异常": "green", "需关注": "orange", "异常": "red", "不适用": "green"},
        {"未见明显异常": "green", "存在需关注项": "orange", "存在异常信号": "red"},
    ],
}


def _expected_status_color(category: str, index: int, status: Any) -> str | None:
    color_rules = STATUS_COLORS.get(category, [])[index]
    if category in {"gait", "behavior"} and index == 2:
        return "green" if status == "未发现异常" else "red"
    if category == "xray" and index in {1, 2, 3} and status not in color_rules:
        text = str(status or "")
        return "green" if "未见异常" in text else "orange" if "需关注" in text else "red" if "异常" in text else None
    return color_rules.get(status) if color_rules is not None else None
EVIDENCE_TOKENS = {
    "dental": [
        ("牙面", "牙根", "犬齿", "臼齿", "黄褐", "沉积"),
        ("牙龈", "粉红", "深红", "红肿", "出血", "边缘"),
        ("牙齿表面", "污渍", "食物残留", "清洁"),
        ("综合", "前三项", "牙龈", "清洁度"),
    ],
    "stool": [
        ("棕", "黄", "黑", "红", "绿", "颜色"),
        ("圆柱", "颗粒", "成形", "偏软", "稀烂", "液态", "形态"),
        ("黏液", "血丝", "异物", "均匀", "质地"),
        ("综合", "颜色", "形态", "质地"),
    ],
    "gait": [
        ("落地", "节律", "步频", "步伐"),
        ("前肢", "后肢", "四肢", "承重", "抬腿", "协调"),
        ("跛行", "僵硬", "不对称", "倾斜", "拖曳", "未发现异常"),
    ],
    "behavior": [
        ("耳朵", "尾巴", "眼神", "姿态", "情绪"),
        ("舔鼻", "哈欠", "视线", "僵硬", "颤抖", "压力"),
        ("呼吸", "皮肤", "神经", "强迫", "排尿", "未发现异常"),
        ("综合", "情绪", "压力", "异常行为"),
    ],
    "xray": [
        ("影像", "拍摄部位", "体位", "质量"),
        ("影像", "骨骼", "皮质骨", "骨密度"),
        ("影像", "关节", "关节面", "关节间隙"),
        ("影像", "软组织", "胸腔", "腹腔", "不适用"),
        ("影像显示", "从影像来看", "AI识别到"),
    ],
}


def _require(mapping: dict[str, Any], keys: list[str], path: str) -> None:
    missing = [key for key in keys if key not in mapping]
    if missing:
        raise OutputValidationError(f"{path} 缺少字段: {', '.join(missing)}")


def _require_text(value: Any, path: str, minimum: int = 1, *, allow_empty: bool = False) -> None:
    if not isinstance(value, str):
        raise OutputValidationError(f"{path} 必须是字符串")
    if not allow_empty and len(value.strip()) < minimum:
        raise OutputValidationError(f"{path} 内容过短")


def _validate_suggestions(result: dict[str, Any]) -> None:
    suggestions = result.get("health_suggestions")
    if not isinstance(suggestions, list) or len(suggestions) != 3:
        raise OutputValidationError("health_suggestions 必须包含 3 条建议")
    expected = ["PRIORITY_高", "PRIORITY_中", "PRIORITY_低"]
    actual = [item.get("ui_label") if isinstance(item, dict) else None for item in suggestions]
    if actual != expected:
        raise OutputValidationError(f"health_suggestions 顺序错误: {actual}")
    for index, item in enumerate(suggestions):
        if not isinstance(item, dict):
            raise OutputValidationError(f"health_suggestions[{index}] 必须是对象")
        _require(item, ["ui_label", "ui_color", "title", "content"], f"health_suggestions[{index}]")
        if item["ui_color"] != "blue":
            raise OutputValidationError(f"health_suggestions[{index}].ui_color 必须为 blue")
        _require_text(item["title"], f"health_suggestions[{index}].title", 4)
        _require_text(item["content"], f"health_suggestions[{index}].content", 20)


def _validate_strings(value: Any, path: str = "result", enforce_text_limit: bool = True) -> None:
    if isinstance(value, str):
        if "\n" in value or "\r" in value:
            raise OutputValidationError(f"{path} 包含换行符")
        if enforce_text_limit and len(value) > 150:
            raise OutputValidationError(f"{path} 超过 150 字符限制")
        return
    if isinstance(value, dict):
        for key, child in value.items():
            _validate_strings(
                child,
                f"{path}.{key}",
                enforce_text_limit and key not in {"url", "thumbnail_url", "raw_images", "avatar"},
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_strings(child, f"{path}[{index}]", enforce_text_limit)


def _validate_summary(result: dict[str, Any], *, report: bool) -> None:
    summary = result["ai_summary"]
    if not isinstance(summary, dict):
        raise OutputValidationError("ai_summary 必须是对象")
    _require(summary, ["severity", "severity_color", "summary"], "ai_summary")
    if summary["severity"] not in {"轻度", "中度", "严重"}:
        raise OutputValidationError("severity 枚举值非法")
    expected_colors = {"轻度": "Green" if report else "green", "中度": "Yellow" if report else "orange", "严重": "Red" if report else "red"}
    if summary["severity_color"] != expected_colors[summary["severity"]]:
        raise OutputValidationError("severity 与 severity_color 不一致")
    _require_text(summary["summary"], "ai_summary.summary", 30)


def _validate_report(result: dict[str, Any]) -> None:
    _require(result, ["indicators"], "result")
    meta = result["report_meta"]
    if not isinstance(meta, dict):
        raise OutputValidationError("report_meta 必须是对象")
    _require(meta, ["report_type", "test_date", "hospital", "pet", "raw_images"], "report_meta")
    _require_text(meta["report_type"], "report_meta.report_type", 2)
    indicators = result["indicators"]
    if not isinstance(indicators, list) or not indicators:
        raise OutputValidationError("indicators 不能为空")
    for index, indicator in enumerate(indicators):
        if not isinstance(indicator, dict):
            raise OutputValidationError(f"indicators[{index}] 必须是对象")
        _require(indicator, ["full_display", "kind", "ui_label", "ref_range", "ui_color", "deviation", "popular_science", "item_advice"], f"indicators[{index}]")
        if indicator["ui_color"] not in {"Green", "Yellow", "Red"}:
            raise OutputValidationError(f"indicators[{index}].ui_color 非法")
        _require_text(indicator["full_display"], f"indicators[{index}].full_display", 3)
        _require_text(indicator["ui_label"], f"indicators[{index}].ui_label", 2)
        _require_text(indicator["ref_range"], f"indicators[{index}].ref_range", 1)
        _require_text(indicator["popular_science"], f"indicators[{index}].popular_science", 24)
        _require_text(indicator["item_advice"], f"indicators[{index}].item_advice", 12)
        if indicator["ui_color"] == "Green" and indicator["deviation"] is not None:
            raise OutputValidationError(f"indicators[{index}] 正常项 deviation 必须为 null")
        if indicator["ui_color"] != "Green":
            _require_text(indicator["deviation"], f"indicators[{index}].deviation", 12)
    if result["disclaimer"] != STANDARD_DISCLAIMER:
        raise OutputValidationError("报告免责声明不符合 Skill 固定文案")


def _validate_home(skill_name: str, result: dict[str, Any]) -> None:
    _require(result, ["dimensions"], "result")
    meta = result["report_meta"]
    if not isinstance(meta, dict):
        raise OutputValidationError("report_meta 必须是对象")
    _require(meta, ["category", "category_name", "test_date", "pet", "media"], "report_meta")
    category, media_types, expected_titles, allowed_colors = HOME_SPECS[skill_name]
    if meta["category"] != category:
        raise OutputValidationError(f"{skill_name} 的 category 不正确")
    media = meta["media"]
    if not isinstance(media, dict) or media.get("type") not in media_types:
        raise OutputValidationError(f"{skill_name} 的 media.type 不正确")
    if category in {"gait", "behavior"} and not isinstance(media.get("duration"), (int, float)):
        raise OutputValidationError(f"{skill_name} 缺少视频 duration")
    dimensions = result["dimensions"]
    if not isinstance(dimensions, list) or len(dimensions) != len(expected_titles):
        raise OutputValidationError(f"{skill_name} 必须包含 {len(expected_titles)} 个维度")
    titles = [item.get("title") if isinstance(item, dict) else None for item in dimensions]
    if titles != expected_titles:
        raise OutputValidationError(f"{skill_name} 维度标题或顺序不符合 Skill: {titles}")
    colors: list[str] = []
    for index, dimension in enumerate(dimensions):
        if not isinstance(dimension, dict):
            raise OutputValidationError(f"dimensions[{index}] 必须是对象")
        _require(dimension, ["title", "status_label", "ui_color", "ai_analysis", "suggestion"], f"dimensions[{index}]")
        color = dimension["ui_color"]
        if color not in allowed_colors:
            raise OutputValidationError(f"dimensions[{index}].ui_color 非法")
        colors.append(color)
        allow_empty = category == "xray" and index == 0
        _require_text(dimension["status_label"], f"dimensions[{index}].status_label", allow_empty=allow_empty)
        _require_text(dimension["ai_analysis"], f"dimensions[{index}].ai_analysis", 28)
        _require_text(dimension["suggestion"], f"dimensions[{index}].suggestion", 12, allow_empty=allow_empty)
        analysis = dimension["ai_analysis"]
        if not any(token in analysis for token in EVIDENCE_TOKENS[category][index]):
            raise OutputValidationError(f"dimensions[{index}].ai_analysis 缺少该维度的具体观察依据")
        status = dimension["status_label"]
        expected_color = _expected_status_color(category, index, status)
        if expected_color is None or color != expected_color:
            raise OutputValidationError(f"dimensions[{index}] 的 status_label 与 ui_color 不符合 Skill")
        if category in {"gait", "behavior"} and not (
            "全程" in dimension["ai_analysis"]
            or "全时段" in dimension["ai_analysis"]
            or re.search(r"(?:第)?\d+(?:\.\d+)?(?:\s*[-–至到]\s*\d+(?:\.\d+)?)?秒", dimension["ai_analysis"])
        ):
            raise OutputValidationError(f"dimensions[{index}].ai_analysis 缺少视频时序或观察依据")
    severity = result["ai_summary"]["severity"]
    if severity == "轻度" and any(color in {"orange", "red"} for color in colors):
        raise OutputValidationError("轻度结果不能包含 orange/red 维度")
    if severity == "严重" and "red" not in colors:
        raise OutputValidationError("严重结果必须包含 red 维度")
    if category == "xray":
        if result["disclaimer"] != XRAY_DISCLAIMER:
            raise OutputValidationError("X 光免责声明不符合 Skill 固定文案")
        if "red" in colors:
            all_text = " ".join(str(item.get("ai_analysis", "")) for item in dimensions)
            if "这类情况需要兽医结合临床检查才能做出准确判断" not in all_text:
                raise OutputValidationError("X 光红色结果缺少临床检查边界说明")
    elif result["disclaimer"] != STANDARD_DISCLAIMER:
        raise OutputValidationError("居家检测免责声明不符合 Skill 固定文案")


def validate_result(skill_name: str, result: dict[str, Any]) -> None:
    if not isinstance(result, dict):
        raise OutputValidationError("结果必须是对象")
    _require(result, ["report_meta", "ai_summary", "health_suggestions", "disclaimer"], "result")
    _validate_summary(result, report=skill_name == "pet-report-analysis")
    _validate_suggestions(result)
    if skill_name == "pet-report-analysis":
        _validate_report(result)
    elif skill_name in HOME_SPECS:
        _validate_home(skill_name, result)
    else:
        raise OutputValidationError(f"没有输出校验器: {skill_name}")
    _validate_strings(result)
