"""All MCP tool functions for the nvim-mcp server."""

from __future__ import annotations

import json
import os
import re
import signal
import tempfile
import time

import pynvim

from nvim_mcp.pty import (
    _create_pty,
    _dismiss_press_enter,
    _init_pyte,
    _pty_send_raw,
    _spawn_nvim,
    _teardown,
    _wait_for_socket,
    _with_drained_pty,
)
from nvim_mcp.rendering import _render_screen_to_png
from nvim_mcp.rpc import NvimRPC
from nvim_mcp.state import (
    NvimSession,
    _ADAPTIVE_STARTUP_POLL_INTERVAL,
    _DRAIN_QUIET_MS,
    _HEALTH_CHECK_TIMEOUT,
    _LAZY_LOAD_TIMEOUT,
    _MAX_TERMINAL_SIZE,
    _NVIM_ERRORS,
    _RPC_DEFAULT_TIMEOUT,
    _RPC_FLUSH_TIMEOUT,
    _RPC_POLL_TIMEOUT,
    _SOCKET_CONNECT_TIMEOUT,
    _VALID_SEVERITIES,
    _require_nvim,
    _rpc_timeout,
    get_session,
    mcp,
    set_session,
)
from nvim_mcp.terminal import (
    _check_terminal_available,
    _screenshot_terminal,
    _start_terminal,
    _teardown_terminal,
)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def _start_headless(
    cmd_extra: list[str] | None, clean: bool, env: dict[str, str]
) -> tuple[NvimSession, str | None]:
    """Start nvim in embed+headless mode. Returns (session, error_or_None)."""
    s = NvimSession()
    cmd = ["nvim", "--embed", "--headless"]
    if clean:
        cmd.append("--clean")
    if cmd_extra:
        cmd.extend(cmd_extra)
    old_env = {}
    for k, v in env.items():
        if os.environ.get(k) != v:
            old_env[k] = os.environ.get(k)
            os.environ[k] = v
    try:
        s.nvim = pynvim.attach("child", argv=cmd)
    except Exception as e:
        s.nvim = None
        return s, f"Failed to start Neovim: {e}"
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return s, None


def _start_pty(
    cmd_extra: list[str] | None,
    clean: bool,
    env: dict[str, str],
    rows: int,
    cols: int,
) -> tuple[NvimSession, str | None]:
    """Start nvim in PTY+socket mode. Returns (session, error_or_None)."""
    s = NvimSession()

    s.socket_dir = tempfile.mkdtemp(prefix=f"nvim-mcp-{os.getpid()}-")
    s.socket_path = os.path.join(s.socket_dir, "nvim.sock")

    master_fd, slave_fd = _create_pty(rows, cols)

    try:
        s.proc = _spawn_nvim(slave_fd, s.socket_path, cmd_extra, clean, env)
    except Exception as e:
        os.close(master_fd)
        os.close(slave_fd)
        os.rmdir(s.socket_dir)
        return s, f"Failed to start Neovim: {e}"

    os.close(slave_fd)
    s.pty_master_fd = master_fd

    try:
        _init_pyte(s, cols, rows)
    except Exception as e:
        _teardown(s, kill_proc=True)
        return s, f"Failed to initialize terminal emulator: {e}"

    conn = _wait_for_socket(s.socket_path, timeout=_SOCKET_CONNECT_TIMEOUT)
    if conn is None:
        _teardown(s, kill_proc=True)
        return s, "Failed to connect to Neovim socket (timeout)."

    s.nvim = conn

    _dismiss_press_enter(s)

    return s, None


