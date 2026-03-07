"""Playwright-style MCP server for Neovim.

Spawns an embedded Neovim process and exposes tools for lifecycle management,
interaction (commands, Lua, keystrokes), and state inspection.
"""

from __future__ import annotations

import atexit
import contextlib
import fcntl
import functools
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import tempfile
import termios
import threading
import time
import unicodedata
from dataclasses import dataclass, field

import socket as socket_mod

import msgpack
import pynvim
import pyte
from mcp.server.fastmcp import FastMCP
from PIL import Image, ImageDraw, ImageFont

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
_MAX_TERMINAL_SIZE = 500

_VALID_SEVERITIES = {"ERROR", "WARN", "INFO", "HINT"}


# ---------------------------------------------------------------------------
# Lightweight msgpack-rpc client for Neovim over Unix socket
# ---------------------------------------------------------------------------


class NvimRPCError(Exception):
    """Error returned by Neovim RPC."""


_NVIM_ERRORS = (pynvim.NvimError, NvimRPCError, RuntimeError, EOFError, OSError)


class NvimRPC:
    """Thin msgpack-rpc client over a Unix domain socket.

    Replaces pynvim for socket connections to avoid asyncio event-loop
    conflicts with the PTY reader thread.
    """

    def __init__(self, path: str, timeout: float = _SOCKET_CONNECT_TIMEOUT):
        self._sock = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        self._sock.settimeout(timeout)
        self._sock.connect(path)
        self._unpacker = msgpack.Unpacker(raw=False)
        self._msgid = 0
        self._lock = threading.Lock()

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def set_timeout(self, timeout: float) -> None:
        """Set the socket timeout for RPC calls."""
        self._sock.settimeout(timeout)

    def _call(self, method: str, args: list) -> object:
        with self._lock:
            self._msgid += 1
            msgid = self._msgid
            req = msgpack.packb([0, msgid, method, args])
            self._sock.sendall(req)
            while True:
                data = self._sock.recv(_PTY_READ_SIZE)
                if not data:
                    raise EOFError("Neovim socket closed")
                self._unpacker.feed(data)
                for msg in self._unpacker:
                    if not isinstance(msg, (list, tuple)) or len(msg) < 4:
                        continue
                    mtype, mid, err, result = msg[0], msg[1], msg[2], msg[3]
                    if mtype == 1 and mid == msgid:
                        if err:
                            raise NvimRPCError(err if isinstance(err, str) else str(err))
                        return result
                    # notifications (type 2) or mismatched responses — skip

    # -- Public API matching pynvim's interface used in this file --

    def eval(self, expr: str) -> object:
        return self._call("nvim_eval", [expr])

    def command(self, cmd: str) -> None:
        self._call("nvim_command", [cmd])

    def command_output(self, cmd: str) -> str:
        result = self._call("nvim_exec2", [cmd, {"output": True}])
        if isinstance(result, dict):
            return result.get("output", "")
        return str(result) if result else ""

    def exec_lua(self, code: str, *args: object) -> object:
        return self._call("nvim_exec_lua", [code, list(args)])

    def feedkeys(self, keys: str, mode: str, escape_ks: bool) -> None:
        self._call("nvim_feedkeys", [keys, mode, escape_ks])

    def replace_termcodes(
        self, s: str, from_part: bool, do_lt: bool, special: bool
    ) -> str:
        result = self._call("nvim_replace_termcodes", [s, from_part, do_lt, special])
        return result if isinstance(result, str) else str(result)

    @property
    def api(self) -> "_NvimAPI":
        return _NvimAPI(self)

    @property
    def current(self) -> "_NvimCurrent":
        return _NvimCurrent(self)


class _NvimAPI:
    def __init__(self, rpc: NvimRPC):
        self._rpc = rpc

    def buf_get_name(self, buf: int) -> str:
        result = self._rpc._call("nvim_buf_get_name", [buf])
        return result if isinstance(result, str) else str(result)

    def buf_get_lines(
        self, buf: int, start: int, end: int, strict: bool
    ) -> list[str]:
        return self._rpc._call("nvim_buf_get_lines", [buf, start, end, strict])


