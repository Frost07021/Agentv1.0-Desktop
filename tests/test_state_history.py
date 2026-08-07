from pathlib import Path

from app.schemas import ConversationCreate, PetContext
from app.state import InMemoryState


def test_history_persists_non_empty_conversations_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "desktop-history.json"
    first = InMemoryState()
    first.configure_persistence(path)
    conversation = first.create_conversation(
        ConversationCreate(
            user_id="local_user",
            pet=PetContext(pet_id="captain", pet_name="警长"),
            mode="fake",
        )
    )
    assert first.list_conversations("local_user") == []
    first.append_user_message(conversation.conversation_id, "警长今天食欲下降", "once", None)

    restored = InMemoryState()
    restored.configure_persistence(path)
    history = restored.list_conversations("local_user", pet_id="captain")
    assert len(history) == 1
    assert history[0].title == "警长今天食欲下降"
    assert restored.list_messages(conversation.conversation_id)[0].text == "警长今天食欲下降"

    restored.delete_conversation(conversation.conversation_id)
    reloaded = InMemoryState()
    reloaded.configure_persistence(path)
    assert reloaded.list_conversations("local_user") == []
