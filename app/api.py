from __future__ import annotations

import asyncio
import json
import shutil
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .chat_runtime import ChatRuntime
from .config import default_workspace_root
from .harness import AnalysisExecutionError, Harness
from .identity import PRODUCT_IDENTITY, public_error
from .route_registry import ROUTES
from .schemas import (
    AnalysisRequest,
    CancelRequest,
    Conversation,
    ConversationCreate,
    Message,
    MessageAccepted,
    MessageCreate,
    PetContext,
    Run,
    TaskRequest,
    TaskResponse,
)
from .state import NotFoundError, STATE


WORKSPACE_ROOT = default_workspace_root()
HARNESS = Harness(WORKSPACE_ROOT)
CHAT_RUNTIME = ChatRuntime(STATE, WORKSPACE_ROOT)
UPLOAD_DIR = WORKSPACE_ROOT / "runtime" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR = WORKSPACE_ROOT / "static"

if "pytest" not in sys.modules:
    STATE.configure_persistence(WORKSPACE_ROOT / "runtime" / "desktop-history.json")

app = FastAPI(
    title=PRODUCT_IDENTITY,
    version="0.4.0",
    description="统一承载宠物管家流式对话、报告检测与五类居家检测的完整客户端服务。",
)

BUILD_VERSION = "desktop-1.4.34"


@app.middleware("http")
async def disable_desktop_asset_cache(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/assets/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


def _not_found(exc: Exception) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc).strip("'"))


def _analysis_failure_detail(exc: AnalysisExecutionError) -> dict[str, object]:
    """Translate every terminal analysis failure into an actionable UI reason."""
    cause = exc.error
    code = str(getattr(cause, "code", "") or type(cause).__name__)
    stage = exc.traces[-1].step_id if exc.traces else "initialization"
    message = str(cause)
    if code in {"MEDIA_NOT_FOUND", "MEDIA_UNREADABLE", "MEDIA_FRAME_EXTRACTION_ERROR"}:
        user_message = "本次素材未能被完整读取，因此无法形成可靠分析结果。"
        suggestion = "请确认文件仍然存在并重新导出为清晰的MP4、MOV、JPG或PDF后再试。"
    elif code in {"STEP1_DEADLINE_EXCEEDED", "VIDEO_PATHS_FAILED", "NATIVE_VIDEO_UNAVAILABLE"}:
        user_message = "视频已成功读取，但整体分析在限定时间内未完成，因此本次无法形成可靠结论。"
        suggestion = "建议稍后直接重试；如再次失败，请补充5–10秒、侧面低机位且四肢完整入镜的视频。"
    elif "OutputValidationError" in type(cause).__name__ or "校验" in message:
        user_message = "素材识别已经完成，但现有证据不足以生成一份前后一致、可可靠展示的结果。"
        suggestion = "请补充更清晰、无遮挡且包含完整目标部位或连续动作的素材后重新检测。"
    elif any(token in message for token in ("JSON", "JSON 对象", "响应必须")):
        user_message = "素材分析已经完成，但结果整理未能生成可安全展示的结构化内容。"
        suggestion = "请保留当前素材并重新检测；若再次出现，可稍后重试以避开服务波动。"
    else:
        user_message = "本次分析在完成前中断，系统未向你展示不可靠或不完整的结论。"
        suggestion = "当前素材已保留，请稍后重新检测；如持续失败，请更换更清晰、完整的素材。"
    return {
        "stage": stage,
        "reason_code": code,
        "user_message": user_message,
        "can_retry": True,
        "suggestion": suggestion,
    }


@app.get("/health/live")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
async def client_home() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health/ready")
async def readiness() -> JSONResponse:
    loaded_skills = HARNESS.registry.list()
    missing_skills = HARNESS.registry.missing_required()
    payload: dict[str, object] = {
        "status": "not_ready" if missing_skills else "ready",
        "build_version": BUILD_VERSION,
        "skills": len(loaded_skills),
        "routes": len(ROUTES.list()),
        "state_backend": "local_json",
        "assistant_name": PRODUCT_IDENTITY,
        "analysis_pipeline": "enabled",
        "home_check_pipeline": "skill_aligned_v1",
        "structured_followup": "skill_aligned_v1",
    }
    if missing_skills:
        payload["missing_skills"] = missing_skills
    return JSONResponse(content=payload, status_code=503 if missing_skills else 200)


@app.get("/v1/skills")
async def skills() -> list[dict[str, str]]:
    return HARNESS.registry.list()


@app.get("/v1/routes")
async def routes() -> list[dict[str, object]]:
    return ROUTES.list()


@app.post("/v1/conversations", response_model=Conversation, status_code=201)
async def create_conversation(request: ConversationCreate) -> Conversation:
    return STATE.create_conversation(request)


