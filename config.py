import sys
from pathlib import Path

OLLAMA_HOST = "http://localhost:11434"
MODEL_NAME = "qwen3:8b"

# Maximum number of stored messages sent to the model on each turn.
# The full history is persisted on disk; only this window reaches the LLM.
MAX_CONTEXT_MESSAGES = 20

# Where the persistent conversation history is stored.
CHAT_HISTORY_PATH = "data/chat_history.json"

# Bounded agent loop (SPEC-010). The maximum number of tool executions the model
# may drive within a single user turn. It is a host-owned safety limit: the model
# can never read or change it, and a request beyond this count is not executed.
MAX_TOOL_CALLS_PER_TURN = 4

# Agent reliability (SPEC-011). Host-owned time limits and repeated-call policy.
# None of these are ever supplied or changed by the model. Timeouts are
# caller-side deadlines (see reliability.run_with_deadline): a component that
# does not return in time is abandoned, not forcibly terminated.
MODEL_REQUEST_TIMEOUT_SECONDS = 120
TOOL_EXECUTION_TIMEOUT_SECONDS = 30
AGENT_TURN_TIMEOUT_SECONDS = 180
MAX_IDENTICAL_TOOL_CALLS = 2

# Local structured tracing (SPEC-011). Append-only JSONL, local-only, never
# uploaded. Generated traces are git-ignored; only this configuration and the
# tracing code are committed.
TRACE_ENABLED = True
TRACE_PATH = "data/traces/agent.jsonl"
TRACE_PAYLOAD_PREVIEW_CHARS = 1000

# Local Chinook SQLite database (SPEC-008). Resolved relative to this file so the
# paths hold regardless of the current working directory. The seed script is the
# trusted source under version control; the runtime database is generated from it
# by scripts/init_database.py and is not committed.
PROJECT_ROOT = Path(__file__).resolve().parent
CHINOOK_SEED_PATH = PROJECT_ROOT / "data" / "seed" / "Chinook_Sqlite.sql"
SQLITE_DATABASE_PATH = PROJECT_ROOT / "data" / "chinook.sqlite"

# Filesystem-backed skill layer (SPEC-012). Skills are declarative packages that
# live under SKILLS_ROOT and are discovered, validated, and frozen at startup —
# never supplied or mutated by the model. The routing model sees only a compact
# catalog (name + description); the full instruction of the one selected skill is
# loaded lazily for that turn. All bounds below are host-owned and validated at
# startup (skill_runtime.config_validation.validate_skill_config).
SKILLS_ROOT = PROJECT_ROOT / "skills"
# Skill routing has its own component timeout but still counts against the whole
# AGENT_TURN_TIMEOUT_SECONDS budget shared with agent execution.
SKILL_ROUTING_TIMEOUT_SECONDS = 30
# Additional routing requests permitted after the first; 1 => at most two total.
SKILL_ROUTING_REPAIR_ATTEMPTS = 1
MAX_SKILL_ROUTING_RESPONSE_CHARS = 2_000
MAX_SKILL_INSTRUCTION_CHARS = 20_000
MAX_SKILL_SCHEMA_BYTES = 100_000
MAX_SKILLS = 100
MAX_SKILL_DESCRIPTION_CHARS = 200

# Local MCP servers launched by the host over stdio (SPEC-009). Each entry is a
# child process the harness starts; the command, arguments, and environment are
# controlled here by the developer and can never be supplied by the model or by
# chat input. `sys.executable` runs the child in the same virtual environment as
# this app, and the script path is resolved from PROJECT_ROOT so it holds
# regardless of the current working directory.
MCP_SERVERS = {
    "time": {
        "command": sys.executable,
        "args": [str(PROJECT_ROOT / "mcp_servers" / "time_server.py")],
    },
}