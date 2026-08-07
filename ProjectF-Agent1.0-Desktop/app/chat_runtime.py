from __future__ import annotations

from .chat_adapter import FakeCaretakerAdapter, OpenAICompatibleChatAdapter
from .config import model_settings_from_environment
from .identity import public_error, redact_model_disclosure, sanitize_model_payload
from .state import InMemoryState
from .structured_response import (
    StructuredResponseService,
    build_structured_context,
    structured_reply_to_text,
)


class ChatRuntime:
    def __init__(self, state: InMemoryState, workspace_root):
        self.state = state
        self.workspace_root = workspace_root

    async def execute(self, run_id: str, user_text: str) -> None:
        run = self.state.get_run(run_id)
        if self.state.is_cancelled(run_id):
            return
        try:
            self.state.update_run(run_id, "context_building")
            conversation = self.state.get_conversation(run.conversation_id or "")
            source_message = self.state.get_message(conversation.conversation_id, run.message_id or "")
            all_history = self.state.list_messages(conversation.conversation_id, limit=50)
            summary = self.state.refresh_summary(conversation.conversation_id)
            history = all_history[-12:]
            context_chars = sum(len(message.text) for message in history)
            self.state.append_event(
                run_id,
                "context.ready",
                {
                    "message_count": len(history),
                    "context_chars": context_chars,
                    "summary_chars": len(summary or ""),
                    "pet_id": conversation.pet.pet_id,
                    "response_mode": "structured" if source_message.reply_to_result_id else "text",
                    "result_id": source_message.reply_to_result_id,
                },
            )
            if self.state.is_cancelled(run_id):
                return

            self.state.update_run(run_id, "generating")
            if source_message.reply_to_result_id:
                record = self.state.get_analysis_result(
                    source_message.reply_to_result_id, conversation.conversation_id
                )
                recent_results = self.state.list_analysis_results(conversation.conversation_id, limit=4)
                context = build_structured_context(
                    record,
                    conversation,
                    user_text,
                    all_history[-10:],
                    recent_results,
                )
                service = StructuredResponseService(
                    self.workspace_root,
                    None
                    if conversation.mode == "fake"
                    else model_settings_from_environment(self.workspace_root),
                )
                payload = sanitize_model_payload(await service.generate(context))
                if self.state.is_cancelled(run_id):
                    return
                self.state.update_run(run_id, "validating")
                for index, segment in enumerate(payload["reply"]["segments"]):
                    self.state.append_event(
                        run_id,
                        "structured.segment",
                        {"index": index, "segment": segment},
                    )
                self.state.append_event(
                    run_id,
                    "structured.suggested_questions",
                    {"questions": payload["reply"]["suggested_questions"]},
                )
                answer = redact_model_disclosure(structured_reply_to_text(payload))[:150]
                assistant = self.state.append_assistant_message(run_id, answer, payload)
                self.state.append_event(
                    run_id,
                    "message.completed",
                    {
                        "message_id": assistant.message_id,
                        "text": assistant.text,
                        "structured_reply": payload,
                        "result_id": record.result_id,
                    },
                )
                self.state.update_run(run_id, "completed")
                return

            adapter = (
                FakeCaretakerAdapter()
                if conversation.mode == "fake"
                else OpenAICompatibleChatAdapter(model_settings_from_environment(self.workspace_root))
            )
            recent_results = self.state.list_analysis_results(conversation.conversation_id, limit=3)
            health_context = self._recent_health_context(recent_results)
            model_summary = "\n".join(part for part in (summary, health_context) if part)
            self.state.append_event(
                run_id,
                "analysis_context.ready",
                {"result_count": len(recent_results), "context_chars": len(health_context)},
            )
            parts: list[str] = []
            async for delta in adapter.stream_reply(conversation, history, user_text, model_summary or None):
                if self.state.is_cancelled(run_id):
                    return
                parts.append(delta)
            hidden_model = getattr(getattr(adapter, "settings", None), "model", "")
            answer = redact_model_disclosure("".join(parts), hidden_model).replace("\r", " ").replace("\n", " ").strip()
            if not answer:
                raise ValueError("模型未返回有效回复")

            self.state.update_run(run_id, "validating")
            if len(answer) > 150:
                raise ValueError("回复超过 150 字符限制")
            for index in range(0, len(answer), 12):
                self.state.append_event(run_id, "token.delta", {"delta": answer[index : index + 12]})
            assistant = self.state.append_assistant_message(run_id, answer)
            self.state.append_event(
                run_id,
                "message.completed",
                {"message_id": assistant.message_id, "text": assistant.text},
            )
            self.state.update_run(run_id, "completed")
        except Exception as exc:
            if not self.state.is_cancelled(run_id):
                self.state.update_run(run_id, "failed", public_error(exc))

    @staticmethod
    def _recent_health_context(records) -> str:
        """把同一会话的已完成检测压缩为普通问答可引用的可信上下文。"""
        if not records:
            return ""
        entries: list[str] = []
        for record in records:
            result = record.result
            meta = result.get("report_meta") or {}
            label = meta.get("report_type") or meta.get("category_name") or record.skill_name
            summary = (result.get("ai_summary") or {}).get("summary")
            if isinstance(summary, str) and summary.strip():
                entries.append(f"{label}：{summary.strip()[:150]}")
        if not entries:
            return ""
        return "近期已完成检测（仅可据此解释，不可扩展为新诊断）：" + "；".join(entries)
