from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from .identity import PRODUCT_IDENTITY


class PetContext(BaseModel):
    pet_id: str | None = None
    pet_name: str | None = None
    avatar: str | None = None
    species: Literal["cat", "dog", "other"] | None = None
    breed: str | None = None
    age_years: float | None = Field(default=None, ge=0, le=100)
    weight_kg: float | None = Field(default=None, gt=0, le=500)
    sex: str | None = None


class TaskRequest(BaseModel):
    skill_name: Literal[
        "pet-report-analysis",
        "home-health-check-dental",
        "home-health-check-stool",
        "home-health-check-gait",
        "home-health-check-behavior",
        "home-health-check-xray",
    ]
    media_path: str
    pet: PetContext = Field(default_factory=PetContext)
    mode: Literal["fake", "real"] = "fake"


class StepTrace(BaseModel):
    step_id: str
    status: Literal["completed", "failed"]
    elapsed_ms: float
    detail: str | None = None


class TaskResponse(BaseModel):
    task_id: str
    result_id: str | None = None
    status: Literal["completed", "failed"]
    skill_name: str
    mode: str
    assistant_name: str = PRODUCT_IDENTITY
    output_file: str | None = None
    result: dict[str, Any] | None = None
    traces: list[StepTrace] = Field(default_factory=list)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ConversationCreate(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    pet: PetContext
    title: str | None = Field(default=None, max_length=100)
    mode: Literal["fake", "real"] = "fake"


class Conversation(BaseModel):
    conversation_id: str
    user_id: str
    pet: PetContext
    title: str
    mode: Literal["fake", "real"]
    summary: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class MessageCreate(BaseModel):
    text: str = Field(min_length=1, max_length=150)
    client_message_id: str | None = Field(default=None, max_length=128)
    reply_to_result_id: str | None = None


class Message(BaseModel):
    message_id: str
    conversation_id: str
    role: Literal["user", "assistant"]
    text: str
    run_id: str | None = None
    reply_to_result_id: str | None = None
    structured_reply: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utc_now)


RunStatus = Literal[
    "accepted",
    "context_building",
    "generating",
    "validating",
    "completed",
    "failed",
    "cancelled",
]


class Run(BaseModel):
    run_id: str
    run_type: Literal["chat_turn", "report_analysis", "home_check"]
    route_key: str
    user_id: str
    pet_id: str | None = None
    conversation_id: str | None = None
    message_id: str | None = None
    status: RunStatus = "accepted"
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class RunEvent(BaseModel):
    sequence: int
    run_id: str
    event_type: str
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class MessageAccepted(BaseModel):
    message_id: str
    run_id: str


class CancelRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=150)


class AnalysisRequest(BaseModel):
    media_path: str
    pet: PetContext
    mode: Literal["fake", "real"] = "fake"
    conversation_id: str | None = None
    client_request_id: str | None = Field(default=None, max_length=128)


class AnalysisResultRecord(BaseModel):
    result_id: str
    task_id: str
    source_type: Literal["report", "home_check"]
    skill_name: str
    conversation_id: str | None = None
    result: dict[str, Any]
    created_at: datetime = Field(default_factory=utc_now)