@mcp.tool()
def nvim_start(
    config: str = "~/.config/nvim",
    clean: bool = False,
    headless: bool = False,
    terminal: str = "",
    args: list[str] | None = None,
    rows: int = 24,
    cols: int = 80,
) -> str:
    """Start an embedded Neovim instance.

    Always call first. Check nvim_is_running before starting a second instance.

    Args:
        config: Path to the Neovim config directory (must be named "nvim",
              e.g. ~/.config/nvim). The parent directory is used as XDG_CONFIG_HOME.
        clean: If True, start with --clean (no config/plugins).
        headless: If True, start with --headless --embed (no PTY, no screenshots).
                  If False (default), start with PTY + socket for full screenshot support.
        terminal: Launch nvim inside a real terminal emulator for pixel-perfect
                  screenshots. Supported: "kitty", "ghostty", "iterm2" (macOS only).
                  Mutually exclusive with headless. Empty string (default) uses PTY mode.
        args: Extra CLI arguments to pass to nvim.
        rows: Terminal rows (only used when headless=False).
        cols: Terminal columns (only used when headless=False).
    """
    _session = get_session()

    if _session is not None and _session.nvim is not None:
        return f"Neovim is already running (PID {_session.pid}). Stop it first."

    if terminal and headless:
        return "Error: 'terminal' and 'headless' are mutually exclusive."

    if terminal:
        term_err = _check_terminal_available(terminal)
        if term_err:
            return f"Error: {term_err}"

    if not headless and not terminal and (rows < 1 or cols < 1 or rows > _MAX_TERMINAL_SIZE or cols > _MAX_TERMINAL_SIZE):
        return f"Error: rows and cols must be between 1 and {_MAX_TERMINAL_SIZE}."

    config = os.path.expanduser(config)

    env = os.environ.copy()
    if not clean and os.path.isdir(config):
        env["XDG_CONFIG_HOME"] = os.path.dirname(config)

    if headless:
        s, err = _start_headless(cmd_extra=args, clean=clean, env=env)
    elif terminal:
        s, err = _start_terminal(
            terminal=terminal, cmd_extra=args, clean=clean, env=env,
            rows=rows, cols=cols,
        )
    else:
        s, err = _start_pty(cmd_extra=args, clean=clean, env=env, rows=rows, cols=cols)
    if err:
        return err

    try:
        s.pid = s.nvim.eval("getpid()")
    except _NVIM_ERRORS:
        # May be blocked on wait_return from plugin errors — dismiss and retry
        if terminal:
            # Terminal mode: dismiss via RPC input
            try:
                if isinstance(s.nvim, NvimRPC):
                    s.nvim.input("\r")
            except _NVIM_ERRORS:
                pass
            time.sleep(_ADAPTIVE_STARTUP_POLL_INTERVAL)
            try:
                s.pid = s.nvim.eval("getpid()")
            except _NVIM_ERRORS as e:
                _teardown(s, kill_proc=True)
                _teardown_terminal(s)
                return f"Failed to initialize Neovim: {e}"
        elif not headless and s.pty_master_fd is not None:
            try:
                os.write(s.pty_master_fd, b"\r")
            except OSError:
                pass
            time.sleep(_ADAPTIVE_STARTUP_POLL_INTERVAL)
            try:
                s.pid = s.nvim.eval("getpid()")
            except _NVIM_ERRORS as e:
                _teardown(s, kill_proc=True)
                return f"Failed to initialize Neovim: {e}"
        else:
            _teardown(s, kill_proc=True)
            return "Failed to initialize Neovim: connection timed out"

    set_session(s)
    _session = s

    msg = f"Neovim started (PID {_session.pid}"

    if not clean and os.path.isdir(config):
        if not _wait_for_lazy(_session, timeout=_LAZY_LOAD_TIMEOUT):
            if terminal:
                mode_label = f"terminal:{terminal}"
            elif headless:
                mode_label = "headless"
            else:
                mode_label = "PTY"
            # Restore 'more' before returning on timeout
            if not headless:
                try:
                    _session.nvim.command("set more")
                except Exception:
                    pass
            msg += f", {mode_label}). Warning: lazy.nvim did not finish loading within {_LAZY_LOAD_TIMEOUT}s."
            return msg

    # Restore 'more' option after lazy.nvim finishes.
    # Kept off during loading so plugin errors don't trigger "-- More --" pager.
    if not headless:
        try:
            _session.nvim.command("set more")
        except Exception:
            pass

    if terminal:
        mode_str = f"terminal:{terminal}"
    elif headless:
        mode_str = "headless"
    else:
        mode_str = f"PTY {cols}x{rows}"
    return f"Neovim started (PID {_session.pid}, {mode_str})."


