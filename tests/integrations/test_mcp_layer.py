"""Unit tests for backend.integrations.mcp_layer.

No real network calls are made anywhere in this suite — the built-in
stub plugins don't make any, and the custom test plugins used here are
purely in-memory.
"""
from typing import Any, Dict

import pytest
import yaml

from backend.integrations.mcp_layer import (
    ConfluencePlugin,
    DiagnosticsPlugin,
    MCPPlugin,
    MCPPluginNotFoundError,
    MCPRegistry,
    SlackPlugin,
)


class EchoPlugin(MCPPlugin):
    """A simple test plugin that echoes the query back."""

    name = "echo"

    def execute(self, query: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "ok", "echo": query}


class ExplodingPlugin(MCPPlugin):
    """A test plugin whose execute() always raises."""

    name = "exploding"

    def execute(self, query: Dict[str, Any]) -> Dict[str, Any]:
        raise RuntimeError("boom")


class NotAPlugin:
    """Deliberately does NOT subclass MCPPlugin."""

    def execute(self, query):
        return {}


@pytest.fixture
def registry() -> MCPRegistry:
    return MCPRegistry()


def test_register_and_execute_custom_plugin(registry: MCPRegistry):
    registry.register_plugin("echo", EchoPlugin)
    result = registry.execute_plugin("echo", {"q": "hello"})
    assert result == {"status": "ok", "echo": {"q": "hello"}}


def test_register_non_mcp_plugin_subclass_raises(registry: MCPRegistry):
    with pytest.raises(ValueError):
        registry.register_plugin("bad", NotAPlugin)


def test_register_plugin_overwrite_logs_warning(registry: MCPRegistry, caplog):
    registry.register_plugin("echo", EchoPlugin)
    with caplog.at_level("WARNING"):
        registry.register_plugin("echo", DiagnosticsPlugin)
    assert any("already registered" in msg for msg in caplog.messages)
    assert registry.get_available_plugins() == ["echo"]
    result = registry.execute_plugin("echo", {})
    assert result["status"] == "stub"


def test_execute_unregistered_plugin_raises(registry: MCPRegistry):
    with pytest.raises(MCPPluginNotFoundError):
        registry.execute_plugin("does-not-exist", {})


def test_execute_plugin_that_raises_returns_structured_error(registry: MCPRegistry):
    registry.register_plugin("exploding", ExplodingPlugin)
    result = registry.execute_plugin("exploding", {})
    assert result["error"] == "boom"
    assert result["plugin"] == "exploding"


def test_get_available_plugins_reflects_registrations(registry: MCPRegistry):
    assert registry.get_available_plugins() == []
    registry.register_plugin("echo", EchoPlugin)
    registry.register_plugin("exploding", ExplodingPlugin)
    assert sorted(registry.get_available_plugins()) == ["echo", "exploding"]


def test_load_plugins_from_config_only_registers_enabled(registry: MCPRegistry, tmp_path):
    config = {
        "mcp_plugins": {
            "knowledge_base": {"enabled": True, "type": "confluent"},  # typo spelling
            "confluence": {"enabled": False, "type": "confluence"},
            "diagnostics": {"enabled": True, "type": "custom"},
            "slack": {"enabled": False, "type": "slack"},
        }
    }
    config_path = tmp_path / "settings.yaml"
    config_path.write_text(yaml.safe_dump(config))

    registry.load_plugins_from_config(str(config_path))

    available = registry.get_available_plugins()
    assert sorted(available) == ["diagnostics", "knowledge_base"]

    # "confluent" typo maps to the same ConfluencePlugin stub.
    result = registry.execute_plugin("knowledge_base", {"question": "how do I reset my password?"})
    assert result["status"] == "stub"
    assert "Confluence" in result["message"]

    result = registry.execute_plugin("diagnostics", {})
    assert result["status"] == "stub"


def test_load_plugins_from_config_correct_confluence_spelling_also_works(registry: MCPRegistry, tmp_path):
    config = {
        "mcp_plugins": {
            "confluence": {"enabled": True, "type": "confluence"},
        }
    }
    config_path = tmp_path / "settings.yaml"
    config_path.write_text(yaml.safe_dump(config))

    registry.load_plugins_from_config(str(config_path))

    assert registry.get_available_plugins() == ["confluence"]
    result = registry.execute_plugin("confluence", {})
    assert result["status"] == "stub"


def test_load_plugins_from_config_missing_file_does_not_raise(registry: MCPRegistry):
    registry.load_plugins_from_config("/nonexistent/path/settings.yaml")
    assert registry.get_available_plugins() == []


def test_load_plugins_from_config_all_disabled_registers_nothing(registry: MCPRegistry, tmp_path):
    config = {
        "mcp_plugins": {
            "knowledge_base": {"enabled": False, "type": "confluent"},
            "diagnostics": {"enabled": False, "type": "custom"},
            "slack": {"enabled": False, "type": "slack"},
            "confluence": {"enabled": False, "type": "confluence"},
        }
    }
    config_path = tmp_path / "settings.yaml"
    config_path.write_text(yaml.safe_dump(config))

    registry.load_plugins_from_config(str(config_path))
    assert registry.get_available_plugins() == []


def test_load_plugins_from_config_against_real_repo_config():
    """The repo's actual config/settings.yaml ships with everything
    disabled by default — load_plugins_from_config should register no
    plugins out of the box, which is the expected scaffold state."""
    registry = MCPRegistry()
    registry.load_plugins_from_config("config/settings.yaml")
    assert registry.get_available_plugins() == []


def test_slack_plugin_stub_is_honest():
    result = SlackPlugin().execute({"channel": "#support"})
    assert result["status"] == "stub"
    assert result["results"] == []


def test_confluence_plugin_stub_is_honest():
    result = ConfluencePlugin().execute({"q": "vpn setup"})
    assert result["status"] == "stub"
    assert result["results"] == []


def test_diagnostics_plugin_stub_is_honest():
    result = DiagnosticsPlugin().execute({"check": "disk_space"})
    assert result["status"] == "stub"
    assert result["results"] == []
