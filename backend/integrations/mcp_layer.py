"""MCP/Plugin layer (Phase 2 starter implementation)

Provides a lightweight plugin registry and execution layer so the ticket
workflow can call out to external tools (knowledge bases, diagnostics,
notification systems, customer-specific systems) through a single,
uniform interface, per docs/PHASE1_SUMMARY.md Task 2.

This module is intentionally scaffolding: the built-in plugins registered
by `load_plugins_from_config` are honest STUBS that return a clearly
marked placeholder result rather than pretending to call a real external
API. Real integrations (Confluence, Slack, etc.) should replace these
stubs' `execute()` bodies, or subclass `MCPPlugin` and register the real
implementation under the same name via `register_plugin`.
"""
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Type

import yaml

logger = logging.getLogger(__name__)


class MCPPluginNotFoundError(KeyError):
    """Raised when execute_plugin() is called with an unregistered plugin name."""


class MCPPlugin(ABC):
    """Base class every MCP plugin implements.

    Subclasses should set a class-level (or instance-level) `name`
    attribute identifying the plugin, and implement `execute()`.
    """

    name: str = "unnamed_plugin"

    @abstractmethod
    def execute(self, query: Dict[str, Any]) -> Dict[str, Any]:
        """Run the plugin against a query, return a structured result."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Built-in stub plugins
#
# These are honest scaffolds for Phase 2: they do NOT make any network
# calls. Each returns a clearly labeled placeholder result so callers
# (and future contributors) can tell at a glance that the integration
# still needs real implementation.
# ---------------------------------------------------------------------------


class ConfluencePlugin(MCPPlugin):
    """Stub for a Confluence/knowledge-base search plugin.

    NOTE: config/settings.yaml currently has an inconsistency where
    `mcp_plugins.knowledge_base.type` is spelled "confluent" (typo, missing
    the second "e"), while `mcp_plugins.confluence.type` is spelled
    correctly as "confluence". Both spellings are mapped to this class in
    the type dispatch below so neither entry silently fails to register.
    A maintainer should clean up the YAML typo in a follow-up.
    """

    name = "confluence"

    def execute(self, query: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "status": "stub",
            "message": (
                "Confluence integration not yet implemented — configure "
                "CONFLUENCE_BASE_URL and CONFLUENCE_API_TOKEN to enable"
            ),
            "results": [],
        }


class DiagnosticsPlugin(MCPPlugin):
    """Stub for a diagnostics/custom-tooling plugin (type: "custom")."""

    name = "diagnostics"

    def execute(self, query: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "status": "stub",
            "message": (
                "Diagnostics integration not yet implemented — wire this "
                "plugin up to your internal diagnostics/monitoring tooling "
                "to enable"
            ),
            "results": [],
        }


class SlackPlugin(MCPPlugin):
    """Stub for a Slack notification plugin."""

    name = "slack"

    def execute(self, query: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "status": "stub",
            "message": (
                "Slack integration not yet implemented — configure "
                "SLACK_BOT_TOKEN and SLACK_CHANNEL_ID to enable"
            ),
            "results": [],
        }


# Maps the `type` field found in config/settings.yaml's `mcp_plugins`
# section to the built-in stub class that should be registered for it.
# Both "confluent" (typo present in the current YAML) and "confluence"
# (correct spelling) intentionally resolve to the same class.
_TYPE_TO_PLUGIN_CLASS: Dict[str, Type[MCPPlugin]] = {
    "confluent": ConfluencePlugin,
    "confluence": ConfluencePlugin,
    "custom": DiagnosticsPlugin,
    "slack": SlackPlugin,
}


class MCPRegistry:
    """Registry + execution layer for MCP plugins.

    Plugin classes are stored (not instances); `execute_plugin` instantiates
    the class on demand. Plugins are expected to be cheap to construct.
    """

    def __init__(self) -> None:
        self._plugins: Dict[str, Type[MCPPlugin]] = {}

    def register_plugin(self, plugin_name: str, plugin_class: Type[MCPPlugin]) -> None:
        """Register a plugin class under `plugin_name`.

        Stores the class itself (not an instance). Raises ValueError if
        `plugin_class` doesn't subclass MCPPlugin. Re-registering an
        existing name logs a warning and overwrites the previous entry.
        """
        if not (isinstance(plugin_class, type) and issubclass(plugin_class, MCPPlugin)):
            raise ValueError(
                f"Cannot register '{plugin_name}': {plugin_class!r} is not a subclass of MCPPlugin"
            )

        if plugin_name in self._plugins:
            logger.warning(
                "Plugin '%s' is already registered (%s); overwriting with %s",
                plugin_name,
                self._plugins[plugin_name].__name__,
                plugin_class.__name__,
            )

        self._plugins[plugin_name] = plugin_class
        logger.debug("Registered plugin '%s' -> %s", plugin_name, plugin_class.__name__)

    def load_plugins_from_config(self, config_path: str = "config/settings.yaml") -> None:
        """Load and register plugins described in the `mcp_plugins` section
        of a settings YAML file.

        Only entries with `enabled: true` are registered; disabled entries
        (the current default for all built-in entries) are skipped. Each
        entry's `type` field is looked up in `_TYPE_TO_PLUGIN_CLASS` to pick
        the built-in stub plugin class to register.
        """
        try:
            with open(config_path, "r") as f:
                config = yaml.safe_load(f) or {}
        except FileNotFoundError:
            logger.error("MCP config file not found: %s", config_path)
            return
        except yaml.YAMLError as e:
            logger.error("Failed to parse MCP config file %s: %s", config_path, e)
            return

        mcp_plugins_config: Dict[str, Any] = config.get("mcp_plugins") or {}
        if not mcp_plugins_config:
            logger.warning("No 'mcp_plugins' section found in %s", config_path)
            return

        for plugin_name, entry in mcp_plugins_config.items():
            if not isinstance(entry, dict):
                logger.warning("Skipping malformed mcp_plugins entry '%s'", plugin_name)
                continue

            if not entry.get("enabled", False):
                logger.debug("Plugin '%s' is disabled in config; skipping", plugin_name)
                continue

            plugin_type = entry.get("type")
            plugin_class = _TYPE_TO_PLUGIN_CLASS.get(plugin_type)
            if plugin_class is None:
                logger.error(
                    "Plugin '%s' has unknown type '%s'; no matching plugin class, skipping",
                    plugin_name,
                    plugin_type,
                )
                continue

            self.register_plugin(plugin_name, plugin_class)
            logger.info("Loaded plugin '%s' (type=%s) from config", plugin_name, plugin_type)

    def execute_plugin(self, plugin_name: str, query: Dict[str, Any]) -> Dict[str, Any]:
        """Instantiate and run the named plugin against `query`.

        Raises MCPPluginNotFoundError if no plugin is registered under
        `plugin_name`. If the plugin's own `execute()` raises, the
        exception is logged at ERROR level and a structured error dict is
        returned instead of propagating the exception to the caller.
        """
        plugin_class = self._plugins.get(plugin_name)
        if plugin_class is None:
            raise MCPPluginNotFoundError(
                f"No plugin registered under name '{plugin_name}'. "
                f"Available plugins: {self.get_available_plugins()}"
            )

        try:
            plugin_instance = plugin_class()
            return plugin_instance.execute(query)
        except Exception as e:  # noqa: BLE001 - intentionally broad: isolate caller from plugin failures
            logger.error("Plugin '%s' raised during execute(): %s", plugin_name, e, exc_info=True)
            return {"error": str(e), "plugin": plugin_name}

    def get_available_plugins(self) -> List[str]:
        """Return the names of all currently registered plugins."""
        return list(self._plugins.keys())


# Module-level default registry instance for convenience, mirroring the
# pattern of a single shared MCP layer that other modules (e.g. a future
# knowledge_retriever.py) can import and use directly.
default_registry = MCPRegistry()