def _wait_for_lazy(s: NvimSession, timeout: int = _LAZY_LOAD_TIMEOUT) -> bool:
    """Poll until lazy.nvim reports all plugins are loaded, or timeout.

    While waiting, sends \\r through the PTY (or via RPC input in terminal
    mode) to dismiss any 'Press ENTER' prompts that plugins may trigger
    during loading.
    """
    if s.nvim is None:
        return False
    has_pty = s.pty_master_fd is not None
    has_rpc = isinstance(s.nvim, NvimRPC)
    if (has_pty or s.terminal) and has_rpc:
        s.nvim.set_timeout(_RPC_POLL_TIMEOUT)
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            try:
                loaded = s.nvim.exec_lua(
                    'local ok, lazy = pcall(require, "lazy"); '
                    "if ok then return lazy.stats().loaded else return -1 end"
                )
                if isinstance(loaded, int) and loaded >= 0:
                    return True
            except _NVIM_ERRORS:
                # RPC timed out — nvim likely blocked on wait_return
                if has_pty:
                    try:
                        os.write(s.pty_master_fd, b"\r")
                    except OSError:
                        pass
                elif s.terminal and has_rpc:
                    try:
                        s.nvim.input("\r")
                    except _NVIM_ERRORS:
                        pass
            time.sleep(_ADAPTIVE_STARTUP_POLL_INTERVAL)
    finally:
        if (has_pty or s.terminal) and has_rpc:
            s.nvim.set_timeout(_SOCKET_CONNECT_TIMEOUT)
    return False


@mcp.tool()
def nvim_stop() -> str:
    """Stop the running Neovim instance."""
    _session = get_session()

    if _session is None or _session.nvim is None:
        return "Neovim is not running."

    pid = _session.pid
    was_headless = _session.proc is None
    was_terminal = _session.terminal is not None

    try:
        _session.nvim.command("qall!")
    except _NVIM_ERRORS:
        pass

    _teardown(_session, kill_proc=True)

    if was_terminal:
        _teardown_terminal(_session)

    # Headless/embed mode: no subprocess, kill by PID
    if was_headless and not was_terminal and pid is not None:
        try:
            os.kill(pid, 0)
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
            try:
                os.kill(pid, 0)
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        except ProcessLookupError:
            pass

    set_session(None)
    return "Neovim stopped."


@mcp.tool()
def nvim_is_running() -> str:
    """Check if a Neovim instance is currently running.

    Probes the connection to detect a crashed Neovim process.
    """
    _session = get_session()
    if _session is None or _session.nvim is None:
        return json.dumps({"running": False})
    try:
        _session.nvim.eval("1")
        return json.dumps({"running": True, "pid": _session.pid})
    except _NVIM_ERRORS:
        return json.dumps({"running": False, "error": "connection lost"})


# ---------------------------------------------------------------------------
# Interaction
# ---------------------------------------------------------------------------


@mcp.tool()
def nvim_execute(command: str, timeout: float = _RPC_DEFAULT_TIMEOUT) -> str:
    """Execute an Ex command in Neovim and return its output.

    Common commands: "edit <file>", "Lazy sync", "checkhealth", "w", "bdelete".
    Returns command output only — use nvim_get_buffer to read buffer contents.

    Args:
        command: The Ex command to run (without leading colon), e.g. "Lazy health".
        timeout: RPC timeout in seconds. Increase for slow commands (e.g. Lazy sync).
    """
    try:
        nvim = _require_nvim()
        with _rpc_timeout(nvim, timeout):
            output = nvim.command_output(command)
            return output if output else "(no output)"
    except _NVIM_ERRORS as e:
        return f"Error: {e}"


@mcp.tool()
def nvim_lua(code: str, timeout: float = _RPC_DEFAULT_TIMEOUT) -> str:
    """Execute Lua code in Neovim and return the result as JSON.

    Use for anything not expressible as an Ex command. Must use 'return' to get values back.

    Args:
        code: Lua code to execute. Use 'return' to get a value back.
              Example: "return vim.api.nvim_buf_line_count(0)"
        timeout: RPC timeout in seconds.
    """
    try:
        nvim = _require_nvim()
        with _rpc_timeout(nvim, timeout):
            result = nvim.exec_lua(code)
            return json.dumps(result, default=str, indent=2)
    except _NVIM_ERRORS as e:
        return f"Error: {e}"