class _NvimCurrent:
    def __init__(self, rpc: NvimRPC):
        self._rpc = rpc

    @property
    def buffer(self) -> "_NvimCurrentBuffer":
        return _NvimCurrentBuffer(self._rpc)


class _NvimCurrentBuffer:
    def __init__(self, rpc: NvimRPC):
        self._rpc = rpc

    @property
    def number(self) -> int:
        return self._rpc._call("nvim_get_current_buf", [])


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


_session: NvimSession | None = None


# ---------------------------------------------------------------------------
# Color mapping for PNG rendering
# ---------------------------------------------------------------------------

_DEFAULT_FG = (204, 204, 204)
_DEFAULT_BG = (30, 30, 30)

_ANSI_COLOR_NAMES = (
    "black", "red", "green", "brown", "blue", "magenta", "cyan", "white",
    "brightblack", "brightred", "brightgreen", "brightbrown",
    "brightblue", "brightmagenta", "brightcyan", "brightwhite",
)

_ANSI_COLORS_BY_INDEX: tuple[tuple[int, int, int], ...] = (
    (0, 0, 0),          # 0  black
    (205, 49, 49),       # 1  red
    (13, 188, 121),      # 2  green
    (229, 229, 16),      # 3  brown/yellow
    (36, 114, 200),      # 4  blue
    (188, 63, 188),      # 5  magenta
    (17, 168, 205),      # 6  cyan
    (204, 204, 204),     # 7  white
    (118, 118, 118),     # 8  bright black
    (241, 76, 76),       # 9  bright red
    (35, 209, 139),      # 10 bright green
    (245, 245, 67),      # 11 bright brown/yellow
    (59, 142, 234),      # 12 bright blue
    (214, 112, 214),     # 13 bright magenta
    (41, 184, 219),      # 14 bright cyan
    (242, 242, 242),     # 15 bright white
)

_ANSI_COLORS = dict(zip(_ANSI_COLOR_NAMES, _ANSI_COLORS_BY_INDEX))