@app.get("/v1/conversations", response_model=list[Conversation])
async def list_conversations(
    user_id: str = Query(default="local_user", min_length=1, max_length=128),
    pet_id: str | None = Query(default=None, max_length=128),
    pet_name: str | None = Query(default=None, min_length=1, max_length=30),
    limit: int = Query(default=100, ge=1, le=200),
) -> list[Conversation]:
    return STATE.list_conversations(user_id=user_id, pet_id=pet_id, pet_name=pet_name, limit=limit)


@app.get("/v1/conversations/{conversation_id}", response_model=Conversation)
async def get_conversation(conversation_id: str) -> Conversation:
    try:
        return STATE.get_conversation(conversation_id)
    except NotFoundError as exc:
        raise _not_found(exc) from exc


@app.delete("/v1/conversations/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: str) -> None:
    try:
        STATE.delete_conversation(conversation_id)
    except NotFoundError as exc:
        raise _not_found(exc) from exc


@app.get("/v1/conversations/{conversation_id}/messages", response_model=list[Message])
async def list_messages(
    conversation_id: str,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[Message]:
    try:
        return STATE.list_messages(conversation_id, limit=limit)
    except NotFoundError as exc:
        raise _not_found(exc) from exc


@app.post(
    "/v1/conversations/{conversation_id}/messages",
    response_model=MessageAccepted,
    status_code=202,
)
async def create_message(
    conversation_id: str,
    request: MessageCreate,
    background_tasks: BackgroundTasks,
) -> MessageAccepted:
    try:
        message, run, duplicate = STATE.append_user_message(
            conversation_id,
            request.text,
            request.client_message_id,
            request.reply_to_result_id,
        )
    except NotFoundError as exc:
        raise _not_found(exc) from exc
    if not duplicate:
        background_tasks.add_task(CHAT_RUNTIME.execute, run.run_id, request.text)
    return MessageAccepted(message_id=message.message_id, run_id=run.run_id)


def _sse(event_type: str, sequence: int, payload: dict[str, object]) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    return f"id: {sequence}\nevent: {event_type}\ndata: {data}\n\n"


async def _conversation_event_stream(conversation_id: str, after_sequence: int) -> AsyncIterator[str]:
    sequence = after_sequence
    idle_ticks = 0
    while idle_ticks < 3000:
        events = STATE.list_events(conversation_id, sequence)
        if events:
            idle_ticks = 0
            for event in events:
                sequence = event.sequence
                payload = event.model_dump(mode="json")
                yield _sse(event.event_type, event.sequence, payload)
            latest_run = STATE.get_run(events[-1].run_id)
            if latest_run.status in {"completed", "failed", "cancelled"}:
                return
        else:
            if not STATE.has_active_runs(conversation_id):
                return
            idle_ticks += 1
            if idle_ticks % 150 == 0:
                yield ": heartbeat\n\n"
        await asyncio.sleep(0.02)


@app.get("/v1/conversations/{conversation_id}/events")
async def conversation_events(
    conversation_id: str,
    after_sequence: int = Query(default=0, ge=0),
) -> StreamingResponse:
    try:
        STATE.get_conversation(conversation_id)
    except NotFoundError as exc:
        raise _not_found(exc) from exc
    return StreamingResponse(
        _conversation_event_stream(conversation_id, after_sequence),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/v1/runs/{run_id}", response_model=Run)
async def get_run(run_id: str) -> Run:
    try:
        return STATE.get_run(run_id)
    except NotFoundError as exc:
        raise _not_found(exc) from exc


@app.post("/v1/runs/{run_id}/cancel", response_model=Run)
async def cancel_run(run_id: str, request: CancelRequest) -> Run:
    del request
    try:
        return STATE.cancel(run_id)
    except NotFoundError as exc:
        raise _not_found(exc) from exc


async def _execute_analysis(route_key: str, request: AnalysisRequest) -> TaskResponse:
    try:
        route = ROUTES.get(route_key)
        response = await HARNESS.execute(
            TaskRequest(
                skill_name=route.skill_name,
                media_path=request.media_path,
                pet=request.pet,
                mode=request.mode,
            )
        )
        if response.result is None:
            raise ValueError("检测任务完成但缺少结果")
        record = STATE.register_analysis_result(
            task_id=response.task_id,
            source_type="report" if route_key.startswith("report.") else "home_check",
            skill_name=route.skill_name,
            result=response.result,
            conversation_id=request.conversation_id,
        )
        response.result_id = record.result_id
        return response
    except AnalysisExecutionError as exc:
        raise HTTPException(status_code=422, detail=_analysis_failure_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=422, detail=public_error(exc)) from exc


@app.post("/v1/analysis/report/{category}/tasks", response_model=TaskResponse)
async def create_report_task(category: str, request: AnalysisRequest) -> TaskResponse:
    return await _execute_analysis(f"report.{category}", request)


@app.post("/v1/analysis/home-check/{category}/tasks", response_model=TaskResponse)
async def create_home_check_task(category: str, request: AnalysisRequest) -> TaskResponse:
    return await _execute_analysis(f"home_check.{category}", request)


async def _save_upload(file: UploadFile, upload_quality: str = "original") -> Path:
    if upload_quality not in {"smart", "original"}:
        raise HTTPException(status_code=422, detail="上传质量参数无效，请重新选择智能压缩或原始质量")
    filename = Path(file.filename or "upload.bin").name
    is_video = (file.content_type or "").startswith("video/") or Path(filename).suffix.lower() in {".mp4", ".mov"}
    smart_video = upload_quality == "smart" and is_video
    source_limit = (100 if smart_video else 50) * 1024 * 1024
    if file.size is not None and file.size > source_limit:
        detail = "智能压缩的视频源文件不能超过 100MB" if smart_video else "原始质量文件不能超过 50MB"
        raise HTTPException(status_code=413, detail=detail)
    target = UPLOAD_DIR / f"{uuid.uuid4().hex}_{filename}"
    written = 0
    with target.open("wb") as output:
        while chunk := await file.read(1024 * 1024):
            written += len(chunk)
            if written > source_limit:
                target.unlink(missing_ok=True)
                detail = "智能压缩的视频源文件不能超过 100MB" if smart_video else "原始质量文件不能超过 50MB"
                raise HTTPException(status_code=413, detail=detail)
            output.write(chunk)
    if smart_video and written > 50 * 1024 * 1024:
        try:
            compressed = await asyncio.to_thread(HARNESS.media_processor.compress_video_to_limit, target)
        except Exception as exc:
            # 保留源文件供重试，并明确区分压缩失败与后续分析失败。
            error_code = getattr(exc, "code", "MEDIA_COMPRESSION_FAILED")
            raise HTTPException(status_code=422, detail=public_error(exc)) from exc
        target.unlink(missing_ok=True)
        return compressed
    return target


async def _execute_uploaded_analysis(
    route_key: str,
    file: UploadFile,
    mode: str,
    pet_id: str | None,
    pet_name: str | None,
    species: str | None,
    breed: str | None,
    conversation_id: str | None,
    upload_quality: str,
) -> TaskResponse:
    target = await _save_upload(file, upload_quality)
    return await _execute_analysis(
        route_key,
        AnalysisRequest(
            media_path=str(target),
            pet=PetContext(
                pet_id=pet_id,
                pet_name=pet_name,
                species=species,
                breed=breed,
            ),
            mode=mode,
            conversation_id=conversation_id,
        ),
    )


@app.post("/v1/analysis/report/{category}/upload", response_model=TaskResponse)
async def upload_report_analysis(
    category: str,
    file: UploadFile = File(...),
    mode: str = Form("fake"),
    pet_id: str | None = Form(None),
    pet_name: str | None = Form(None),
    species: str | None = Form(None),
    breed: str | None = Form(None),
    conversation_id: str | None = Form(None),
    upload_quality: str = Form("original"),
) -> TaskResponse:
    return await _execute_uploaded_analysis(
        f"report.{category}", file, mode, pet_id, pet_name, species, breed, conversation_id, upload_quality
    )


@app.post("/v1/analysis/home-check/{category}/upload", response_model=TaskResponse)
async def upload_home_check_analysis(
    category: str,
    file: UploadFile = File(...),
    mode: str = Form("fake"),
    pet_id: str | None = Form(None),
    pet_name: str | None = Form(None),
    species: str | None = Form(None),
    breed: str | None = Form(None),
    conversation_id: str | None = Form(None),
    upload_quality: str = Form("original"),
) -> TaskResponse:
    return await _execute_uploaded_analysis(
        f"home_check.{category}", file, mode, pet_id, pet_name, species, breed, conversation_id, upload_quality
    )


# 兼容原 Demo 接口，便于现有调用方渐进迁移。
@app.post("/v1/agent/tasks", response_model=TaskResponse, deprecated=True)
async def create_legacy_task(request: TaskRequest) -> TaskResponse:
    try:
        return await HARNESS.execute(request)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=public_error(exc)) from exc


@app.post("/v1/agent/tasks/upload", response_model=TaskResponse, deprecated=True)
async def upload_legacy_task(
    skill_name: str = Form(...),
    mode: str = Form("fake"),
    pet_id: str | None = Form(None),
    pet_name: str | None = Form(None),
    file: UploadFile = File(...),
) -> TaskResponse:
    filename = Path(file.filename or "upload.bin").name
    target = UPLOAD_DIR / f"{uuid.uuid4().hex}_{filename}"
    with target.open("wb") as output:
        shutil.copyfileobj(file.file, output)
    request = TaskRequest(
        skill_name=skill_name,
        media_path=str(target),
        pet=PetContext(pet_id=pet_id, pet_name=pet_name),
        mode=mode,
    )
    return await create_legacy_task(request)


app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")