# Map of special key names to their raw byte equivalents for PTY fallback
_RAW_KEY_MAP = {
    "<ESC>": b"\x1b",
    "<CR>": b"\r",
    "<C-C>": b"\x03",
    "<C-[>": b"\x1b",
    "<C-]>": b"\x1d",
    "<C-\\>": b"\x1c",
    "<C-W>": b"\x17",
    "<C-A>": b"\x01",
    "<C-B>": b"\x02",
    "<C-D>": b"\x04",
    "<C-E>": b"\x05",
    "<C-F>": b"\x06",
    "<C-G>": b"\x07",
    "<C-H>": b"\x08",
    "<C-J>": b"\x0a",
    "<C-K>": b"\x0b",
    "<C-L>": b"\x0c",
    "<C-N>": b"\x0e",
    "<C-O>": b"\x0f",
    "<C-P>": b"\x10",
    "<C-R>": b"\x12",
    "<C-T>": b"\x14",
    "<C-U>": b"\x15",
    "<C-V>": b"\x16",
    "<C-X>": b"\x18",
    "<C-Z>": b"\x1a",
    "<TAB>": b"\t",
    "<BS>": b"\x7f",
    "<SPACE>": b" ",
}

_SPECIAL_KEY_RE = re.compile(r"(<[^>]+>)")


def _keys_to_raw(keys: str) -> bytes:
    """Convert key notation string to raw bytes for PTY write.

    Key names are case-insensitive (e.g. <cr>, <CR>, <Cr> all work).
    Unknown special keys are silently skipped.
    """
    parts = _SPECIAL_KEY_RE.split(keys)
    result = b""
    for part in parts:
        upper = part.upper()
        if upper in _RAW_KEY_MAP:
            result += _RAW_KEY_MAP[upper]
        elif part.startswith("<") and part.endswith(">"):
            pass
        else:
            result += part.encode("utf-8")
    return result


@mcp.tool()
def nvim_send_keys(keys: str, escape: bool = True, timeout: float = _RPC_DEFAULT_TIMEOUT) -> str:
    """Send keystrokes to Neovim.

    Prefer nvim_execute for Ex commands — use this for normal-mode actions,
    insert-mode typing, or dismissing prompts. Uses RPC by default; falls back
    to PTY write if RPC is blocked (e.g. by Telescope or ToggleTerm).

    Args:
        keys: Key sequence to send. Supports special keys like <CR>, <Esc>,
              <C-w>, <C-c>, etc. Note: <Leader> is a vim mapping concept,
              not a key code. Use the actual leader key instead (e.g.
              <Space>e for <leader>e when leader is Space).
        escape: If True, translate special key notation via replace_termcodes.
        timeout: RPC timeout in seconds. Use a short value (1-2s) when you
                 expect nvim might be blocked.
    """
    try:
        nvim = _require_nvim()
        _session = get_session()
        with _rpc_timeout(nvim, timeout):
            try:
                if escape:
                    keys_translated = nvim.replace_termcodes(keys, True, True, True)
                else:
                    keys_translated = keys
                nvim.feedkeys(keys_translated, "n", True)
                nvim.command("")
                return f"Sent keys: {keys}"
            except _NVIM_ERRORS:
                # RPC blocked — fall back to PTY raw write or RPC input
                if _session and _session.pty_master_fd is not None:
                    raw = _keys_to_raw(keys)
                    if raw and _pty_send_raw(_session, raw):
                        return f"Sent keys via PTY (RPC was blocked): {keys}"
                elif _session and _session.terminal and isinstance(_session.nvim, NvimRPC):
                    try:
                        _session.nvim.input(keys)
                        return f"Sent keys via nvim_input (RPC feedkeys was blocked): {keys}"
                    except _NVIM_ERRORS:
                        pass
                raise
    except _NVIM_ERRORS as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


