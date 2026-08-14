from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from .identity import redact_model_disclosure, sanitize_model_payload
from .schemas import AnalysisResultRecord, Conversation, ConversationCreate, Message, Run, RunEvent


class NotFoundError(KeyError):
    pass


class InMemoryState:
    """MVP 状态仓库；接口刻意保持简单，后续可替换为 PostgreSQL/Redis。"""

    def __init__(self) -> None:
        self.conversations: dict[str, Conversation] = {}
        self.messages: dict[str, list[Message]] = defaultdict(list)
        self.runs: dict[str, Run] = {}
        self.events: dict[str, list[RunEvent]] = defaultdict(list)
        self._event_sequences: dict[str, int] = defaultdict(int)
        self._client_messages: dict[tuple[str, str], tuple[str, str]] = {}
        self._cancelled: set[str] = set()
        self.analysis_results: dict[str, AnalysisResultRecord] = {}
        self._persistence_path: Path | None = None
        self._persistence_lock = RLock()

    def reset(self) -> None:
        persistence_path = self._persistence_path
        self.__init__()
        self._persistence_path = persistence_path

    def configure_persistence(self, path: Path) -> None:
        self._persistence_path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.conversations = {}
            for item in payload.get("conversations", []):
                conversation = Conversation.model_validate(item)
                self._sanitize_conversation(conversation)
                self.conversations[conversation.conversation_id] = conversation
            self.messages = defaultdict(list)
            for conversation_id, items in payload.get("messages", {}).items():
                if conversation_id not in self.conversations:
                    continue
                messages = [Message.model_validate(item) for item in items]
                self.messages[conversation_id] = [self._sanitize_message(message) for message in messages]
            self.analysis_results = {}
            for item in payload.get("analysis_results", []):
                record = AnalysisResultRecord.model_validate(item)
                record.result = sanitize_model_payload(record.result)
                self.analysis_results[record.result_id] = record
        except (OSError, ValueError, TypeError, KeyError):
            # A damaged history file must not prevent the desktop client from starting.
            self.conversations = {}
            self.messages = defaultdict(list)
            self.analysis_results = {}
        else:
            # Migrate existing local history so an old model disclosure cannot
            # reappear after the next desktop restart.
            self._persist()

    def _persist(self) -> None:
        path = self._persistence_path
        if path is None:
            return
        payload = {
            "version": 1,
            "conversations": [
                self._sanitize_conversation(item).model_dump(mode="json")
                for item in self.conversations.values()
            ],
            "messages": {
                conversation_id: [self._sanitize_message(item).model_dump(mode="json") for item in items]
                for conversation_id, items in self.messages.items()
            },
            "analysis_results": [
                {
                    **item.model_dump(mode="json"),
                    "result": sanitize_model_payload(item.result),
                }
                for item in self.analysis_results.values()
            ],
        }
        with self._persistence_lock:
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            temporary.replace(path)

    def create_conversation(self, request: ConversationCreate) -> Conversation:
        conversation_id = f"conv_{uuid.uuid4().hex}"
        pet_name = request.pet.pet_name or "宠物"
        conversation = Conversation(
            conversation_id=conversation_id,
            user_id=request.user_id,
            pet=request.pet,
            title=request.title or f"{pet_name}的宠物管家",
            mode=request.mode,
        )
        self.conversations[conversation_id] = conversation
        self._persist()
        return conversation

    def get_conversation(self, conversation_id: str) -> Conversation:
        try:
            return self._sanitize_conversation(self.conversations[conversation_id])
        except KeyError as exc:
            raise NotFoundError(f"会话不存在: {conversation_id}") from exc

    def append_user_message(
        self,
        conversation_id: str,
        text: str,
        client_message_id: str | None,
        reply_to_result_id: str | None,
    ) -> tuple[Message, Run, bool]:
        conversation = self.get_conversation(conversation_id)
        if reply_to_result_id:
            self.get_analysis_result(reply_to_result_id, conversation_id)
        if client_message_id:
            existing = self._client_messages.get((conversation_id, client_message_id))
            if existing:
                message_id, run_id = existing
                message = next(item for item in self.messages[conversation_id] if item.message_id == message_id)
                return message, self.runs[run_id], True

        message = Message(
            message_id=f"msg_{uuid.uuid4().hex}",
            conversation_id=conversation_id,
            role="user",
            text=text,
            reply_to_result_id=reply_to_result_id,
        )
        run = Run(
            run_id=f"run_{uuid.uuid4().hex}",
            run_type="chat_turn",
            route_key="chat.structured_followup" if reply_to_result_id else "chat.caretaker",
            user_id=conversation.user_id,
            pet_id=conversation.pet.pet_id,
            conversation_id=conversation_id,
            message_id=message.message_id,
        )
        message.run_id = run.run_id
        self.messages[conversation_id].append(message)
        if len(self.messages[conversation_id]) == 1:
            conversation.title = text[:36]
        conversation.summary = message.text[:120]
        conversation.updated_at = datetime.now(timezone.utc)
        self.runs[run.run_id] = run
        if client_message_id:
            self._client_messages[(conversation_id, client_message_id)] = (message.message_id, run.run_id)
        self.append_event(run.run_id, "run.accepted", {"message_id": message.message_id})
        self._persist()
        return message, run, False

    def append_assistant_message(
        self, run_id: str, text: str, structured_reply: dict[str, Any] | None = None
    ) -> Message:
        run = self.get_run(run_id)
        if not run.conversation_id:
            raise ValueError("chat run 缺少 conversation_id")
        message = Message(
            message_id=f"msg_{uuid.uuid4().hex}",
            conversation_id=run.conversation_id,
            role="assistant",
            text=redact_model_disclosure(text),
            run_id=run_id,
            structured_reply=sanitize_model_payload(structured_reply) if structured_reply is not None else None,
        )
        self.messages[run.conversation_id].append(message)
        conversation = self.get_conversation(run.conversation_id)
        conversation.summary = message.text[:120]
        conversation.updated_at = datetime.now(timezone.utc)
        self._persist()
        return message

    def get_message(self, conversation_id: str, message_id: str) -> Message:
        self.get_conversation(conversation_id)
        for message in self.messages[conversation_id]:
            if message.message_id == message_id:
                return self._sanitize_message(message)
        raise NotFoundError(f"消息不存在: {message_id}")

    def register_analysis_result(
        self,
        *,
        task_id: str,
        source_type: str,
        skill_name: str,
        result: dict[str, Any],
        conversation_id: str | None,
    ) -> AnalysisResultRecord:
        if conversation_id:
            self.get_conversation(conversation_id)
        record = AnalysisResultRecord(
            result_id=f"result_{uuid.uuid4().hex}",
            task_id=task_id,
            source_type=source_type,  # type: ignore[arg-type]
            skill_name=skill_name,
            conversation_id=conversation_id,
            result=result,
        )
        self.analysis_results[record.result_id] = record
        self._persist()
        return record

    def get_analysis_result(
        self, result_id: str, conversation_id: str | None = None
    ) -> AnalysisResultRecord:
        try:
            record = self.analysis_results[result_id]
        except KeyError as exc:
            raise NotFoundError(f"检测结果不存在: {result_id}") from exc
        if conversation_id is not None and conversation_id != record.conversation_id:
            raise NotFoundError(f"当前会话无权访问检测结果: {result_id}")
        return record

    def list_analysis_results(
        self, conversation_id: str, limit: int = 3
    ) -> list[AnalysisResultRecord]:
        self.get_conversation(conversation_id)
        records = [
            item
            for item in self.analysis_results.values()
            if item.conversation_id == conversation_id
        ]
        return sorted(records, key=lambda item: item.created_at)[-limit:]

    def list_messages(self, conversation_id: str, limit: int = 50) -> list[Message]:
        self.get_conversation(conversation_id)
        return [self._sanitize_message(message) for message in self.messages[conversation_id][-limit:]]

    def list_conversations(
        self,
        user_id: str,
        pet_id: str | None = None,
        pet_name: str | None = None,
        limit: int = 100,
    ) -> list[Conversation]:
        records = [
            self._sanitize_conversation(conversation)
            for conversation in self.conversations.values()
            if conversation.user_id == user_id
            and (pet_id is None or conversation.pet.pet_id == pet_id)
            and (pet_name is None or conversation.pet.pet_name == pet_name)
            and bool(self.messages.get(conversation.conversation_id))
        ]
        records.sort(key=lambda item: item.updated_at, reverse=True)
        return records[:limit]

    def delete_conversation(self, conversation_id: str) -> None:
        self.get_conversation(conversation_id)
        del self.conversations[conversation_id]
        self.messages.pop(conversation_id, None)
        self.events.pop(conversation_id, None)
        self._event_sequences.pop(conversation_id, None)
        run_ids = [run_id for run_id, run in self.runs.items() if run.conversation_id == conversation_id]
        for run_id in run_ids:
            self.runs.pop(run_id, None)
            self._cancelled.discard(run_id)
        self._client_messages = {
            key: value for key, value in self._client_messages.items() if key[0] != conversation_id
        }
        self.analysis_results = {
            result_id: record
            for result_id, record in self.analysis_results.items()
            if record.conversation_id != conversation_id
        }
        self._persist()

    def refresh_summary(self, conversation_id: str, keep_recent: int = 12) -> str | None:
        conversation = self.get_conversation(conversation_id)
        self._sanitize_conversation(conversation)
        older = self.messages[conversation_id][:-keep_recent]
        if not older:
            return conversation.summary
        role_labels = {"user": "用户", "assistant": "管家"}
        lines = [f"{role_labels[item.role]}：{item.text[:120]}" for item in older[-20:]]
        conversation.summary = " | ".join(lines)[-3000:]
        return conversation.summary

    @staticmethod
    def _sanitize_message(message: Message) -> Message:
        if message.role == "assistant":
            message.text = redact_model_disclosure(message.text)
            if message.structured_reply is not None:
                message.structured_reply = sanitize_model_payload(message.structured_reply)
        return message

    @staticmethod
    def _sanitize_conversation(conversation: Conversation) -> Conversation:
        conversation.title = redact_model_disclosure(conversation.title)
        if conversation.summary:
            conversation.summary = redact_model_disclosure(conversation.summary)
        return conversation

    def get_run(self, run_id: str) -> Run:
        try:
            return self.runs[run_id]
        except KeyError as exc:
            raise NotFoundError(f"Run 不存在: {run_id}") from exc

    def update_run(self, run_id: str, status: str, error: str | None = None) -> Run:
        run = self.get_run(run_id)
        run.status = status  # type: ignore[assignment]
        run.error = error
        run.updated_at = datetime.now(timezone.utc)
        self.append_event(run_id, f"run.{status}", {"error": error} if error else {})
        return run

    def append_event(self, run_id: str, event_type: str, data: dict[str, Any] | None = None) -> RunEvent:
        run = self.get_run(run_id)
        if not run.conversation_id:
            raise ValueError("MVP 事件流仅支持 conversation run")
        conversation_id = run.conversation_id
        self._event_sequences[conversation_id] += 1
        event = RunEvent(
            sequence=self._event_sequences[conversation_id],
            run_id=run_id,
            event_type=event_type,
            data=data or {},
        )
        self.events[conversation_id].append(event)
        return event

    def list_events(self, conversation_id: str, after_sequence: int = 0) -> list[RunEvent]:
        self.get_conversation(conversation_id)
        return [event for event in self.events[conversation_id] if event.sequence > after_sequence]

    def cancel(self, run_id: str) -> Run:
        run = self.get_run(run_id)
        if run.status in {"completed", "failed", "cancelled"}:
            return run
        self._cancelled.add(run_id)
        return self.update_run(run_id, "cancelled")

    def is_cancelled(self, run_id: str) -> bool:
        return run_id in self._cancelled

    def has_active_runs(self, conversation_id: str) -> bool:
        return any(
            run.conversation_id == conversation_id
            and run.status not in {"completed", "failed", "cancelled"}
            for run in self.runs.values()
        )


STATE = InMemoryState()
