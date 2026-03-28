"""PTY creation, reader thread, drain logic, SGR fix, and process teardown."""

from __future__ import annotations

import atexit
import contextlib
import fcntl
import os
import pty
import re
import select
import signal
import struct
import subprocess
import termios
import threading
import time

import pyte

from nvim_mcp.rpc import NvimRPC
from nvim_mcp.state import (
    NvimSession,
    _ADAPTIVE_STARTUP_POLL_INTERVAL,
    _ADAPTIVE_STARTUP_TIMEOUT,
    _DRAIN_HARD_TIMEOUT,
    _DRAIN_QUIET_MS,
    _NVIM_ERRORS,
    _PROCESS_KILL_TIMEOUT,
    _PROCESS_TERM_TIMEOUT,
    _PTY_READ_SIZE,
    _READER_PAUSE_TIMEOUT,
    _RPC_POLL_TIMEOUT,
    _SOCKET_CONNECT_TIMEOUT,
    get_session,
)


# ---------------------------------------------------------------------------
# SGR colon-subparam fix
# ---------------------------------------------------------------------------

# Nvim with termguicolors emits colon-separated SGR subparameters
# (e.g. \e[38:2:R:G:Bm) which pyte doesn't parse. Convert to semicolons.
_SGR_COLON_RE = re.compile(
    rb"\x1b\["           # CSI
    rb"("
    rb"[0-9:;]*"         # params (may contain colons)
    rb")"
    rb"m"                # SGR terminator
)


def _fix_sgr_colons(data: bytes) -> bytes:
    """Replace colon subparam separators with semicolons in SGR sequences."""
    return _SGR_COLON_RE.sub(
        lambda m: b"\x1b[" + m.group(1).replace(b":", b";") + b"m",
        data,
    )


def _feed_pyte(s: NvimSession, data: bytes) -> None:
    """Fix SGR colons in raw PTY data and feed it into the pyte stream."""
    data = _fix_sgr_colons(data)
    with s.pty_lock:
        if s.pyte_stream is not None:
            s.pyte_stream.feed(data.decode("utf-8", errors="replace"))


# ---------------------------------------------------------------------------
# PTY helpers
# ---------------------------------------------------------------------------


def _pty_reader_thread(s: NvimSession) -> None:
    """Background thread: drain PTY output into pyte screen.

    Pauses when ``s.pty_reader_active`` is cleared.  Sets
    ``s.pty_reader_paused`` while idle so ``_drain_pty`` can wait for a
    guaranteed handoff instead of sleeping a fixed interval.
    """
    while s.pty_master_fd is not None:
        s.pty_reader_paused.set()          # signal: not touching the fd
        if not s.pty_reader_active.wait(timeout=0.1):
            continue
        s.pty_reader_paused.clear()        # signal: about to use the fd
        try:
            rlist, _, _ = select.select([s.pty_master_fd], [], [], 0.1)
            if rlist:
                data = os.read(s.pty_master_fd, _PTY_READ_SIZE)
                if not data:
                    break
                _feed_pyte(s, data)
        except (OSError, ValueError):
            break
    s.pty_reader_paused.set()              # exiting — mark as idle


def _drain_pty(s: NvimSession, quiet_ms: int = _DRAIN_QUIET_MS) -> None:
    """Block until PTY output goes quiet, indicating nvim finished rendering.

    Pauses the background reader thread and waits for its acknowledgment
    before reading, guaranteeing this function is the sole fd consumer.

    The reader remains paused on return.  Prefer ``_with_drained_pty`` which
    automatically resumes the reader via a context manager.
    """
    if s.pty_master_fd is None:
        return
    s.pty_reader_active.clear()
    # Wait for the reader to finish any in-progress select/read cycle
    if not s.pty_reader_paused.wait(timeout=_READER_PAUSE_TIMEOUT):
        s.pty_reader_active.set()
        return
    deadline = time.time() + _DRAIN_HARD_TIMEOUT
    while time.time() < deadline:
        rlist, _, _ = select.select([s.pty_master_fd], [], [], quiet_ms / 1000.0)
        if not rlist:
            return  # quiet for quiet_ms — done
        try:
            data = os.read(s.pty_master_fd, _PTY_READ_SIZE)
        except OSError:
            return
        if not data:
            return
        _feed_pyte(s, data)


@contextlib.contextmanager
def _with_drained_pty(s: NvimSession, quiet_ms: int = _DRAIN_QUIET_MS):
    """Drain PTY and yield with reader paused. Resumes reader on exit."""
    _drain_pty(s, quiet_ms=quiet_ms)
    try:
        yield
    finally:
        s.pty_reader_active.set()


def _pty_send_raw(s: NvimSession, data: bytes) -> bool:
    """Write raw bytes directly to the PTY master fd, bypassing RPC.

    Returns True if the write succeeded, False otherwise.
    """
    if s.pty_master_fd is None:
        return False
    try:
        os.write(s.pty_master_fd, data)
        return True
    except OSError:
        return False