@mcp.tool()
def nvim_get_buffer(buffer_id: int = 0) -> str:
    """Get the contents of a buffer.

    Use after nvim_execute("edit ...") to read file contents, or to inspect
    output buffers like :checkhealth.

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
    except _NVIM_ERRORS as e:
        return f"Error: {e}"


@mcp.tool()
def nvim_get_state() -> str:
    """Get current Neovim state: mode, cursor, file, modified, filetype, buffers, cwd.

    Call first to orient — shows what file is open, cursor position, mode.
    Use to verify effects of previous actions."""
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
    except _NVIM_ERRORS as e:
        return f"Error: {e}"


@mcp.tool()
def nvim_get_messages(clear: bool = False) -> str:
    """Get Neovim's :messages output. Primary tool for checking errors.

    Call after nvim_start to check startup errors. Call after any command
    that might produce warnings.

    Args:
        clear: If True, clear messages after reading.
    """
    try:
        nvim = _require_nvim()
        output = nvim.command_output("messages")
        if clear:
            nvim.command("messages clear")
        return output if output.strip() else "(no messages)"
    except _NVIM_ERRORS as e:
        return f"Error: {e}"


@mcp.tool()
def nvim_get_diagnostics(buffer_id: int = 0, severity: str = "") -> str:
    """Get LSP diagnostics for a buffer.

    Requires LSP to be attached — open a file with a configured server first.
    Returns empty list if no LSP is active.

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
    except _NVIM_ERRORS as e:
        return f"Error: {e}"


@mcp.tool()
def nvim_screenshot(output_path: str = "") -> str:
    """Capture a PNG screenshot of the current Neovim terminal display.

    Use to see visual state when text tools aren't enough (UI popups, color
    themes, layout). Read the returned path with the Read tool to view the image.
    In PTY mode: renders the pyte terminal emulator to PNG.
    In terminal mode: captures the actual terminal window via screencapture (macOS).
    Works even when RPC is blocked (e.g. by Telescope or ToggleTerm).

    Args:
        output_path: File path for the PNG. If empty, uses a temp file.
    """
    try:
        _require_nvim()
        s = get_session()

        if not output_path:
            f = tempfile.NamedTemporaryFile(
                suffix=".png", prefix="nvim-screenshot-", delete=False
            )
            output_path = f.name
            f.close()

        # Terminal mode — capture actual window
        if s.terminal is not None:
            # Try RPC flush with short timeout
            with _rpc_timeout(s.nvim, _RPC_FLUSH_TIMEOUT):
                try:
                    s.nvim.eval("1")
                except _NVIM_ERRORS:
                    pass
            return _screenshot_terminal(s, output_path)

        # PTY mode — render pyte screen
        if s.pyte_screen is None:
            return "Error: screenshot not available (nvim started in headless mode)"
        # Try RPC flush with short timeout; proceed with screenshot regardless
        with _rpc_timeout(s.nvim, _RPC_FLUSH_TIMEOUT):
            try:
                s.nvim.eval("1")
            except _NVIM_ERRORS:
                pass  # RPC blocked — screenshot from current PTY state anyway
        with _with_drained_pty(s, quiet_ms=_DRAIN_QUIET_MS):
            with s.pty_lock:
                _render_screen_to_png(s.pyte_screen, output_path)
        return os.path.abspath(output_path)
    except _NVIM_ERRORS as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def _check_startup_messages(nvim: pynvim.Nvim | NvimRPC) -> str | None:
    """Return startup :messages content, or None if empty."""
    msgs = nvim.command_output("messages").strip()
    return f"[startup messages]\n{msgs}" if msgs else None


def _run_checkhealth(nvim: pynvim.Nvim | NvimRPC) -> str | None:
    """Run :checkhealth and return ERROR/WARNING lines, or None.

    Uses a generous timeout since :checkhealth is synchronous but can be
    slow (2-5s with many providers).
    """
    try:
        nvim.command("messages clear")
        with _rpc_timeout(nvim, _HEALTH_CHECK_TIMEOUT):
            nvim.command("checkhealth")
        health_lines = nvim.api.buf_get_lines(
            nvim.current.buffer.number, 0, -1, False
        )
        health_issues = [
            line for line in health_lines
            if "ERROR" in line and line.strip().startswith("-")
            or "WARNING" in line and line.strip().startswith("-")
        ]
        nvim.command("bdelete!")
        nvim.command("messages clear")
        if health_issues:
            return (
                f"[:checkhealth] {len(health_issues)} issue(s):\n"
                + "\n".join(health_issues[:20])
            )
    except _NVIM_ERRORS:
        pass
    return None


