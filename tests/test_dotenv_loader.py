"""`load_dotenv_if_present` tests (SPEC-013 follow-up).

A tiny, dependency-free `.env` loader: fills gaps in the process
environment, never overrides an already-exported variable, and treats a
missing file as a no-op. No live model, MCP, or network is involved.
"""

import os

from app import load_dotenv_if_present


def test_missing_file_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.delenv("SOME_TEST_VAR", raising=False)
    load_dotenv_if_present(tmp_path / "does_not_exist.env")
    assert "SOME_TEST_VAR" not in os.environ


def test_sets_a_missing_variable(tmp_path, monkeypatch):
    monkeypatch.delenv("SOME_TEST_VAR", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("SOME_TEST_VAR=hello\n", encoding="utf-8")
    load_dotenv_if_present(env_file)
    assert os.environ["SOME_TEST_VAR"] == "hello"


def test_does_not_override_an_existing_variable(tmp_path, monkeypatch):
    monkeypatch.setenv("SOME_TEST_VAR", "from-shell")
    env_file = tmp_path / ".env"
    env_file.write_text("SOME_TEST_VAR=from-file\n", encoding="utf-8")
    load_dotenv_if_present(env_file)
    assert os.environ["SOME_TEST_VAR"] == "from-shell"


def test_strips_matching_quotes(tmp_path, monkeypatch):
    monkeypatch.delenv("SOME_TEST_VAR", raising=False)
    monkeypatch.delenv("OTHER_TEST_VAR", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        'SOME_TEST_VAR="quoted value"\nOTHER_TEST_VAR=\'single quoted\'\n',
        encoding="utf-8",
    )
    load_dotenv_if_present(env_file)
    assert os.environ["SOME_TEST_VAR"] == "quoted value"
    assert os.environ["OTHER_TEST_VAR"] == "single quoted"


def test_ignores_comments_blank_lines_and_malformed_lines(tmp_path, monkeypatch):
    monkeypatch.delenv("SOME_TEST_VAR", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# a comment",
                "",
                "not a valid line without an equals sign",
                "SOME_TEST_VAR=value",
                "",
            ]
        ),
        encoding="utf-8",
    )
    load_dotenv_if_present(env_file)
    assert os.environ["SOME_TEST_VAR"] == "value"


def test_blank_key_is_ignored(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("=value_with_no_key\n", encoding="utf-8")
    # Must not raise and must not create an empty-string environ key.
    load_dotenv_if_present(env_file)
    assert "" not in os.environ
