"""Playwright-style MCP server for Neovim.

Spawns an embedded Neovim process and exposes tools for lifecycle management,
interaction (commands, Lua, keystrokes), and state inspection.

This is a thin entry point. All logic lives in the nvim_mcp package.
"""

from nvim_mcp.state import get_session, mcp
from nvim_mcp.tools import (
    nvim_execute,
    nvim_get_buffer,
    nvim_get_diagnostics,
    nvim_get_messages,
    nvim_get_state,
    nvim_health_check,
    nvim_is_running,
    nvim_lua,
    nvim_resize,
    nvim_screenshot,
    nvim_send_keys,
    nvim_start,
    nvim_stop,
)


def __getattr__(name: str):
    if name == "_session":
        return get_session()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    mcp.run()
