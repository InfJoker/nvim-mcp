"""Playwright-style MCP server for Neovim.

Spawns an embedded Neovim process and exposes tools for lifecycle management,
interaction (commands, Lua, keystrokes), and state inspection.
"""

from __future__ import annotations

import atexit
import json
import os
import signal
import time

import pynvim
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("neovim")

_nvim: pynvim.Nvim | None = None
_nvim_pid: int | None = None

_VALID_SEVERITIES = {"ERROR", "WARN", "INFO", "HINT"}


def _require_nvim() -> pynvim.Nvim:
    if _nvim is None:
        raise RuntimeError("Neovim is not running. Call nvim_start first.")
    return _nvim


def _cleanup() -> None:
    """Kill any orphaned nvim process on exit."""
    if _nvim_pid is not None:
        try:
            os.kill(_nvim_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


atexit.register(_cleanup)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@mcp.tool()
def nvim_start(
    config: str = "~/.config/nvim",
    clean: bool = False,
    headless: bool = True,
    args: list[str] | None = None,
) -> str:
    """Start an embedded Neovim instance.

    Args:
        config: Path to the Neovim config directory.
        clean: If True, start with --clean (no config/plugins).
        headless: If True, start with --headless (no UI).
        args: Extra CLI arguments to pass to nvim.
    """
    global _nvim, _nvim_pid

    if _nvim is not None:
        return f"Neovim is already running (PID {_nvim_pid}). Stop it first."

    config = os.path.expanduser(config)

    cmd = ["nvim", "--embed"]
    if headless:
        cmd.append("--headless")
    if clean:
        cmd.append("--clean")
    if args:
        cmd.extend(args)

    env = os.environ.copy()
    if not clean and os.path.isdir(config):
        env["XDG_CONFIG_HOME"] = os.path.dirname(config)

    try:
        _nvim = pynvim.attach("child", argv=cmd, env=env)
    except Exception as e:
        _nvim = None
        return f"Failed to start Neovim: {e}"

    try:
        _nvim_pid = _nvim.eval("getpid()")
    except Exception as e:
        # Cleanup the connection if we can't get PID
        try:
            _nvim.command("qall!")
        except Exception:
            pass
        _nvim = None
        _nvim_pid = None
        return f"Failed to initialize Neovim: {e}"

    msg = f"Neovim started (PID {_nvim_pid})."

    if not clean and os.path.isdir(config):
        if not _wait_for_lazy(timeout=30):
            msg += " Warning: lazy.nvim did not finish loading within 30s."

    return msg


def _wait_for_lazy(timeout: int = 30) -> bool:
    """Poll until lazy.nvim reports all plugins are loaded, or timeout.

    Returns True if lazy loaded successfully, False on timeout.
    """
    if _nvim is None:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            loaded = _nvim.exec_lua(
                'local ok, lazy = pcall(require, "lazy"); '
                "if ok then return lazy.stats().loaded else return -1 end"
            )
            if isinstance(loaded, int) and loaded >= 0:
                return True
        except pynvim.NvimError:
            pass
        time.sleep(0.5)
    return False


@mcp.tool()
def nvim_stop() -> str:
    """Stop the running Neovim instance."""
    global _nvim, _nvim_pid

    if _nvim is None:
        return "Neovim is not running."

    pid = _nvim_pid

    try:
        _nvim.command("qall!")
    except (pynvim.NvimError, EOFError, OSError):
        pass

    # Force kill if still alive
    if pid is not None:
        try:
            os.kill(pid, 0)  # Check if alive
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
            try:
                os.kill(pid, 0)  # Still alive after SIGTERM?
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # SIGTERM was sufficient
        except ProcessLookupError:
            pass  # Already dead

    _nvim = None
    _nvim_pid = None
    return "Neovim stopped."


@mcp.tool()
def nvim_is_running() -> str:
    """Check if a Neovim instance is currently running."""
    if _nvim is None:
        return json.dumps({"running": False})
    return json.dumps({"running": True, "pid": _nvim_pid})


# ---------------------------------------------------------------------------
# Interaction
# ---------------------------------------------------------------------------


@mcp.tool()
def nvim_execute(command: str) -> str:
    """Execute an Ex command in Neovim and return its output.

    Args:
        command: The Ex command to run (without leading colon), e.g. "Lazy health".
    """
    try:
        nvim = _require_nvim()
        output = nvim.command_output(command)
        return output if output else "(no output)"
    except (pynvim.NvimError, RuntimeError) as e:
        return f"Error: {e}"


@mcp.tool()
def nvim_lua(code: str) -> str:
    """Execute Lua code in Neovim and return the result as JSON.

    Args:
        code: Lua code to execute. Use 'return' to get a value back.
              Example: "return vim.api.nvim_buf_line_count(0)"
    """
    try:
        nvim = _require_nvim()
        result = nvim.exec_lua(code)
        return json.dumps(result, default=str, indent=2)
    except (pynvim.NvimError, RuntimeError) as e:
        return f"Error: {e}"


@mcp.tool()
def nvim_send_keys(keys: str, escape: bool = True) -> str:
    """Send keystrokes to Neovim.

    Args:
        keys: Key sequence to send. Supports special keys like <CR>, <Esc>,
              <C-w>, <Leader>, etc.
        escape: If True, translate special key notation via replace_termcodes.
    """
    try:
        nvim = _require_nvim()
        if escape:
            keys_translated = nvim.replace_termcodes(keys, True, True, True)
        else:
            keys_translated = keys
        nvim.feedkeys(keys_translated, "n", True)
        # Flush the input queue so keys are processed before the next tool call
        nvim.command("")
        return f"Sent keys: {keys}"
    except (pynvim.NvimError, RuntimeError) as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


@mcp.tool()
def nvim_get_buffer(buffer_id: int = 0) -> str:
    """Get the contents of a buffer.

    Args:
        buffer_id: Buffer number (handle). 0 means current buffer.
    """
    try:
        nvim = _require_nvim()
        if buffer_id == 0:
            buffer_id = nvim.current.buffer.number
        name = nvim.api.buf_get_name(buffer_id) or "(unnamed)"
        lines = nvim.api.buf_get_lines(buffer_id, 0, -1, False)
        numbered = "\n".join(f"{i + 1:4d} | {line}" for i, line in enumerate(lines))
        return f"Buffer: {name} ({len(lines)} lines)\n{numbered}"
    except (pynvim.NvimError, RuntimeError) as e:
        return f"Error: {e}"


@mcp.tool()
def nvim_get_state() -> str:
    """Get current Neovim state: mode, cursor, file, modified, filetype, buffers, cwd."""
    try:
        nvim = _require_nvim()
        state = nvim.exec_lua("""
            local bufs = {}
            for _, b in ipairs(vim.api.nvim_list_bufs()) do
                if vim.api.nvim_buf_is_loaded(b) then
                    table.insert(bufs, {
                        id = b,
                        name = vim.api.nvim_buf_get_name(b),
                        modified = vim.bo[b].modified,
                    })
                end
            end
            return {
                mode = vim.api.nvim_get_mode().mode,
                cursor = vim.api.nvim_win_get_cursor(0),
                current_file = vim.api.nvim_buf_get_name(0),
                modified = vim.bo.modified,
                filetype = vim.bo.filetype,
                buffer_list = bufs,
                cwd = vim.fn.getcwd(),
            }
        """)
        return json.dumps(state, default=str, indent=2)
    except (pynvim.NvimError, RuntimeError) as e:
        return f"Error: {e}"


@mcp.tool()
def nvim_get_messages(clear: bool = False) -> str:
    """Get Neovim's :messages output. Primary tool for checking errors.

    Args:
        clear: If True, clear messages after reading.
    """
    try:
        nvim = _require_nvim()
        output = nvim.command_output("messages")
        if clear:
            nvim.command("messages clear")
        return output if output.strip() else "(no messages)"
    except (pynvim.NvimError, RuntimeError) as e:
        return f"Error: {e}"


@mcp.tool()
def nvim_get_diagnostics(buffer_id: int = 0, severity: str = "") -> str:
    """Get LSP diagnostics for a buffer.

    Args:
        buffer_id: Buffer number. 0 means current buffer.
        severity: Filter by severity: "ERROR", "WARN", "INFO", "HINT", or "" for all.
    """
    if severity and severity.upper() not in _VALID_SEVERITIES:
        return f"Error: invalid severity '{severity}'. Use ERROR, WARN, INFO, or HINT."
    try:
        nvim = _require_nvim()
        result = nvim.exec_lua(
            """
            local bufnr, sev_filter = ...
            if bufnr == 0 then bufnr = vim.api.nvim_get_current_buf() end
            local opts = { bufnr = bufnr }
            if sev_filter and sev_filter ~= "" then
                opts.severity = vim.diagnostic.severity[sev_filter:upper()]
            end
            local diagnostics = vim.diagnostic.get(bufnr, opts)
            local result = {}
            local sev_names = { "ERROR", "WARN", "INFO", "HINT" }
            for _, d in ipairs(diagnostics) do
                table.insert(result, {
                    lnum = d.lnum + 1,
                    col = d.col + 1,
                    severity = sev_names[d.severity] or tostring(d.severity),
                    message = d.message,
                    source = d.source or "",
                })
            end
            return result
        """,
            buffer_id,
            severity,
        )
        return json.dumps(result, default=str, indent=2) if result else "No diagnostics."
    except (pynvim.NvimError, RuntimeError) as e:
        return f"Error: {e}"


if __name__ == "__main__":
    mcp.run()
