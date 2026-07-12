from llm import chat_with_model
from prompts import SYSTEM_PROMPT


def main() -> None:
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT,
        }
    ]

    print("Local AI chat")
    print("Enter /bye to exit.\n")

    while True:
        user_message = input("You: ").strip()

        if not user_message:
            continue

        if user_message.lower() == "/bye":
            print("Chat finished.")
            break

        messages.append(
            {
                "role": "user",
                "content": user_message,
            }
        )

        try:
            assistant_message = chat_with_model(messages)
        except Exception as error:
            print(f"\nApplication error: {error}\n")

            # Удаляем сообщение пользователя, поскольку ответа на него не получили.
            messages.pop()
            continue

        messages.append(
            {
                "role": "assistant",
                "content": assistant_message,
            }
        )

        print(f"\nQwen: {assistant_message}\n")


if __name__ == "__main__":
    main()