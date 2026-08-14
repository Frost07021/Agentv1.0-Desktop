import subprocess
import shutil
from pathlib import Path

import imageio_ffmpeg
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.api import HARNESS, _analysis_failure_detail, app
from app.harness import AnalysisExecutionError
from app.schemas import StepTrace
from app.state import STATE


@pytest.fixture(autouse=True)
def reset_state() -> None:
    STATE.reset()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_health_readiness_and_fixed_routes(client: TestClient) -> None:
    assert client.get("/health/live").json() == {"status": "ok"}
    ready = client.get("/health/ready").json()
    assert ready["status"] == "ready"
    assert ready["build_version"] == "desktop-1.4.34"
    assert ready["state_backend"] == "local_json"
    assert ready["skills"] == 7
    routes = client.get("/v1/routes").json()
    assert {route["route_key"] for route in routes} == {
        "report.general",
        "home_check.dental",
        "home_check.stool",
        "home_check.gait",
        "home_check.behavior",
        "home_check.xray",
    }


def test_readiness_rejects_an_incomplete_skill_bundle(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(HARNESS.registry, "missing_required", lambda: ["home-health-check-gait"])

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["missing_skills"] == ["home-health-check-gait"]


def test_serves_mature_client(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "流式养宠对话" in response.text
    assert "报告检测" in response.text
    assert "居家检测" in response.text
    assert 'maxlength="150"' in response.text
    assert 'accept="image/jpeg,image/png,image/webp,application/pdf,.pdf"' in response.text
    assert "PDF 将自动逐页识别" in response.text
    assert "desktop-workbench" in response.text
    assert response.text.index("两大健康功能") < response.text.index("今日主动关怀")
    assert "历史记录" in response.text
    assert 'id="history-list"' in response.text
    assert 'id="add-pet-profile"' in response.text
    assert "FURA Desktop 1.4.19" in response.text
    assert response.headers["cache-control"].startswith("no-store")


def test_terminal_analysis_failure_has_actionable_reason(tmp_path: Path) -> None:
    cause = RuntimeError("STEP1_DEADLINE_EXCEEDED: 视频理解总耗时超过 330 秒")
    cause.code = "STEP1_DEADLINE_EXCEEDED"  # type: ignore[attr-defined]
    failure = AnalysisExecutionError(
        cause,
        [
            StepTrace(
                step_id="home.step1.video_understanding",
                status="failed",
                elapsed_ms=330000,
                error="deadline",
            )
        ],
        tmp_path / "failure.json",
    )

    detail = _analysis_failure_detail(failure)

    assert detail["stage"] == "home.step1.video_understanding"
    assert detail["reason_code"] == "STEP1_DEADLINE_EXCEEDED"
    assert "限定时间内未完成" in str(detail["user_message"])
    assert detail["can_retry"] is True
    assert "重新" in str(detail["suggestion"]) or "重试" in str(detail["suggestion"])


def test_history_lists_non_empty_conversations_and_supports_delete(client: TestClient) -> None:
    empty_id = client.post(
        "/v1/conversations",
        json={"user_id": "local_user", "pet": {"pet_id": "captain", "pet_name": "警长"}, "mode": "fake"},
    ).json()["conversation_id"]
    assert client.get("/v1/conversations?user_id=local_user").json() == []
    response = client.post(
        f"/v1/conversations/{empty_id}/messages",
        json={"text": "警长今天不太爱吃饭", "client_message_id": "history-1"},
    )
    assert response.status_code == 202
    history = client.get("/v1/conversations?user_id=local_user").json()
    assert len(history) == 1
    assert history[0]["conversation_id"] == empty_id
    assert history[0]["title"] == "警长今天不太爱吃饭"
    assert client.get(f"/v1/conversations/{empty_id}/messages").json()
    assert client.delete(f"/v1/conversations/{empty_id}").status_code == 204
    assert client.get("/v1/conversations?user_id=local_user").json() == []


def test_result_page_renders_status_labels_and_skill_colors(client: TestClient) -> None:
    script = client.get("/assets/app.js")
    styles = client.get("/assets/styles.css")
    assert script.status_code == 200
    assert styles.status_code == 200
    assert "function reportStatus(item)" in script.text
    assert 'status: reportStatus(item)' in script.text
    assert 'class="result-status tone-${item.tone}"' in script.text
    assert "临界·需关注" in script.text
    assert "本次使用全时段顺序帧分析" in script.text
    for tone in ("green", "blue", "orange", "red"):
        assert f".tone-{tone}" in styles.text
        assert f".result-item.result-{tone}" in styles.text or tone == "green"


def test_analysis_failure_uses_visible_retry_modal(client: TestClient) -> None:
    script = client.get("/assets/app.js").text
    assert "function renderAnalysisError(error, scope, name)" in script
    assert "function openResultModal()" in script
    assert 'id="retry-analysis"' in script
    assert "当前素材已保留" in script


def test_desktop_history_and_upload_exception_flows_are_bundled(client: TestClient) -> None:
    script = client.get("/assets/app.js").text
    assert "function loadHistory()" in script
    assert "function openHistoryDetail(conversationId)" in script
    assert "function requestDeleteConversation(conversationId)" in script
    assert "function restoreConversationMessages()" in script
    assert 'body.append("upload_quality", state.uploadQuality[scope])' in script
    assert "原视频超出上传上限" in script
    assert "超过 100MB 素材上限" in script
    assert "视频时长超过 15 秒" in script
    assert "最多支持 10 页" in script
    assert "MEDIA_COMPRESSION_FAILED" in script
    assert "samePetSnapshot" in script
    assert "petIdentityChanged" in script
    assert "history-pet-filters" in script
    assert "petProfileKey" in script
    assert "renderPetProfileManager" in script
    assert "requestDeletePetProfile" in script
    assert "data-delete-pet-profile" in script
    assert "至少需要保留一份宠物档案" in script


def test_conversation_history_keeps_pet_snapshots_immutable(client: TestClient) -> None:
    first = client.post(
        "/v1/conversations",
        json={"user_id": "history_pet_user", "pet": {"pet_id": "pet-a", "pet_name": "警长", "species": "cat"}, "mode": "fake"},
    ).json()["conversation_id"]
    client.post(f"/v1/conversations/{first}/messages", json={"text": "警长今天状态不错"})

    second = client.post(
        "/v1/conversations",
        json={"user_id": "history_pet_user", "pet": {"pet_id": "pet-b", "pet_name": "拉拉", "species": "dog"}, "mode": "fake"},
    ).json()["conversation_id"]
    client.post(f"/v1/conversations/{second}/messages", json={"text": "拉拉今天吃饭正常"})

    records = client.get("/v1/conversations?user_id=history_pet_user").json()
    snapshots = {item["conversation_id"]: item["pet"] for item in records}
    assert snapshots[first]["pet_name"] == "警长"
    assert snapshots[first]["pet_id"] == "pet-a"
    assert snapshots[second]["pet_name"] == "拉拉"
    assert snapshots[second]["pet_id"] == "pet-b"


def test_current_pet_history_filter_handles_legacy_shared_pet_ids(client: TestClient) -> None:
    for name, text in (("警长", "警长的历史消息"), ("拉拉", "拉拉的历史消息")):
        conversation_id = client.post(
            "/v1/conversations",
            json={"user_id": "legacy_history_user", "pet": {"pet_id": "pet_demo_001", "pet_name": name}, "mode": "fake"},
        ).json()["conversation_id"]
        client.post(f"/v1/conversations/{conversation_id}/messages", json={"text": text})

    records = client.get(
        "/v1/conversations?user_id=legacy_history_user&pet_id=pet_demo_001&pet_name=警长"
    ).json()
    assert len(records) == 1
    assert records[0]["pet"]["pet_name"] == "警长"


def test_chat_input_rejects_more_than_150_characters(client: TestClient) -> None:
    conversation_id = client.post(
        "/v1/conversations",
        json={"user_id": "user_limit", "pet": {"pet_name": "警长"}, "mode": "fake"},
    ).json()["conversation_id"]
    response = client.post(
        f"/v1/conversations/{conversation_id}/messages",
        json={"text": "字" * 151},
    )
    assert response.status_code == 422


def test_caretaker_multi_turn_chat_and_sse(client: TestClient) -> None:
    conversation = client.post(
        "/v1/conversations",
        json={
            "user_id": "user_1",
            "pet": {"pet_id": "pet_1", "pet_name": "警长", "species": "cat", "breed": "英短"},
            "mode": "fake",
        },
    ).json()
    conversation_id = conversation["conversation_id"]

    accepted = client.post(
        f"/v1/conversations/{conversation_id}/messages",
        json={"text": "最近走路一瘸一拐，怎么办？", "client_message_id": "mobile_001"},
    )
    assert accepted.status_code == 202
    run_id = accepted.json()["run_id"]
    assert client.get(f"/v1/runs/{run_id}").json()["status"] == "completed"

    messages = client.get(f"/v1/conversations/{conversation_id}/messages").json()
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert "步态分析" in messages[-1]["text"]

    event_body = client.get(f"/v1/conversations/{conversation_id}/events").text
    assert "event: token.delta" in event_body
    assert "event: message.completed" in event_body
    assert "event: run.completed" in event_body

    duplicate = client.post(
        f"/v1/conversations/{conversation_id}/messages",
        json={"text": "这条内容不会重复执行", "client_message_id": "mobile_001"},
    ).json()
    assert duplicate["run_id"] == run_id
    assert len(client.get(f"/v1/conversations/{conversation_id}/messages").json()) == 2


def test_emergency_language_is_prioritized(client: TestClient) -> None:
    conversation_id = client.post(
        "/v1/conversations",
        json={"user_id": "user_2", "pet": {"pet_name": "团子"}, "mode": "fake"},
    ).json()["conversation_id"]
    client.post(
        f"/v1/conversations/{conversation_id}/messages",
        json={"text": "团子突然抽搐而且站不起来"},
    )
    answer = client.get(f"/v1/conversations/{conversation_id}/messages").json()[-1]["text"]
    assert "立即" in answer
    assert "急诊" in answer
    assert "不要自行喂药" in answer


def test_report_analysis_uses_product_route(client: TestClient, tmp_path: Path) -> None:
    image_path = tmp_path / "report.jpg"
    Image.new("RGB", (640, 480), "white").save(image_path)
    response = client.post(
        "/v1/analysis/report/general/tasks",
        json={
            "media_path": str(image_path),
            "pet": {"pet_id": "pet_1", "pet_name": "警长"},
            "mode": "fake",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["skill_name"] == "pet-report-analysis"
    assert body["result"]["report_meta"]["pet"]["pet_name"] == "警长"


def test_report_pdf_upload_is_converted_and_analyzed_page_by_page(client: TestClient, tmp_path: Path) -> None:
    pdf_path = tmp_path / "multi-page-report.pdf"
    pages = [Image.new("RGB", (640, 480), color) for color in ("white", "lightgray")]
    pages[0].save(pdf_path, "PDF", save_all=True, append_images=pages[1:], resolution=100)

    with pdf_path.open("rb") as report:
        response = client.post(
            "/v1/analysis/report/general/upload",
            data={"mode": "fake", "pet_name": "警长", "species": "cat"},
            files={"file": (pdf_path.name, report, "application/pdf")},
        )

    assert response.status_code == 200
    raw_images = response.json()["result"]["report_meta"]["raw_images"]
    assert len(raw_images) == 2
    assert all(Path(path).suffix == ".jpg" for path in raw_images)


def test_unknown_category_is_rejected(client: TestClient, tmp_path: Path) -> None:
    response = client.post(
        "/v1/analysis/home-check/nutrition/tasks",
        json={"media_path": str(tmp_path / "nutrition.jpg"), "pet": {"pet_name": "警长"}},
    )
    assert response.status_code == 422
    assert "不支持的固定业务路由" in response.json()["detail"]


@pytest.mark.parametrize(
    ("category", "expected_name", "dimension_count"),
    [
        ("dental", "牙科评估", 4),
        ("stool", "便便分析", 4),
        ("xray", "X光片解读", 5),
    ],
)
def test_image_home_checks_are_embedded_in_product_routes(
    client: TestClient,
    tmp_path: Path,
    category: str,
    expected_name: str,
    dimension_count: int,
) -> None:
    image_path = tmp_path / f"{category}.jpg"
    Image.new("RGB", (640, 480), "white").save(image_path)
    with image_path.open("rb") as image:
        response = client.post(
            f"/v1/analysis/home-check/{category}/upload",
            data={"mode": "fake", "pet_name": "警长", "species": "cat"},
            files={"file": (image_path.name, image, "image/jpeg")},
        )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["report_meta"]["category_name"] == expected_name
    assert len(result["dimensions"]) == dimension_count


@pytest.mark.parametrize(
    ("category", "expected_name", "dimension_count"),
    [("gait", "步态分析", 3), ("behavior", "行为评估", 4)],
)
def test_video_home_checks_are_embedded_in_product_routes(
    client: TestClient,
    tmp_path: Path,
    category: str,
    expected_name: str,
    dimension_count: int,
) -> None:
    video_path = tmp_path / f"{category}.mp4"
    subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=white:s=320x240:r=10:d=1.2",
            "-pix_fmt",
            "yuv420p",
            str(video_path),
        ],
        capture_output=True,
        check=True,
    )
    with video_path.open("rb") as video:
        response = client.post(
            f"/v1/analysis/home-check/{category}/upload",
            data={"mode": "fake", "pet_name": "警长", "species": "cat"},
            files={"file": (video_path.name, video, "video/mp4")},
        )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["report_meta"]["category_name"] == expected_name
    assert len(result["dimensions"]) == dimension_count


def test_analysis_result_followup_uses_structured_response_skill(client: TestClient, tmp_path: Path) -> None:
    conversation_id = client.post(
        "/v1/conversations",
        json={
            "user_id": "user_structured",
            "pet": {"pet_id": "pet_1", "pet_name": "警长", "species": "cat"},
            "mode": "fake",
        },
    ).json()["conversation_id"]
    image_path = tmp_path / "dental.jpg"
    Image.new("RGB", (320, 240), "white").save(image_path)
    analysis = client.post(
        "/v1/analysis/home-check/dental/tasks",
        json={
            "media_path": str(image_path),
            "pet": {"pet_id": "pet_1", "pet_name": "警长", "species": "cat"},
            "mode": "fake",
            "conversation_id": conversation_id,
        },
    )
    assert analysis.status_code == 200
    result_id = analysis.json()["result_id"]
    assert result_id.startswith("result_")

    accepted = client.post(
        f"/v1/conversations/{conversation_id}/messages",
        json={"text": "牙结石需要马上处理吗？", "reply_to_result_id": result_id},
    )
    assert accepted.status_code == 202
    run = client.get(f"/v1/runs/{accepted.json()['run_id']}").json()
    assert run["status"] == "completed"
    assert run["route_key"] == "chat.structured_followup"

    assistant = client.get(f"/v1/conversations/{conversation_id}/messages").json()[-1]
    structured = assistant["structured_reply"]
    assert structured["reply"]["emotion"] == "warm_empathy"
    assert len(structured["reply"]["segments"]) >= 10
    assert "牙结石评估" in assistant["text"]

    event_body = client.get(f"/v1/conversations/{conversation_id}/events").text
    assert "event: structured.segment" in event_body
    assert "event: structured.suggested_questions" in event_body
    assert '"structured_reply"' in event_body


def test_structured_followup_rejects_cross_conversation_result(client: TestClient, tmp_path: Path) -> None:
    first = client.post(
        "/v1/conversations",
        json={"user_id": "owner_1", "pet": {"pet_name": "警长"}, "mode": "fake"},
    ).json()["conversation_id"]
    second = client.post(
        "/v1/conversations",
        json={"user_id": "owner_2", "pet": {"pet_name": "团子"}, "mode": "fake"},
    ).json()["conversation_id"]
    image_path = tmp_path / "stool.jpg"
    Image.new("RGB", (200, 200), "white").save(image_path)
    result_id = client.post(
        "/v1/analysis/home-check/stool/tasks",
        json={"media_path": str(image_path), "pet": {"pet_name": "警长"}, "mode": "fake", "conversation_id": first},
    ).json()["result_id"]
    response = client.post(
        f"/v1/conversations/{second}/messages",
        json={"text": "帮我解释", "reply_to_result_id": result_id},
    )
    assert response.status_code == 404
    assert "无权访问检测结果" in response.json()["detail"]


def test_regular_chat_builds_context_from_recent_analysis(client: TestClient, tmp_path: Path) -> None:
    conversation_id = client.post(
        "/v1/conversations",
        json={"user_id": "context_owner", "pet": {"pet_name": "警长"}, "mode": "fake"},
    ).json()["conversation_id"]
    image_path = tmp_path / "dental-context.jpg"
    Image.new("RGB", (240, 240), "white").save(image_path)
    analysis = client.post(
        "/v1/analysis/home-check/dental/tasks",
        json={"media_path": str(image_path), "pet": {"pet_name": "警长"}, "mode": "fake", "conversation_id": conversation_id},
    )
    assert analysis.status_code == 200
    accepted = client.post(
        f"/v1/conversations/{conversation_id}/messages",
        json={"text": "结合刚才的结果，今天先做什么？"},
    )
    assert accepted.status_code == 202
    context_events = [
        event
        for event in STATE.events[conversation_id]
        if event.run_id == accepted.json()["run_id"] and event.event_type == "analysis_context.ready"
    ]
    assert len(context_events) == 1
    assert context_events[0].data["result_count"] == 1
