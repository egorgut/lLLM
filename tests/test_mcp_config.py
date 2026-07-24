"""Tracker MCP configuration loader tests (SPEC-013).

No network, no `uvx`, and no real Tracker credentials are required -- these
exercise environment-variable validation and secret sanitisation in
isolation. `mcp_integration/config.py` is the only module in the app that
reads Tracker-related `os.environ`, so every test here manipulates that
environment directly via `monkeypatch`.
"""

import pytest

import config
from mcp_integration.client import McpStartupError
from mcp_integration.config import McpServerConfig, env_bool, load_tracker_server_config

FAKE_TOKEN = "fake-token-value-should-never-leak"


def _clear_tracker_env(monkeypatch):
    for key in (
        "TRACKER_MCP_ENABLED",
        "TRACKER_TOKEN",
        "TRACKER_ORG_ID",
        "TRACKER_CLOUD_ORG_ID",
    ):
        monkeypatch.delenv(key, raising=False)


class TestEnvBool:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("SOME_FLAG", raising=False)
        assert env_bool("SOME_FLAG", default=False) is False
        assert env_bool("SOME_FLAG", default=True) is True

    @pytest.mark.parametrize("raw", ["1", "true", "True", "yes", "YES", "on", "ON"])
    def test_truthy_values(self, monkeypatch, raw):
        monkeypatch.setenv("SOME_FLAG", raw)
        assert env_bool("SOME_FLAG", default=False) is True

    @pytest.mark.parametrize("raw", ["0", "false", "False", "no", "NO", "off", "OFF"])
    def test_falsy_values(self, monkeypatch, raw):
        monkeypatch.setenv("SOME_FLAG", raw)
        assert env_bool("SOME_FLAG", default=True) is False

    def test_blank_value_uses_default(self, monkeypatch):
        monkeypatch.setenv("SOME_FLAG", "   ")
        assert env_bool("SOME_FLAG", default=True) is True

    def test_garbage_value_raises(self, monkeypatch):
        monkeypatch.setenv("SOME_FLAG", "maybe")
        with pytest.raises(ValueError):
            env_bool("SOME_FLAG", default=False)


class TestLoadTrackerServerConfigDisabled:
    def test_disabled_by_default_reads_nothing(self, monkeypatch):
        _clear_tracker_env(monkeypatch)
        cfg = load_tracker_server_config()
        assert cfg.enabled is False
        assert cfg.env == {}
        assert cfg.server_id == config.TRACKER_MCP_SERVER_ID

    def test_explicitly_disabled(self, monkeypatch):
        _clear_tracker_env(monkeypatch)
        monkeypatch.setenv("TRACKER_MCP_ENABLED", "false")
        cfg = load_tracker_server_config()
        assert cfg.enabled is False
        assert cfg.env == {}

    def test_disabled_config_still_declares_required_tools(self, monkeypatch):
        # So server_summaries()/the skill-omit decision can inspect
        # cfg.allowed_tools/required_tools uniformly, whether enabled or not.
        _clear_tracker_env(monkeypatch)
        cfg = load_tracker_server_config()
        assert cfg.required_tools == config.TRACKER_MCP_REQUIRED_TOOLS
        assert cfg.allowed_tools == config.TRACKER_MCP_REQUIRED_TOOLS


