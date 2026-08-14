import pytest

from app.validators import (
    OutputValidationError,
    make_gait_inconclusive_result,
    stabilize_repaired_result,
    validate_result,
    validate_result_against_evidence,
)
from app.home_check_workflow import HomeCheckWorkflow


HOME_TITLES = {
    "dental": ["牙结石评估", "牙龈健康", "口腔清洁度", "洁牙建议"],
    "stool": ["颜色分析", "形态评估", "质地特征", "消化健康总评"],
    "gait": ["步伐节律", "四肢协调性", "异常信号"],
    "behavior": ["情绪状态", "压力水平", "异常行为信号", "行为健康总评"],
    "xray": ["影像信息", "骨骼结构", "关节评估", "软组织与体腔", "综合影像解读"],
}
HOME_DEFAULTS = {
    "dental": [("无", "green"), ("正常", "green"), ("清洁", "green"), ("暂不需要", "green")],
    "stool": [("正常", "green"), ("正常", "green"), ("正常", "green"), ("良好", "green")],
    "gait": [("正常", "green"), ("正常", "green"), ("未发现异常", "green")],
    "behavior": [("愉悦", "green"), ("低", "green"), ("未发现异常", "green"), ("良好", "green")],
    "xray": [("", "green"), ("未见异常", "green"), ("未见异常", "green"), ("不适用", "green"), ("未见明显异常", "green")],
}
HOME_ANALYSES = {
    "dental": [
        "图片中牙面与犬齿区域清晰可见，可据牙根沉积特征进行评估。",
        "图片中牙龈颜色和边缘清晰可见，未以演示结果替代医学判断。",
        "图片中牙齿表面与食物残留区域可见，可据此评估清洁度。",
        "综合前三项牙龈与清洁度观察形成洁牙建议，仍需真实模式复核。",
    ],
    "stool": [
        "图片中粪便颜色区域清晰可见，可据棕黄色范围进行评估。",
        "图片中整体形态与是否成形清晰可见，可据此评估。",
        "图片中表面质地及是否存在黏液异物可见，需真实模式复核。",
        "综合颜色、形态和质地三项观察形成消化健康总评。",
    ],
    "gait": [
        "视频全程可用于观察四肢落地节律与步频，演示模式不作医学判断。",
        "视频全程可用于观察四肢抬腿幅度和承重协调性，仍需真实模式复核。",
        "视频全程可用于复核跛行、不对称或拖曳信号，演示模式未发现异常。",
    ],
    "behavior": [
        "视频全程可用于观察耳朵、尾巴、眼神和身体姿态等情绪信号。",
        "视频全程可用于观察舔鼻、哈欠、视线回避和压力变化。",
        "视频全程可用于复核呼吸、皮肤与神经行为，演示模式未发现异常。",
        "综合视频全程的情绪、压力和异常行为字段形成行为健康总评。",
    ],
    "xray": [
        "影像质量与拍摄部位清晰度已进入结构化检查流程。",
        "影像中的骨骼密度与皮质骨边缘将由真实模式进行判断。",
        "影像中的关节间隙与关节面将由真实模式进行判断。",
        "影像为局部素材时软组织与体腔维度可标记为不适用。",
        "从影像来看当前仅完成演示链路，不能替代真实影像解读。",
    ],
}


def _home_result(category: str, media_type: str, dimensions: int, color: str) -> dict:
    assert len(HOME_TITLES[category]) == dimensions
    video = media_type == "video"
    statuses = list(HOME_DEFAULTS[category])
    if color == "blue":
        blue_status = {"dental": "轻微", "stool": "偏浅", "behavior": "平静", "gait": "轻微异常"}[category]
        statuses[0] = (blue_status, "blue")
    return {
        "report_meta": {
            "category": category,
            "category_name": category,
            "test_date": "2026-07-28 12:00",
            "pet": {"pet_name": "警长"},
            "media": {"type": media_type, "url": "local", "thumbnail_url": "local", "duration": 12.4 if video else None},
        },
        "ai_summary": {
            "severity": "轻度",
            "severity_color": "green",
            "summary": "这是用于验证结构化输出质量、字段完整性、状态一致性与具体建议内容的完整测试结果摘要。",
        },
        "dimensions": [
            {
                "title": title,
                "status_label": statuses[index][0],
                "ui_color": statuses[index][1] if color in {"green", "blue"} else color,
                "ai_analysis": HOME_ANALYSES[category][index] + " 本字段同时保留可核验的具体观察位置与状态依据。",
                "suggestion": "建议继续记录相关变化，并在需要时进行复测。",
            }
            for index, title in enumerate(HOME_TITLES[category])
        ],
        "health_suggestions": [
            {
                "ui_label": label,
                "ui_color": "blue",
                "title": "后续建议",
                "content": "根据本次具体观察安排后续护理、日常记录与定期复测计划，并持续对比变化趋势。",
            }
            for label in ("PRIORITY_高", "PRIORITY_中", "PRIORITY_低")
        ],
        "disclaimer": "以上分析由 AI 生成，仅供参考，不构成医疗诊断。如有疑虑，请咨询专业兽医。",
    }


