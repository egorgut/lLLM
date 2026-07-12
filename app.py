from config import CHAT_HISTORY_PATH
from conversation import Conversation
from llm import chat_with_model
from storage import JsonConversationStore


def main() -> None:
    store = JsonConversationStore(CHAT_HISTORY_PATH)
    conversation = Conversation(messages=store.load())

    print("Local AI chat")
    print("Enter /reset to clear the conversation, /bye to exit.\n")

    while True:
        user_message = input("You: ").strip()

        if not user_message:
            continue

        if user_message.lower() == "/bye":
            print("Chat finished.")
            break

        if user_message.lower() == "/reset":
            conversation.reset()
            store.save(conversation.stored_messages)
            print("Conversation cleared.\n")
            continue

        conversation.add_user_message(user_message)

        try:
            assistant_message = chat_with_model(conversation.messages_for_model)
        except Exception as error:
            print(f"\nApplication error: {error}\n")

            # Удаляем сообщение пользователя, поскольку ответа на него не получили.
            conversation.remove_last_message()
            continue

        conversation.add_assistant_message(assistant_message)
        store.save(conversation.stored_messages)

        print(f"\nQwen: {assistant_message}\n")


if __name__ == "__main__":
    main()