def _trigger_lazy_plugins(nvim: pynvim.Nvim | NvimRPC) -> str | None:
    """Open a scratch buffer to trigger FileType autocommands, return new messages."""
    try:
        nvim.command("messages clear")
        nvim.command("enew | setlocal buftype=nofile filetype=lua")
        msgs = nvim.command_output("messages").strip()
        nvim.command("bdelete!")
        return f"[lazy-load messages]\n{msgs}" if msgs else None
    except _NVIM_ERRORS:
        return None


def _check_plugin_errors(nvim: pynvim.Nvim | NvimRPC) -> tuple[dict, str | None]:
    """Query lazy.nvim for plugin statuses. Returns (report_dict, issue_or_None)."""
    report = nvim.exec_lua("""
        local ok, lazy_config = pcall(require, "lazy.core.config")
        if not ok then return { has_lazy = false } end
        local results = { has_lazy = true, plugins = {} }
        for name, plugin in pairs(lazy_config.plugins) do
            results.plugins[name] = {
                loaded = plugin._.loaded ~= nil,
                has_errors = plugin._.has_errors or false,
            }
        end
        local stats = require("lazy").stats()
        results.loaded = stats.loaded
        results.count = stats.count
        return results
    """)
    if not isinstance(report, dict):
        return {}, None
    error_plugins = []
    if report.get("has_lazy"):
        for name, info in report.get("plugins", {}).items():
            if isinstance(info, dict) and info.get("has_errors"):
                error_plugins.append(name)
    issue = f"[plugins with errors] {', '.join(sorted(error_plugins))}" if error_plugins else None
    return report, issue


def _check_diagnostics(nvim: pynvim.Nvim | NvimRPC) -> str | None:
    """Check diagnostics across all buffers. Returns summary or None."""
    diag_summary = nvim.exec_lua("""
        local all = vim.diagnostic.get()
        if #all == 0 then return nil end
        local by_sev = {}
        for _, d in ipairs(all) do
            local sev = vim.diagnostic.severity[d.severity] or "?"
            by_sev[sev] = (by_sev[sev] or 0) + 1
        end
        return by_sev
    """)
    if diag_summary and isinstance(diag_summary, dict):
        parts = [f"{sev}: {count}" for sev, count in diag_summary.items()]
        return f"[diagnostics] {', '.join(parts)}"
    return None


@mcp.tool()
def nvim_health_check() -> str:
    """Comprehensive config health check.

    Slow (~5-10s). Use only after config changes, plugin updates, or nvim
    upgrades — not for routine checks. For quick error checks, use
    nvim_get_messages instead. Runs :checkhealth, checks :messages, triggers
    lazy-loaded plugins, then reports all errors and plugin statuses.
    """
    try:
        nvim = _require_nvim()

        issues: list[str] = []

        startup = _check_startup_messages(nvim)
        if startup:
            issues.append(startup)

        checkhealth = _run_checkhealth(nvim)
        if checkhealth:
            issues.append(checkhealth)

        lazy_msgs = _trigger_lazy_plugins(nvim)
        if lazy_msgs:
            issues.append(lazy_msgs)

        plugin_report, plugin_issue = _check_plugin_errors(nvim)
        if plugin_issue:
            issues.append(plugin_issue)

        diags = _check_diagnostics(nvim)
        if diags:
            issues.append(diags)

        # Build report
        if isinstance(plugin_report, dict) and plugin_report.get("has_lazy"):
            header = f"Plugins: {plugin_report.get('loaded', '?')}/{plugin_report.get('count', '?')} loaded"
        else:
            header = "No lazy.nvim detected"

        if not issues:
            return f"Health check passed. {header}. No errors."

        return f"Health check: {len(issues)} issue(s) found. {header}.\n\n" + "\n\n".join(issues)
    except _NVIM_ERRORS as e:
        return f"Error: {e}"