def test_gait_step1_conflict_is_forced_to_warm_orange_result() -> None:
    result = _home_result("gait", "video", 3, "green")
    result["dimensions"][2].update(
        {
            "status_label": "拖曳步态",
            "ui_color": "red",
            "ai_analysis": "视频第2-4秒疑似出现后肢拖曳和骨盆摇摆，但两次整体分析结论不同。",
        }
    )
    result["ai_summary"].update(
        {"severity": "严重", "severity_color": "red", "summary": "视频第2-4秒疑似出现后肢拖曳。"}
    )
    evidence = {
        "consistency": {"status": "conflict"},
        "video_observation": {
            "normality_consistency": "conflict",
            "abnormal_candidates": [
                {"signal": "后肢拖曳", "confidence": "high", "timestamp": 3}
            ],
        },
        "step1_observations": [
            {"abnormal_candidates": []},
            {"abnormal_candidates": [{"signal": "后肢拖曳", "timestamp": 3}]},
        ],
        "step2_evidence": {"keyframe_observations": []},
        "keyframe_observations": [],
    }

    result = HomeCheckWorkflow._apply_gait_uncertainty(result, evidence)

    assert result["ai_summary"]["assessment_status"] == "inconclusive"
    assert result["ai_summary"]["severity"] == "中度"
    assert result["ai_summary"]["severity_color"] == "orange"
    assert "管家" in result["ai_summary"]["summary"]
    assert result["dimensions"][2]["status_label"] == "疑似异常，建议复测"
    assert result["dimensions"][2]["ui_color"] == "orange"
    validate_result("home-health-check-gait", result)
    validate_result_against_evidence("home-health-check-gait", result, evidence)


def test_dense_only_normal_tendency_is_forced_to_inconclusive_orange() -> None:
    result = _home_result("gait", "video", 3, "green")
    evidence = {
        "consistency": {"status": "dense_normal_unconfirmed"},
        "video_observation": {
            "normality_consistency": "dense_normal_unconfirmed",
            "runtime_provider": "ffmpeg_dense_timeline",
            "abnormal_candidates": [],
        },
        "step1_observations": [{"abnormal_candidates": []}],
        "step2_evidence": {"keyframe_observations": []},
        "keyframe_observations": [],
    }

    result = HomeCheckWorkflow._apply_gait_uncertainty(result, evidence)

    assert result["ai_summary"]["assessment_status"] == "inconclusive"
    assert result["ai_summary"]["severity_color"] == "orange"
    assert "原生完整视频分析未能使用" in result["ai_summary"]["summary"]
    assert result["dimensions"][2]["status_label"] == "无法准确判断"
    assert result["dimensions"][2]["ui_color"] == "orange"
    validate_result("home-health-check-gait", result)
    validate_result_against_evidence("home-health-check-gait", result, evidence)


def test_missing_gait_dimension_evidence_returns_partial_result() -> None:
    result = _home_result("gait", "video", 3, "green")

    partial = make_gait_inconclusive_result(
        result,
        "dimensions[0].ai_analysis 缺少该维度的具体观察依据",
    )

    assert partial is not None
    assert partial["ai_summary"]["assessment_status"] == "partial"
    assert partial["ai_summary"]["severity_color"] == "orange"
    assert partial["dimensions"][0]["status_label"] == "无法准确判断"
    assert "无法准确判断" in partial["dimensions"][0]["ai_analysis"]
    validate_result("home-health-check-gait", stabilize_repaired_result(partial))