class TestLoadTrackerServerConfigEnabled:
    def test_enabled_requires_token(self, monkeypatch):
        _clear_tracker_env(monkeypatch)
        monkeypatch.setenv("TRACKER_MCP_ENABLED", "true")
        monkeypatch.setenv("TRACKER_ORG_ID", "org-123")
        with pytest.raises(McpStartupError) as excinfo:
            load_tracker_server_config()
        assert excinfo.value.error_type == "tracker_configuration_missing"
        assert "TRACKER_TOKEN" in str(excinfo.value)

    def test_enabled_requires_one_org_id(self, monkeypatch):
        _clear_tracker_env(monkeypatch)
        monkeypatch.setenv("TRACKER_MCP_ENABLED", "true")
        monkeypatch.setenv("TRACKER_TOKEN", FAKE_TOKEN)
        with pytest.raises(McpStartupError) as excinfo:
            load_tracker_server_config()
        assert excinfo.value.error_type == "tracker_configuration_missing"

    def test_both_org_ids_conflict(self, monkeypatch):
        _clear_tracker_env(monkeypatch)
        monkeypatch.setenv("TRACKER_MCP_ENABLED", "true")
        monkeypatch.setenv("TRACKER_TOKEN", FAKE_TOKEN)
        monkeypatch.setenv("TRACKER_ORG_ID", "org-123")
        monkeypatch.setenv("TRACKER_CLOUD_ORG_ID", "cloud-org-456")
        with pytest.raises(McpStartupError) as excinfo:
            load_tracker_server_config()
        assert excinfo.value.error_type == "tracker_configuration_conflict"

    def test_blank_token_treated_as_absent(self, monkeypatch):
        _clear_tracker_env(monkeypatch)
        monkeypatch.setenv("TRACKER_MCP_ENABLED", "true")
        monkeypatch.setenv("TRACKER_TOKEN", "   ")
        monkeypatch.setenv("TRACKER_ORG_ID", "org-123")
        with pytest.raises(McpStartupError) as excinfo:
            load_tracker_server_config()
        assert excinfo.value.error_type == "tracker_configuration_missing"

    def test_blank_org_id_treated_as_absent(self, monkeypatch):
        _clear_tracker_env(monkeypatch)
        monkeypatch.setenv("TRACKER_MCP_ENABLED", "true")
        monkeypatch.setenv("TRACKER_TOKEN", FAKE_TOKEN)
        monkeypatch.setenv("TRACKER_ORG_ID", "   ")
        with pytest.raises(McpStartupError) as excinfo:
            load_tracker_server_config()
        assert excinfo.value.error_type == "tracker_configuration_missing"

    def test_unpinned_package_is_rejected(self, monkeypatch):
        _clear_tracker_env(monkeypatch)
        monkeypatch.setenv("TRACKER_MCP_ENABLED", "true")
        monkeypatch.setenv("TRACKER_TOKEN", FAKE_TOKEN)
        monkeypatch.setenv("TRACKER_ORG_ID", "org-123")
        monkeypatch.setattr(config, "TRACKER_MCP_PACKAGE", "yandex-tracker-mcp@latest")
        with pytest.raises(McpStartupError) as excinfo:
            load_tracker_server_config()
        assert excinfo.value.error_type == "tracker_configuration_conflict"

    def test_committed_package_constant_is_pinned(self):
        # A code-review-time invariant on the committed literal, not user
        # input -- belt-and-suspenders alongside the runtime check above.
        assert "@latest" not in config.TRACKER_MCP_PACKAGE
        assert "==" in config.TRACKER_MCP_PACKAGE

    def test_enabled_success_builds_expected_env(self, monkeypatch):
        _clear_tracker_env(monkeypatch)
        monkeypatch.setenv("TRACKER_MCP_ENABLED", "true")
        monkeypatch.setenv("TRACKER_TOKEN", FAKE_TOKEN)
        monkeypatch.setenv("TRACKER_ORG_ID", "org-123")
        monkeypatch.setenv("UNRELATED_SECRET", "should-not-appear")
        cfg = load_tracker_server_config()
        assert cfg.enabled is True
        assert cfg.env["TRACKER_TOKEN"] == FAKE_TOKEN
        assert cfg.env["TRANSPORT"] == "stdio"
        assert cfg.env["TRACKER_ORG_ID"] == "org-123"
        assert "TRACKER_CLOUD_ORG_ID" not in cfg.env
        assert "UNRELATED_SECRET" not in cfg.env
        assert cfg.allowed_tools == config.TRACKER_MCP_REQUIRED_TOOLS
        assert cfg.required_tools == config.TRACKER_MCP_REQUIRED_TOOLS
        assert cfg.command == config.TRACKER_MCP_COMMAND
        assert tuple(cfg.args) == tuple(config.TRACKER_MCP_ARGS)

    def test_cloud_org_id_variant(self, monkeypatch):
        _clear_tracker_env(monkeypatch)
        monkeypatch.setenv("TRACKER_MCP_ENABLED", "true")
        monkeypatch.setenv("TRACKER_TOKEN", FAKE_TOKEN)
        monkeypatch.setenv("TRACKER_CLOUD_ORG_ID", "cloud-org-456")
        cfg = load_tracker_server_config()
        assert cfg.env["TRACKER_CLOUD_ORG_ID"] == "cloud-org-456"
        assert "TRACKER_ORG_ID" not in cfg.env

    def test_no_secret_value_in_any_error_message(self, monkeypatch):
        _clear_tracker_env(monkeypatch)
        monkeypatch.setenv("TRACKER_MCP_ENABLED", "true")
        monkeypatch.setenv("TRACKER_TOKEN", FAKE_TOKEN)
        monkeypatch.setenv("TRACKER_ORG_ID", "super-secret-org-id")
        monkeypatch.setenv("TRACKER_CLOUD_ORG_ID", "another-secret-org-id")
        with pytest.raises(McpStartupError) as excinfo:
            load_tracker_server_config()
        message = str(excinfo.value)
        assert FAKE_TOKEN not in message
        assert "super-secret-org-id" not in message
        assert "another-secret-org-id" not in message


class TestMcpServerConfigValidation:
    def test_required_tools_must_be_subset_of_allowed(self):
        with pytest.raises(ValueError):
            McpServerConfig(
                server_id="x",
                command="cmd",
                args=(),
                allowed_tools=frozenset({"a"}),
                required_tools=frozenset({"a", "b"}),
            )

    def test_enabled_server_cannot_have_empty_allowlist(self):
        with pytest.raises(ValueError):
            McpServerConfig(
                server_id="x", command="cmd", args=(), enabled=True, allowed_tools=frozenset()
            )

    def test_disabled_server_may_have_empty_allowlist(self):
        McpServerConfig(
            server_id="x", command="cmd", args=(), enabled=False, allowed_tools=frozenset()
        )

    def test_none_allowlist_preserves_unrestricted_behavior(self):
        # This is what keeps the trusted local `time` server unfiltered.
        McpServerConfig(server_id="time", command="cmd", args=(), allowed_tools=None)