def _resize_pty(s: NvimSession, rows: int, cols: int) -> None:
    """Resize PTY and pyte screen. Must be called inside _with_drained_pty.

    Sets the PTY window size via ioctl and resizes the pyte virtual terminal.
    Does NOT send RPC commands — the caller must notify nvim separately
    (after the PTY reader is resumed) to avoid filling the PTY OS buffer
    while the reader is paused.
    """
    if s.pty_master_fd is not None:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(s.pty_master_fd, termios.TIOCSWINSZ, winsize)
    if s.pyte_screen is not None:
        s.pyte_screen.resize(rows, cols)


def _wait_for_socket(path: str, timeout: float = _SOCKET_CONNECT_TIMEOUT) -> NvimRPC | None:
    """Poll until nvim's listen socket accepts connections."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            try:
                return NvimRPC(path)
            except (OSError, ConnectionRefusedError):
                pass
        time.sleep(0.1)
    return None


# ---------------------------------------------------------------------------
# PTY startup helpers
# ---------------------------------------------------------------------------


def _create_pty(rows: int, cols: int) -> tuple[int, int]:
    """Create a PTY pair with the given terminal size. Returns (master, slave)."""
    master_fd, slave_fd = pty.openpty()
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
    return master_fd, slave_fd


def _spawn_nvim(
    slave_fd: int, socket_path: str, cmd_extra: list[str] | None,
    clean: bool, env: dict[str, str],
) -> subprocess.Popen:
    """Spawn nvim attached to the given slave PTY fd."""
    cmd = ["nvim", "--listen", socket_path, "--cmd", "set nomore"]
    if clean:
        cmd.append("--clean")
    if cmd_extra:
        cmd.extend(cmd_extra)
    env["TERM"] = "xterm-256color"
    return subprocess.Popen(
        cmd,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=env,
        process_group=0,
    )


def _init_pyte(s: NvimSession, cols: int, rows: int) -> None:
    """Initialize pyte screen/stream and start the reader thread."""
    s.pyte_screen = pyte.Screen(cols, rows)
    s.pyte_stream = pyte.Stream(s.pyte_screen)
    s.pty_reader_paused.set()
    s.pty_reader_active.set()
    s.pty_reader_ref = threading.Thread(
        target=_pty_reader_thread, args=(s,), daemon=True
    )
    s.pty_reader_ref.start()


def _dismiss_press_enter(s: NvimSession) -> None:
    """Adaptively dismiss 'Press ENTER' prompts after startup.

    Polls RPC with a short timeout until nvim responds, sending \\r through
    the PTY only when nvim appears blocked.  Stops as soon as RPC responds
    successfully.
    """
    if s.pty_master_fd is None or s.nvim is None:
        return
    # Use a short socket timeout for fast polling during startup
    if isinstance(s.nvim, NvimRPC):
        s.nvim.set_timeout(_RPC_POLL_TIMEOUT)
    deadline = time.time() + _ADAPTIVE_STARTUP_TIMEOUT
    while time.time() < deadline:
        try:
            s.nvim.eval("1")
            break  # RPC responsive — no prompt blocking
        except _NVIM_ERRORS:
            # nvim is blocked on wait_return — send \r to dismiss
            try:
                os.write(s.pty_master_fd, b"\r")
            except OSError:
                break
            time.sleep(_ADAPTIVE_STARTUP_POLL_INTERVAL)
    # Restore normal timeout
    if isinstance(s.nvim, NvimRPC):
        s.nvim.set_timeout(_SOCKET_CONNECT_TIMEOUT)


# ---------------------------------------------------------------------------
# Teardown and cleanup
# ---------------------------------------------------------------------------


def _teardown(s: NvimSession, *, kill_proc: bool = True) -> None:
    """Release PTY, process, and socket resources."""
    s.pty_reader_active.clear()

    if s.nvim is not None and isinstance(s.nvim, NvimRPC):
        s.nvim.close()

    if s.pty_master_fd is not None:
        try:
            os.close(s.pty_master_fd)
        except OSError:
            pass
        s.pty_master_fd = None

    if s.pty_reader_ref is not None:
        s.pty_reader_ref.join(timeout=1)
        s.pty_reader_ref = None

    if kill_proc and s.proc is not None:
        try:
            os.killpg(s.proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            s.proc.wait(timeout=_PROCESS_TERM_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(s.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                s.proc.wait(timeout=_PROCESS_KILL_TIMEOUT)
            except subprocess.TimeoutExpired:
                pass
        s.proc = None

    if s.socket_path and os.path.exists(s.socket_path):
        try:
            os.unlink(s.socket_path)
        except OSError:
            pass
        s.socket_path = None

    if s.socket_dir and os.path.isdir(s.socket_dir):
        try:
            os.rmdir(s.socket_dir)
        except OSError:
            pass
        s.socket_dir = None

    s.pyte_screen = None
    s.pyte_stream = None


def _cleanup() -> None:
    """Best-effort cleanup on interpreter exit."""
    s = get_session()
    if s is None:
        return
    if s.terminal is not None:
        from nvim_mcp.terminal import _teardown_terminal
        _teardown_terminal(s)
    if s.proc is not None:
        try:
            os.killpg(s.proc.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
    elif s.pid is not None:
        try:
            os.kill(s.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    _teardown(s, kill_proc=False)


atexit.register(_cleanup)
