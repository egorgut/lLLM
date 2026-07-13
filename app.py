from config import CHAT_HISTORY_PATH
from conversation import Conversation
from llm import stream_chat_with_model
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

        print("\nQwen: ", end="", flush=True)
        response_parts: list[str] = []

        try:
            for chunk in stream_chat_with_model(conversation.messages_for_model):
                print(chunk, end="", flush=True)
                response_parts.append(chunk)
        except KeyboardInterrupt:
            print("\nGeneration interrupted.\n")

            # Ответ не получен целиком — откатываем сообщение пользователя.
            conversation.remove_last_message()
            continue
        except Exception as error:
            print(f"\nApplication error: {error}\n")

            # Удаляем сообщение пользователя, поскольку ответа на него не получили.
            conversation.remove_last_message()
            continue

        assistant_message = "".join(response_parts)

        if not assistant_message:
            print("\nApplication error: Model returned an empty response.\n")

            # Пустой ответ считаем неуспешным — откатываем сообщение пользователя.
            conversation.remove_last_message()
            continue

        conversation.add_assistant_message(assistant_message)
        store.save(conversation.stored_messages)

        print("\n")


if __name__ == "__main__":
    main()