@pytest.mark.parametrize(
    ("skill_name", "category", "media_type", "dimensions"),
    [
        ("home-health-check-dental", "dental", "image", 4),
        ("home-health-check-stool", "stool", "image", 4),
        ("home-health-check-behavior", "behavior", "video", 4),
    ],
)
def test_blue_dimension_color_is_valid_for_supported_skills(
    skill_name: str, category: str, media_type: str, dimensions: int
) -> None:
    validate_result(skill_name, _home_result(category, media_type, dimensions, "blue"))


def test_blue_dimension_color_remains_invalid_for_gait() -> None:
    with pytest.raises(OutputValidationError, match="ui_color"):
        validate_result("home-health-check-gait", _home_result("gait", "video", 3, "blue"))


def test_every_structured_field_is_limited_to_150_characters() -> None:
    result = _home_result("dental", "image", 4, "green")
    result["ai_summary"]["summary"] = "测" * 151
    with pytest.raises(OutputValidationError, match="150"):
        validate_result("home-health-check-dental", result)


def test_dimension_status_and_color_must_follow_skill_mapping() -> None:
    result = _home_result("dental", "image", 4, "green")
    result["dimensions"][0]["status_label"] = "重度"
    with pytest.raises(OutputValidationError, match="status_label"):
        validate_result("home-health-check-dental", result)


def test_dimension_analysis_must_contain_category_evidence() -> None:
    result = _home_result("stool", "image", 4, "green")
    result["dimensions"][0]["ai_analysis"] = "素材处理流程已经完成并生成较长的通用说明，但仍然没有提供任何真实可核验的画面细节或状态依据。"
    with pytest.raises(OutputValidationError, match="具体观察依据"):
        validate_result("home-health-check-stool", result)


def test_gait_abnormal_dimension_accepts_biomechanical_evidence_without_fixed_lameness_word() -> None:
    result = _home_result("gait", "video", 3, "green")
    result["dimensions"][1].update({"status_label": "需关注", "ui_color": "orange"})
    result["dimensions"][2].update(
        {
            "status_label": "后躯运动模式异常",
            "ui_color": "red",
            "ai_analysis": "视频0.5-6秒后躯持续低位，双侧推进不足、离地净空偏低且骨盆侧摆，前肢代偿牵引重复出现。",
        }
    )
    result["ai_summary"].update({"severity": "严重", "severity_color": "red"})

    validate_result("home-health-check-gait", result)


def test_normal_gait_rejects_unsupported_supplement_recommendations() -> None:
    result = _home_result("gait", "video", 3, "green")
    result["health_suggestions"][1]["content"] = (
        "当前步态未见异常，建议可适当补充关节营养品，并继续观察宠物日常活动表现。"
    )
    with pytest.raises(OutputValidationError, match="正常步态结果不得主动推荐"):
        validate_result("home-health-check-gait", result)


def test_high_confidence_gait_evidence_cannot_be_erased_by_green_final_result() -> None:
    result = _home_result("gait", "video", 3, "green")
    evidence = {
        "video_observation": {
            "abnormal_candidates": [
                {
                    "start_seconds": 6,
                    "end_seconds": 10.84,
                    "timestamp": 8,
                    "signal": "后肢拖曳与后躯摇摆",
                    "confidence": "high",
                }
            ]
        },
        "keyframe_observations": [
            {
                "source": "原视频 8.000 秒关键帧",
                "assessment": "abnormal",
                "observations": [
                    {
                        "finding": "后肢拖曳并足背着地",
                        "visual_evidence": "后足背侧接触地面且后躯向对侧代偿",
                        "confidence": "high",
                    }
                ],
                "limitations": [],
            }
        ],
    }
    with pytest.raises(OutputValidationError, match="高置信步态异常证据"):
        validate_result_against_evidence("home-health-check-gait", result, evidence)

    result["dimensions"][2].update(
        {"status_label": "存在异常信号", "ui_color": "red"}
    )
    result["ai_summary"].update({"severity": "严重", "severity_color": "red"})
    validate_result_against_evidence("home-health-check-gait", result, evidence)


