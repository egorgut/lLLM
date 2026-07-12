OLLAMA_HOST = "http://localhost:11434"
MODEL_NAME = "qwen3:8b"

# Maximum number of stored messages sent to the model on each turn.
# The full history is persisted on disk; only this window reaches the LLM.
MAX_CONTEXT_MESSAGES = 20

# Where the persistent conversation history is stored.
CHAT_HISTORY_PATH = "data/chat_history.json"