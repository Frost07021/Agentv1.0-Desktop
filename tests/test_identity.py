import json

from app.identity import PRODUCT_IDENTITY, identity_system_prompt, sanitize_model_payload
from app.schemas import Conversation, Message, PetContext, TaskResponse
from app.state import InMemoryState


def test_identity_policy_is_attached_to_model_prompts() -> None:
    prompt = identity_system_prompt("执行宠物健康分析")
    assert PRODUCT_IDENTITY in prompt
    assert "底层模型名称" in prompt


def test_model_metadata_is_removed_and_names_are_redacted() -> None:
    result = sanitize_model_payload(
        {
            "reply": "我是 qwen3.7-plus 模型，接口是 https://model.example/v1",
            "model_name": "qwen3.7-plus",
            "provider": "example-provider",
        },
        "qwen3.7-plus",
    )
    assert "model_name" not in result
    assert "provider" not in result
    assert "qwen3.7-plus" not in result["reply"]
    assert PRODUCT_IDENTITY in result["reply"]


def test_task_response_exposes_only_product_identity() -> None:
    response = TaskResponse(task_id="task", status="completed", skill_name="demo", mode="real")
    assert response.assistant_name == PRODUCT_IDENTITY
    assert "model_name" not in response.model_dump()


def test_persisted_history_is_migrated_and_sanitized(tmp_path) -> None:
    conversation = Conversation(
        conversation_id="conv_old",
        user_id="user",
        pet=PetContext(pet_name="拉拉", species="cat", age_years=8),
        title="你是哪个模型",
        mode="fake",
        summary="当前判断：我是通义千问驱动的宠物管家",
    )
    message = Message(
        message_id="msg_old",
        conversation_id=conversation.conversation_id,
        role="assistant",
        text="当前判断：我是通义千问驱动的宠物管家；已知依据：系统设定",
    )
    history_path = tmp_path / "desktop-history.json"
    history_path.write_text(
        json.dumps(
            {
                "version": 1,
                "conversations": [conversation.model_dump(mode="json")],
                "messages": {conversation.conversation_id: [message.model_dump(mode="json")]},
                "analysis_results": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    state = InMemoryState()
    state.configure_persistence(history_path)

    loaded_message = state.list_messages(conversation.conversation_id)[0]
    loaded_conversation = state.get_conversation(conversation.conversation_id)
    persisted = history_path.read_text(encoding="utf-8")

    assert "通义千问" not in loaded_message.text
    assert "通义千问" not in (loaded_conversation.summary or "")
    assert PRODUCT_IDENTITY in loaded_message.text
    assert "通义千问" not in persisted