def test_two_high_confidence_gait_keyframes_cannot_be_erased_by_final_result() -> None:
    result = _home_result("gait", "video", 3, "green")
    evidence = {
        "video_observation": {"abnormal_candidates": []},
        "keyframe_observations": [
            {
                "source": "原视频 9.000 秒关键帧",
                "assessment": "abnormal",
                "observations": [
                    {
                        "finding": "左后肢外展且疑似足背着地",
                        "visual_evidence": "后足着地角度异常，骨盆向对侧代偿倾斜",
                        "confidence": "high",
                    }
                ],
                "limitations": ["单帧不能确认持续时长"],
            },
            {
                "source": "原视频 10.000 秒关键帧",
                "assessment": "abnormal",
                "observations": [
                    {
                        "finding": "后肢持续外展且无法正常承重",
                        "visual_evidence": "后肢未回到身体正下方，躯干向对侧代偿",
                        "confidence": "high",
                    }
                ],
                "limitations": [],
            },
        ],
    }
    with pytest.raises(OutputValidationError, match="高置信步态异常证据"):
        validate_result_against_evidence("home-health-check-gait", result, evidence)

    result["dimensions"][2].update(
        {"status_label": "跛行倾向/足背着地", "ui_color": "red"}
    )
    result["ai_summary"].update({"severity": "严重", "severity_color": "red"})
    validate_result_against_evidence("home-health-check-gait", result, evidence)


def test_single_gait_keyframe_cannot_force_or_support_red_result() -> None:
    result = _home_result("gait", "video", 3, "green")
    evidence = {
        "video_observation": {
            "abnormal_candidates": [],
            "normality_review_triggered": True,
            "normality_review_channels": ["native_blind_repeat"],
        },
        "keyframe_observations": [
            {
                "source": "原视频 0.500 秒关键帧",
                "assessment": "abnormal",
                "observations": [
                    {
                        "finding": "后肢屈曲且足部未完全伸展",
                        "visual_evidence": "单帧可见后肢处于摆动相",
                        "confidence": "high",
                    }
                ],
                "limitations": ["单帧无法排除正常摆动相"],
            }
        ],
    }

    validate_result_against_evidence("home-health-check-gait", result, evidence)

    result["dimensions"][2].update(
        {"status_label": "跛行倾向", "ui_color": "red"}
    )
    result["ai_summary"].update({"severity": "严重", "severity_color": "red"})
    with pytest.raises(OutputValidationError, match="低置信单一通道或单帧步态相位不能支持"):
        validate_result_against_evidence("home-health-check-gait", result, evidence)


def test_high_confidence_continuous_video_gait_abnormality_cannot_be_vetoed_by_blurry_frame() -> None:
    result = _home_result("gait", "video", 3, "green")
    evidence = {
        "video_observation": {
            "abnormal_candidates": [
                {
                    "start_seconds": 1,
                    "end_seconds": 8,
                    "timestamp": 4,
                    "signal": "疑似后肢拖曳",
                    "confidence": "high",
                }
            ]
        },
        "keyframe_observations": [
            {
                "source": "原视频 4.000 秒关键帧",
                "assessment": "attention",
                "observations": [
                    {
                        "finding": "后足细节模糊",
                        "visual_evidence": "无法确证足背着地或关节塌陷",
                        "confidence": "low",
                    }
                ],
                "limitations": ["后方视角且运动模糊"],
            }
        ],
    }

    with pytest.raises(OutputValidationError, match="高置信步态异常证据"):
        validate_result_against_evidence("home-health-check-gait", result, evidence)

    result["dimensions"][2].update(
        {"status_label": "拖曳步态", "ui_color": "red"}
    )
    result["ai_summary"].update({"severity": "严重", "severity_color": "red"})
    validate_result_against_evidence("home-health-check-gait", result, evidence)