def _resolve_color(color: str, is_fg: bool) -> tuple[int, int, int]:
    """Convert a pyte color attribute to an RGB tuple."""
    if not color or color == "default":
        return _DEFAULT_FG if is_fg else _DEFAULT_BG
    # Named color
    if color in _ANSI_COLORS:
        return _ANSI_COLORS[color]
    # 256-color index (pyte stores as string like "196")
    if color.isdigit():
        idx = int(color)
        if idx < 16:
            return _ANSI_COLORS_BY_INDEX[idx]
        if idx < 232:
            # 6x6x6 color cube
            idx -= 16
            b = (idx % 6) * 51
            idx //= 6
            g = (idx % 6) * 51
            r = (idx // 6) * 51
            return (r, g, b)
        # Grayscale ramp
        v = 8 + (idx - 232) * 10
        return (v, v, v)
    # 24-bit hex (pyte may give "RRGGBB" or "#RRGGBB")
    hex_str = color.lstrip("#")
    if len(hex_str) == 6:
        try:
            r = int(hex_str[0:2], 16)
            g = int(hex_str[2:4], 16)
            b = int(hex_str[4:6], 16)
            return (r, g, b)
        except ValueError:
            pass
    return _DEFAULT_FG if is_fg else _DEFAULT_BG


# ---------------------------------------------------------------------------
# Font loading and PNG rendering
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=4)
def _load_font(size: int = 14) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load a monospace font, trying platform-specific paths."""
    for name in [
        "/System/Library/Fonts/Menlo.ttc",               # macOS
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",  # Debian/Ubuntu
        "/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono.ttf",  # Fedora
        "/usr/share/fonts/TTF/DejaVuSansMono.ttf",       # Arch
        "DejaVuSansMono.ttf",                             # system path
    ]:
        try:
            return ImageFont.truetype(name, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _char_width(ch: str) -> int:
    """Return the display width of a character (1 or 2 for wide/fullwidth)."""
    if len(ch) != 1:
        return 1
    eaw = unicodedata.east_asian_width(ch)
    return 2 if eaw in ("W", "F") else 1


def _render_screen_to_png(screen: pyte.Screen, path: str) -> None:
    """Render a pyte screen to a PNG image."""
    font = _load_font(14)
    bbox = font.getbbox("M")
    char_w = bbox[2] - bbox[0]
    char_h = bbox[3] - bbox[1]
    line_h = char_h + 2
    y_offset = -bbox[1]  # baseline offset

    img = Image.new(
        "RGB",
        (screen.columns * char_w, screen.lines * line_h),
        _DEFAULT_BG,
    )
    draw = ImageDraw.Draw(img)

    for row in range(screen.lines):
        skip_next = False
        for col in range(screen.columns):
            if skip_next:
                skip_next = False
                continue
            char = screen.buffer[row][col]
            fg = _resolve_color(char.fg, is_fg=True)
            bg = _resolve_color(char.bg, is_fg=False)
            if char.reverse:
                fg, bg = bg, fg
            x = col * char_w
            y = row * line_h
            cw = _char_width(char.data) if char.data.strip() else 1
            cell_w = char_w * cw
            draw.rectangle([x, y, x + cell_w, y + line_h], fill=bg)
            if char.data.strip():
                draw.text((x, y + y_offset), char.data, font=font, fill=fg)
                if cw == 2:
                    skip_next = True

    img.save(path, "PNG")


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
# Internal helpers
# ---------------------------------------------------------------------------


def _require_nvim() -> pynvim.Nvim | NvimRPC:
    if _session is None or _session.nvim is None:
        raise RuntimeError("Neovim is not running. Call nvim_start first.")
    return _session.nvim


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
    if _session is None:
        return
    if _session.proc is not None:
        try:
            os.killpg(_session.proc.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
    elif _session.pid is not None:
        try:
            os.kill(_session.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    _teardown(_session, kill_proc=False)


atexit.register(_cleanup)


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
    args: list[str] | None = None,
    rows: int = 24,
    cols: int = 80,
) -> str:
    """Start an embedded Neovim instance.

    Args:
        config: Path to the Neovim config directory (must be named "nvim",
              e.g. ~/.config/nvim). The parent directory is used as XDG_CONFIG_HOME.
        clean: If True, start with --clean (no config/plugins).
        headless: If True, start with --headless --embed (no PTY, no screenshots).
                  If False (default), start with PTY + socket for full screenshot support.
        args: Extra CLI arguments to pass to nvim.
        rows: Terminal rows (only used when headless=False).
        cols: Terminal columns (only used when headless=False).
    """
    global _session

    if _session is not None and _session.nvim is not None:
        return f"Neovim is already running (PID {_session.pid}). Stop it first."

    if not headless and (rows < 1 or cols < 1 or rows > _MAX_TERMINAL_SIZE or cols > _MAX_TERMINAL_SIZE):
        return f"Error: rows and cols must be between 1 and {_MAX_TERMINAL_SIZE}."

    config = os.path.expanduser(config)

    env = os.environ.copy()
    if not clean and os.path.isdir(config):
        env["XDG_CONFIG_HOME"] = os.path.dirname(config)

    if headless:
        s, err = _start_headless(cmd_extra=args, clean=clean, env=env)
    else:
        s, err = _start_pty(cmd_extra=args, clean=clean, env=env, rows=rows, cols=cols)
    if err:
        return err

    try:
        s.pid = s.nvim.eval("getpid()")
    except _NVIM_ERRORS:
        # May be blocked on wait_return from plugin errors — dismiss and retry
        if not headless and s.pty_master_fd is not None:
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

    _session = s

    msg = f"Neovim started (PID {_session.pid}"

    if not clean and os.path.isdir(config):
        if not _wait_for_lazy(_session, timeout=_LAZY_LOAD_TIMEOUT):
            mode_label = "PTY" if not headless else "headless"
            # Restore 'more' before returning on timeout
            if not headless:
                try:
                    _session.nvim.command("set more")
                except Exception:
                    pass
            msg += f", {mode_label}). Warning: lazy.nvim did not finish loading within {_LAZY_LOAD_TIMEOUT}s."
            return msg

    # Restore 'more' option after lazy.nvim finishes (PTY mode only).
    # Kept off during loading so plugin errors don't trigger "-- More --" pager.
    if not headless:
        try:
            _session.nvim.command("set more")
        except Exception:
            pass

    mode_str = "headless" if headless else f"PTY {cols}x{rows}"
    return f"Neovim started (PID {_session.pid}, {mode_str})."


def _wait_for_lazy(s: NvimSession, timeout: int = _LAZY_LOAD_TIMEOUT) -> bool:
    """Poll until lazy.nvim reports all plugins are loaded, or timeout.

    While waiting, sends \\r through the PTY to dismiss any 'Press ENTER'
    prompts that plugins may trigger during loading.
    """
    if s.nvim is None:
        return False
    has_pty = s.pty_master_fd is not None
    if has_pty and isinstance(s.nvim, NvimRPC):
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
            time.sleep(_ADAPTIVE_STARTUP_POLL_INTERVAL)
    finally:
        if has_pty and isinstance(s.nvim, NvimRPC):
            s.nvim.set_timeout(_SOCKET_CONNECT_TIMEOUT)
    return False


@mcp.tool()
def nvim_stop() -> str:
    """Stop the running Neovim instance."""
    global _session

    if _session is None or _session.nvim is None:
        return "Neovim is not running."

    pid = _session.pid
    was_headless = _session.proc is None

    try:
        _session.nvim.command("qall!")
    except _NVIM_ERRORS:
        pass

    _teardown(_session, kill_proc=True)

    # Headless/embed mode: no subprocess, kill by PID
    if was_headless and pid is not None:
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

    _session = None
    return "Neovim stopped."


@mcp.tool()
def nvim_is_running() -> str:
    """Check if a Neovim instance is currently running.

    Probes the connection to detect a crashed Neovim process.
    """
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
def nvim_execute(command: str) -> str:
    """Execute an Ex command in Neovim and return its output.

    Args:
        command: The Ex command to run (without leading colon), e.g. "Lazy health".
    """
    try:
        nvim = _require_nvim()
        output = nvim.command_output(command)
        return output if output else "(no output)"
    except _NVIM_ERRORS as e:
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
    except _NVIM_ERRORS as e:
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
        nvim.command("")
        return f"Sent keys: {keys}"
    except _NVIM_ERRORS as e:
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
    except _NVIM_ERRORS as e:
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
    except _NVIM_ERRORS as e:
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
    except _NVIM_ERRORS as e:
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
    except _NVIM_ERRORS as e:
        return f"Error: {e}"


@mcp.tool()
def nvim_screenshot(output_path: str = "") -> str:
    """Capture a PNG screenshot of the current Neovim terminal display.

    Args:
        output_path: File path for the PNG. If empty, uses a temp file.
    """
    try:
        nvim = _require_nvim()
        if _session.pyte_screen is None:
            return "Error: screenshot not available (nvim started in headless mode)"
        s = _session
        # RPC round-trip to flush pending commands, then drain PTY until quiet.
        nvim.eval("1")
        with _with_drained_pty(s, quiet_ms=_DRAIN_QUIET_MS):
            if not output_path:
                f = tempfile.NamedTemporaryFile(
                    suffix=".png", prefix="nvim-screenshot-", delete=False
                )
                output_path = f.name
                f.close()
            with s.pty_lock:
                _render_screen_to_png(s.pyte_screen, output_path)
        return os.path.abspath(output_path)
    except _NVIM_ERRORS as e:
        return f"Error: {e}"


if __name__ == "__main__":
    mcp.run()
