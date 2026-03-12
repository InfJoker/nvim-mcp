"""Shared state, constants, and session management for nvim-mcp."""

from __future__ import annotations

import contextlib
import subprocess
import threading
from dataclasses import dataclass, field

from typing import TYPE_CHECKING

import pynvim
import pyte
from mcp.server.fastmcp import FastMCP

if TYPE_CHECKING:
    from nvim_mcp.rpc import NvimRPC

mcp = FastMCP("neovim")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PTY_READ_SIZE = 65536
_SOCKET_CONNECT_TIMEOUT = 10.0
_LAZY_LOAD_TIMEOUT = 30
_PROCESS_TERM_TIMEOUT = 3
_PROCESS_KILL_TIMEOUT = 1
_DRAIN_HARD_TIMEOUT = 2.0
_DRAIN_QUIET_MS = 50
_READER_PAUSE_TIMEOUT = 0.5
_ADAPTIVE_STARTUP_TIMEOUT = 15.0
_ADAPTIVE_STARTUP_POLL_INTERVAL = 0.2
_RPC_POLL_TIMEOUT = 0.5
_RPC_DEFAULT_TIMEOUT = 10.0
_RPC_FLUSH_TIMEOUT = 1.0
_HEALTH_CHECK_TIMEOUT = 30.0
_MAX_TERMINAL_SIZE = 500
_TERMINAL_SOCKET_CONNECT_TIMEOUT = 15.0
_TERMINAL_NAMES = {"kitty", "ghostty", "iterm2"}
_WINDOW_ID_POLL_TIMEOUT = 5.0
_WINDOW_ID_POLL_INTERVAL = 0.3
_OSASCRIPT_FALLBACK_TIMEOUT = 3
_APPLESCRIPT_FOCUS_DELAY = 0.5
_APPLESCRIPT_LAUNCH_TIMEOUT = 15
_KITTEN_RPC_TIMEOUT = 5
_ITERM2_CLOSE_TIMEOUT = 5

_VALID_SEVERITIES = {"ERROR", "WARN", "INFO", "HINT"}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class NvimRPCError(Exception):
    """Error returned by Neovim RPC."""


_NVIM_ERRORS = (pynvim.NvimError, NvimRPCError, RuntimeError, EOFError, OSError)


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------


@dataclass
class NvimSession:
    """Encapsulates all mutable state for a single Neovim session."""

    nvim: pynvim.Nvim | NvimRPC | None = None
    pid: int | None = None
    proc: subprocess.Popen | None = None
    pty_master_fd: int | None = None
    socket_path: str | None = None
    socket_dir: str | None = None
    pyte_screen: pyte.Screen | None = None
    pyte_stream: pyte.Stream | None = None
    pty_lock: threading.Lock = field(default_factory=threading.Lock)
    pty_reader_active: threading.Event = field(default_factory=threading.Event)
    pty_reader_paused: threading.Event = field(default_factory=threading.Event)
    pty_reader_ref: threading.Thread | None = None
    terminal: str | None = None
    terminal_proc: subprocess.Popen | None = None
    terminal_window_id: int | None = None
    terminal_title: str | None = None
    kitty_socket: str | None = None
    kitty_pid: int | None = None
    ghostty_pid: int | None = None
    ghostty_windows_before: set[int] | None = None
    iterm2_window_id: str | None = None


_session: NvimSession | None = None


def get_session() -> NvimSession | None:
    return _session


def set_session(s: NvimSession | None) -> None:
    global _session
    _session = s


# ---------------------------------------------------------------------------
# Session accessors
# ---------------------------------------------------------------------------


def _require_nvim() -> pynvim.Nvim | NvimRPC:
    """Return the active nvim connection or raise."""
    if _session is None or _session.nvim is None:
        raise RuntimeError("Neovim is not running. Call nvim_start first.")
    return _session.nvim


@contextlib.contextmanager
def _rpc_timeout(nvim, timeout: float):
    """Temporarily override RPC timeout for NvimRPC connections.

    Saves and restores the previous timeout via ``get_timeout()``.
    No-op for pynvim (headless) connections.
    """
    # Import here to avoid circular import at module level
    from nvim_mcp.rpc import NvimRPC

    if isinstance(nvim, NvimRPC):
        prev = nvim.get_timeout()
        nvim.set_timeout(timeout)
        try:
            yield
        finally:
            nvim.set_timeout(prev)
    else:
        yield