def test_repeated_medium_confidence_abnormal_cycles_cannot_be_erased() -> None:
    result = _home_result("gait", "video", 3, "green")
    evidence = {
        "video_observation": {
            "abnormal_candidates": [
                {
                    "start_seconds": 1,
                    "end_seconds": 6,
                    "timestamp": 4,
                    "signal": "双侧后肢推进不足",
                    "evidence": "多个周期后肢步幅短且前肢代偿",
                    "confidence": "medium",
                }
            ],
            "cycle_assessments": [
                {"verdict": "abnormal", "evidence": "第一周期双侧蹬地不足"},
                {"verdict": "abnormal", "evidence": "第二周期骨盆低位且前肢拉动"},
                {"verdict": "attention", "evidence": "第三周期受遮挡"},
            ],
        },
        "keyframe_observations": [
            {
                "source": "原视频 4.000 秒关键帧",
                "assessment": "attention",
                "observations": [],
                "limitations": ["运动模糊"],
            }
        ],
    }

    with pytest.raises(OutputValidationError, match="2个步态周期重复异常"):
        validate_result_against_evidence("home-health-check-gait", result, evidence)

    result["dimensions"][2].update({"status_label": "后肢推进不足", "ui_color": "red"})
    result["ai_summary"].update({"severity": "严重", "severity_color": "red"})
    validate_result_against_evidence("home-health-check-gait", result, evidence)


def test_repeated_present_screening_flag_cannot_be_erased() -> None:
    result = _home_result("gait", "video", 3, "green")
    evidence = {
        "video_observation": {
            "abnormal_candidates": [],
            "cycle_assessments": [],
            "screening_flags": [
                {
                    "code": "bilateral_hind_hypometria",
                    "status": "present",
                    "repeated_cycles": 3,
                    "evidence": "三个周期双侧后肢步幅短且离地净空低",
                }
            ],
        },
        "keyframe_observations": [],
    }

    with pytest.raises(OutputValidationError, match="跨周期筛查体征"):
        validate_result_against_evidence("home-health-check-gait", result, evidence)

    result["dimensions"][2].update({"status_label": "双侧后肢步幅缩短", "ui_color": "red"})
    result["ai_summary"].update({"severity": "严重", "severity_color": "red"})
    validate_result_against_evidence("home-health-check-gait", result, evidence)


def test_normal_gait_accepts_equivalent_no_abnormality_evidence_wording() -> None:
    result = _home_result("gait", "video", 3, "green")
    result["dimensions"][2]["ai_analysis"] = (
        "视频0-5秒未见明显异常信号，四肢交替行走自然，关键帧复核也未见异常姿态或固定代偿。"
    )

    validate_result("home-health-check-gait", result)


def test_gait_temporal_evidence_accepts_seconds_abbreviation() -> None:
    result = _home_result("gait", "video", 3, "green")
    result["dimensions"][1].update(
        {
            "status_label": "需关注",
            "ui_color": "orange",
            "ai_analysis": "关键帧1.5s与4.5s显示后肢承重细节受视角影响，四肢整体协调但建议补充侧面视频复核。",
        }
    )
    result["ai_summary"].update({"severity": "中度", "severity_color": "orange"})

    validate_result("home-health-check-gait", result)


def test_gait_red_dimension_requires_severe_summary() -> None:
    result = _home_result("gait", "video", 3, "green")
    result["dimensions"][2].update(
        {"status_label": "跛行倾向/步幅不对称", "ui_color": "red"}
    )
    result["ai_summary"].update({"severity": "中度", "severity_color": "orange"})

    with pytest.raises(OutputValidationError, match="red 维度必须映射为严重"):
        validate_result("home-health-check-gait", result)

    stabilize_repaired_result(result)
    validate_result("home-health-check-gait", result)
    assert result["ai_summary"]["severity"] == "严重"
    assert result["ai_summary"]["severity_color"] == "red"


def test_repaired_report_short_advice_is_stabilized_then_strictly_validated() -> None:
    result = {
        "report_meta": {
            "report_type": "血常规检查",
            "test_date": "2026-07-29",
            "hospital": None,
            "pet": {"pet_name": "警长"},
            "raw_images": ["report.jpg"],
        },
        "ai_summary": {
            "severity": "轻度",
            "severity_color": "Green",
            "summary": "本次报告已识别主要指标，当前未见明显异常变化，仍建议结合近期状态持续观察。",
        },
        "indicators": [
            {
                "full_display": "WBC(白细胞)",
                "kind": "血常规",
                "ui_label": "8.2 ×10^9/L",
                "ref_range": "5.5-19.5",
                "ui_color": "Green",
                "deviation": None,
                "popular_science": "白细胞可辅助反映免疫与炎症状态，需要结合分类计数和临床表现综合理解。",
                "item_advice": "继续观察",
            }
        ],
        "health_suggestions": [
            {"ui_label": "PRIORITY_高", "ui_color": "blue", "title": "核对原始报告", "content": "核对报告中的项目、数值、单位与参考区间，避免识别误差影响理解。"},
            {"ui_label": "PRIORITY_中", "ui_color": "blue", "title": "记录近期状态", "content": "持续记录精神、食欲、饮水、排便和活动情况，并对比后续变化。"},
            {"ui_label": "PRIORITY_低", "ui_color": "blue", "title": "按时安排复查", "content": "保留本次报告并根据兽医建议安排复查，持续比较指标变化趋势。"},
        ],
        "disclaimer": "以上分析由 AI 生成，仅供参考，不构成医疗诊断。如有疑虑，请咨询专业兽医。",
    }

    with pytest.raises(OutputValidationError, match="item_advice 内容过短"):
        validate_result("pet-report-analysis", result)

    stabilize_repaired_result(result)
    validate_result("pet-report-analysis", result)
    assert result["indicators"][0]["item_advice"].startswith("继续观察")
    assert len(result["indicators"][0]["item_advice"]) <= 150


def test_repaired_home_summary_color_is_stabilized_from_severity() -> None:
    result = _home_result("gait", "video", 3, "green")
    result["ai_summary"].update({"severity": "中度", "severity_color": "Yellow"})

    with pytest.raises(OutputValidationError, match="severity 与 severity_color 不一致"):
        validate_result("home-health-check-gait", result)

    stabilize_repaired_result(result)
    validate_result("home-health-check-gait", result)
    assert result["ai_summary"]["severity_color"] == "orange"


def test_repaired_gait_dimension_without_temporal_text_is_stabilized_then_validated() -> None:
    result = _home_result("gait", "video", 3, "green")
    result["dimensions"][1]["ai_analysis"] = "视频中四肢抬腿与协调变化需要结合连续画面判断，当前未见固定节奏停止或拖曳。"

    with pytest.raises(OutputValidationError, match="视频时序"):
        validate_result("home-health-check-gait", result)

    stabilize_repaired_result(result)
    validate_result("home-health-check-gait", result)
    assert "视频全程及关键帧复核" in result["dimensions"][1]["ai_analysis"]
    assert len(result["dimensions"][1]["ai_analysis"]) <= 150


def test_degraded_gait_repair_does_not_claim_native_full_video() -> None:
    result = _home_result("gait", "video", 3, "green")
    result["report_meta"]["analysis_runtime"] = {"analysis_quality": "degraded_dense_storyboard"}
    result["dimensions"][1]["ai_analysis"] = "视频中四肢抬腿与协调变化需要结合连续画面判断，当前未见固定节奏停止或拖曳。"

    stabilize_repaired_result(result)
    validate_result("home-health-check-gait", result)
    analysis = result["dimensions"][1]["ai_analysis"]
    assert "覆盖全时段的顺序帧" in analysis
    assert "视频全程" not in analysis


def test_repaired_gait_status_color_is_derived_from_status_label() -> None:
    result = _home_result("gait", "video", 3, "green")
    result["dimensions"][2].update({"status_label": "存在异常信号", "ui_color": "orange"})
    result["ai_summary"].update({"severity": "中度", "severity_color": "orange"})

    with pytest.raises(OutputValidationError, match="status_label 与 ui_color"):
        validate_result("home-health-check-gait", result)

    stabilize_repaired_result(result)
    validate_result("home-health-check-gait", result)
    assert result["dimensions"][2]["ui_color"] == "red"


def test_repaired_gait_severity_is_reconciled_with_attention_dimensions() -> None:
    result = _home_result("gait", "video", 3, "green")
    result["dimensions"][0].update({"status_label": "轻微异常", "ui_color": "orange"})
    result["ai_summary"].update({"severity": "轻度", "severity_color": "green"})

    with pytest.raises(OutputValidationError, match="轻度结果不能包含"):
        validate_result("home-health-check-gait", result)

    stabilize_repaired_result(result)
    validate_result("home-health-check-gait", result)
    assert result["ai_summary"]["severity"] == "中度"
    assert result["ai_summary"]["severity_color"] == "orange"
