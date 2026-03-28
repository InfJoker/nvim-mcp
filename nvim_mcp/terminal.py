"""Terminal mode: launch, teardown, screenshot, and window ID discovery."""

from __future__ import annotations

import json
import os
import platform
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
import uuid

from nvim_mcp.rpc import NvimRPC
from nvim_mcp.state import (
    NvimSession,
    _ADAPTIVE_STARTUP_POLL_INTERVAL,
    _ADAPTIVE_STARTUP_TIMEOUT,
    _APPLESCRIPT_FOCUS_DELAY,
    _APPLESCRIPT_LAUNCH_TIMEOUT,
    _BLANK_SCREENSHOT_THRESHOLD,
    _FRONTMOST_APP_TIMEOUT,
    _ITERM2_CLOSE_TIMEOUT,
    _KITTEN_RPC_TIMEOUT,
    _NVIM_ERRORS,
    _OSASCRIPT_FALLBACK_TIMEOUT,
    _PROCESS_KILL_TIMEOUT,
    _PROCESS_TERM_TIMEOUT,
    _RPC_POLL_TIMEOUT,
    _SCREENCAPTURE_TIMEOUT,
    _SOCKET_CONNECT_TIMEOUT,
    _TERMINAL_NAMES,
    _TERMINAL_OWNER_NAMES,
    _TERMINAL_SOCKET_CONNECT_TIMEOUT,
    _WINDOW_ID_POLL_INTERVAL,
    _WINDOW_ID_POLL_TIMEOUT,
)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _check_terminal_available(terminal: str) -> str | None:
    """Validate that the terminal emulator is installed. Returns error or None."""
    terminal = terminal.lower()
    if terminal not in _TERMINAL_NAMES:
        return f"Unknown terminal '{terminal}'. Supported: {', '.join(sorted(_TERMINAL_NAMES))}"

    if platform.system() != "Darwin":
        return "Terminal mode is only supported on macOS."

    if terminal == "iterm2":
        # Check for app bundle
        if not os.path.isdir("/Applications/iTerm.app"):
            return "iTerm2 not found at /Applications/iTerm.app"
        return None

    # kitty and ghostty — check via shutil.which or macOS app bundle path
    if shutil.which(terminal) is None:
        app_paths = {
            "kitty": "/Applications/kitty.app/Contents/MacOS/kitty",
            "ghostty": "/Applications/Ghostty.app/Contents/MacOS/ghostty",
        }
        path = app_paths.get(terminal)
        if path and os.path.isfile(path):
            return None
        return f"'{terminal}' not found in PATH. Install it first."
    return None


# ---------------------------------------------------------------------------
# Window ID discovery
# ---------------------------------------------------------------------------


def _list_window_ids(owner_name: str) -> set[int]:
    """Return the set of CGWindowIDs owned by *owner_name* (no permissions needed).

    Uses kCGWindowListOptionAll because windows launched in the background
    (via ``open -na`` / ``open -gna``) may not appear in the on-screen-only list.
    Excludes zero-size helper windows (e.g. kitty's status bar windows).
    """
    ids: set[int] = set()
    try:
        from Quartz import (  # type: ignore[import-untyped]
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowListOptionAll,
        )
        windows = CGWindowListCopyWindowInfo(
            kCGWindowListOptionAll, kCGNullWindowID
        )
        if windows:
            for win in windows:
                if win.get("kCGWindowOwnerName") == owner_name:
                    wid = win.get("kCGWindowNumber")
                    if not wid:
                        continue
                    # Skip zero-size helper windows
                    bounds = win.get("kCGWindowBounds")
                    if bounds:
                        w = bounds.get("Width", 0)
                        h = bounds.get("Height", 0)
                        if w == 0 and h == 0:
                            continue
                    ids.add(int(wid))
    except ImportError:
        pass
    return ids


def _find_new_window_id(
    owner_name: str, before: set[int], title: str | None = None,
) -> int | None:
    """Poll for a new window owned by *owner_name* that wasn't in *before*.

    If *title* is given, prefer the window whose kCGWindowName matches
    (avoids picking kitty helper windows). Requires Screen Recording for
    title matching; falls back to highest ID without it.

    Uses a single Quartz query per poll iteration (title check is done inline
    on the same window list used for ID diffing).
    """
    try:
        from Quartz import (  # type: ignore[import-untyped]
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowListOptionAll,
        )
    except ImportError:
        return None

    deadline = time.time() + _WINDOW_ID_POLL_TIMEOUT
    fallback_id: int | None = None
    while time.time() < deadline:
        windows = CGWindowListCopyWindowInfo(kCGWindowListOptionAll, kCGNullWindowID)
        new_ids: set[int] = set()
        titled_id: int | None = None
        if windows:
            for win in windows:
                if win.get("kCGWindowOwnerName") != owner_name:
                    continue
                wid = win.get("kCGWindowNumber")
                if not wid:
                    continue
                wid_int = int(wid)
                if wid_int in before:
                    continue
                # Skip zero-size helper windows
                bounds = win.get("kCGWindowBounds")
                if bounds:
                    w = bounds.get("Width", 0)
                    h = bounds.get("Height", 0)
                    if w == 0 and h == 0:
                        continue
                new_ids.add(wid_int)
                # Title match (needs Screen Recording for kCGWindowName)
                if title and titled_id is None:
                    name = win.get("kCGWindowName")
                    if name and title in str(name):
                        titled_id = wid_int
        if titled_id is not None:
            return titled_id
        if new_ids:
            # Remember fallback but keep polling for title match — the
            # terminal may not have set the window title yet.
            fallback_id = max(new_ids)
            if not title:
                return fallback_id
        time.sleep(_WINDOW_ID_POLL_INTERVAL)
    return fallback_id


def _find_window_id(pid: int | None = None, title: str | None = None) -> int | None:
    """Find a macOS CGWindowID by PID or window title.

    Tries Quartz framework first, falls back to osascript.
    Returns None on Linux or if the window is not found.
    """
    if platform.system() != "Darwin":
        return None

    deadline = time.time() + _WINDOW_ID_POLL_TIMEOUT
    while time.time() < deadline:
        wid = _find_window_id_once(pid=pid, title=title)
        if wid is not None:
            return wid
        time.sleep(_WINDOW_ID_POLL_INTERVAL)
    return None


def _find_window_id_once(pid: int | None = None, title: str | None = None) -> int | None:
    """Single attempt to find window ID via Quartz or osascript fallback."""
    # Try Quartz first
    try:
        from Quartz import (  # type: ignore[import-untyped]
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowListOptionOnScreenOnly,
        )
        windows = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly, kCGNullWindowID
        )
        if windows:
            for win in windows:
                if pid is not None and win.get("kCGWindowOwnerPID") == pid:
                    wid = win.get("kCGWindowNumber")
                    if wid:
                        return int(wid)
                if title is not None and title in str(win.get("kCGWindowName", "")):
                    wid = win.get("kCGWindowNumber")
                    if wid:
                        return int(wid)
    except ImportError:
        pass

    # Fallback: osascript
    if pid is not None:
        try:
            result = subprocess.run(
                [
                    "osascript", "-e",
                    f'tell application "System Events" to get id of first window of '
                    f'(first process whose unix id is {pid})',
                ],
                capture_output=True, text=True, timeout=_OSASCRIPT_FALLBACK_TIMEOUT,
            )
            if result.returncode == 0 and result.stdout.strip().isdigit():
                return int(result.stdout.strip())
        except (subprocess.TimeoutExpired, OSError):
            pass
    if title is not None:
        try:
            result = subprocess.run(
                [
                    "osascript", "-e",
                    f'tell application "System Events"\n'
                    f'  repeat with proc in every process\n'
                    f'    repeat with w in windows of proc\n'
                    f'      if name of w contains "{title}" then return id of w\n'
                    f'    end repeat\n'
                    f'  end repeat\n'
                    f'end tell',
                ],
                capture_output=True, text=True, timeout=_OSASCRIPT_FALLBACK_TIMEOUT,
            )
            if result.returncode == 0 and result.stdout.strip().isdigit():
                return int(result.stdout.strip())
        except (subprocess.TimeoutExpired, OSError):
            pass
    return None


def _get_window_owner_pid(window_id: int) -> int | None:
    """Return the PID of the process that owns a CGWindowID, or None."""
    try:
        from Quartz import (  # type: ignore[import-untyped]
            CGWindowListCopyWindowInfo,
            kCGWindowListOptionIncludingWindow,
        )
        windows = CGWindowListCopyWindowInfo(
            kCGWindowListOptionIncludingWindow, window_id
        )
        if windows:
            pid = windows[0].get("kCGWindowOwnerPID")
            return int(pid) if pid else None
    except ImportError:
        pass
    return None


def _find_kitty_window_id_once(kitty_socket: str) -> int | None:
    """Single attempt to get CGWindowID from kitty via remote control."""
    try:
        kitten_bin = shutil.which("kitten") or "kitten"
        r = subprocess.run(
            [kitten_bin, "@", "--to", f"unix:{kitty_socket}", "ls"],
            capture_output=True, text=True, timeout=_KITTEN_RPC_TIMEOUT,
        )
        if r.returncode == 0:
            data = json.loads(r.stdout)
            for os_win in data:
                pwid = os_win.get("platform_window_id")
                if pwid:
                    return int(pwid)
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError, ValueError):
        pass
    return None


def _find_kitty_window_id(kitty_socket: str) -> int | None:
    """Poll kitty remote control for CGWindowID (no macOS permissions needed)."""
    deadline = time.time() + _WINDOW_ID_POLL_TIMEOUT
    while time.time() < deadline:
        wid = _find_kitty_window_id_once(kitty_socket)
        if wid is not None:
            return wid
        time.sleep(_WINDOW_ID_POLL_INTERVAL)
    return None


# ---------------------------------------------------------------------------
# Startup helpers
# ---------------------------------------------------------------------------


def _dismiss_press_enter_rpc(s: NvimSession) -> None:
    """Dismiss 'Press ENTER' prompts via nvim_input() RPC (no PTY needed)."""
    if s.nvim is None or not isinstance(s.nvim, NvimRPC):
        return
    s.nvim.set_timeout(_RPC_POLL_TIMEOUT)
    deadline = time.time() + _ADAPTIVE_STARTUP_TIMEOUT
    while time.time() < deadline:
        try:
            s.nvim.eval("1")
            break
        except _NVIM_ERRORS:
            try:
                s.nvim.input("\r")
            except _NVIM_ERRORS:
                pass
            time.sleep(_ADAPTIVE_STARTUP_POLL_INTERVAL)
    s.nvim.set_timeout(_SOCKET_CONNECT_TIMEOUT)


def _resolve_app_bundle(binary: str, default_app: str) -> str | None:
    """Resolve macOS .app bundle path from a binary path or default location.

    Returns the .app path, or None if not found.
    """
    real = os.path.realpath(binary)
    if ".app/" in real:
        return real[:real.index(".app/") + 4]
    if os.path.isdir(default_app):
        return default_app
    return None


def _capture_frontmost_app() -> str:
    """Return the name of the current frontmost macOS app, or empty string."""
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first process'
             ' whose frontmost is true'],
            capture_output=True, text=True, timeout=_FRONTMOST_APP_TIMEOUT,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return ""


def _restore_focus(terminal_process_name: str, prev_app: str) -> None:
    """Fire-and-forget: restore focus if the terminal stole it."""
    if not prev_app:
        return
    try:
        subprocess.Popen(
            ["osascript", "-e",
             f'delay {_APPLESCRIPT_FOCUS_DELAY}\n'
             f'tell application "System Events"\n'
             f'  try\n'
             f'    if frontmost of process "{terminal_process_name}" is true then\n'
             f'      set frontmost of process "{prev_app}" to true\n'
             f'    end if\n'
             f'  end try\n'
             f'end tell'],
        )
    except OSError:
        pass


def _env_wrapped_cmd(env: dict[str, str], cmd: list[str]) -> list[str]:
    """Wrap command with /usr/bin/env for vars that differ from os.environ."""
    assignments = [f"{k}={v}" for k, v in env.items() if os.environ.get(k) != v]
    if assignments:
        return ["/usr/bin/env"] + assignments + cmd
    return cmd


def _start_terminal(
    terminal: str,
    cmd_extra: list[str] | None,
    clean: bool,
    env: dict[str, str],
    rows: int,
    cols: int,
) -> tuple[NvimSession, str | None]:
    """Start nvim inside a real terminal emulator. Returns (session, error_or_None)."""
    from nvim_mcp.pty import _wait_for_socket

    s = NvimSession()
    s.terminal = terminal.lower()
    s.terminal_title = f"nvim-mcp-{uuid.uuid4().hex[:12]}"

    s.socket_dir = tempfile.mkdtemp(prefix=f"nvim-mcp-{os.getpid()}-")
    s.socket_path = os.path.join(s.socket_dir, "nvim.sock")

    # Use absolute path to nvim — terminal shells may not have it in PATH
    nvim_bin = shutil.which("nvim") or "nvim"
    nvim_cmd_parts = [nvim_bin, "--listen", s.socket_path, "--cmd", "set nomore"]
    if clean:
        nvim_cmd_parts.append("--clean")
    if cmd_extra:
        nvim_cmd_parts.extend(cmd_extra)

    # Snapshot existing windows for diff-based window ID discovery (all terminals)
    owner = _TERMINAL_OWNER_NAMES.get(s.terminal)
    if owner and platform.system() == "Darwin":
        s.windows_before = _list_window_ids(owner)

    try:
        if s.terminal == "kitty":
            s.kitty_socket = os.path.join(s.socket_dir, "kitty.sock")
            kitty_bin = shutil.which("kitty") or "/Applications/kitty.app/Contents/MacOS/kitty"
            kitty_app = _resolve_app_bundle(kitty_bin, "/Applications/kitty.app")
            if kitty_app is None:
                return s, "Cannot locate kitty.app bundle (needed for background launch)"
            exe_cmd = _env_wrapped_cmd(env, nvim_cmd_parts)
            kitty_args = [
                "--single-instance=no",
                f"--title={s.terminal_title}",
                f"--listen-on=unix:{s.kitty_socket}",
                "-o", "allow_remote_control=yes",
                "-o", "close_on_child_death=yes",
                "-o", "macos_quit_when_last_window_closed=yes",
                "-o", "remember_window_size=no",
                "-o", f"initial_window_width={cols}c",
                "-o", f"initial_window_height={rows}c",
                "-e",
            ] + exe_cmd
            prev_app = _capture_frontmost_app()
            subprocess.Popen(["open", "-na", kitty_app, "--args"] + kitty_args)
            _restore_focus("kitty", prev_app)
            # No terminal_proc — kitty spawned by launchd

        elif s.terminal == "ghostty":
            ghostty_bin = shutil.which("ghostty") or "/Applications/Ghostty.app/Contents/MacOS/ghostty"
            ghostty_app = _resolve_app_bundle(ghostty_bin, "/Applications/Ghostty.app")
            if ghostty_app is None:
                return s, "Cannot locate Ghostty.app bundle (needed for background launch)"
            # Use --command= (config key) instead of -e to avoid Ghostty's v1.2.0+
            # "Allow Ghostty to Execute" security prompt (GHSA-q9fg-cpmh-c78x).
            # Config-based commands are trusted; -e commands always prompt.
            exe_cmd = _env_wrapped_cmd(env, nvim_cmd_parts)
            ghostty_args = [
                f"--title={s.terminal_title}",
                f"--command={shlex.join(exe_cmd)}",
                "--quit-after-last-window-closed=true",
                f"--window-width={cols}",
                f"--window-height={rows}",
            ]
            # Use `open -na` (not -gna): the -g flag prevents Ghostty's
            # window from appearing until clicked in the dock.
            prev_app = _capture_frontmost_app()
            subprocess.Popen(["open", "-na", ghostty_app, "--args"] + ghostty_args)
            _restore_focus("Ghostty", prev_app)
            # No terminal_proc — ghostty spawned by launchd via `open -na`

        elif s.terminal == "iterm2":
            exe_cmd = _env_wrapped_cmd(env, nvim_cmd_parts)
            nvim_cmd_str = shlex.join(exe_cmd)
            # Escape backslashes and double quotes for AppleScript string
            nvim_cmd_str = nvim_cmd_str.replace("\\", "\\\\").replace('"', '\\"')
            applescript = (
                f'tell application "System Events"\n'
                f'  set prevProc to name of first process whose frontmost is true\n'
                f'end tell\n'
                f'tell application "iTerm2"\n'
                f'  set newWindow to (create window with default profile'
                f' command "{nvim_cmd_str}")\n'
                # Set bounds immediately to prevent macOS auto-maximize.
                # Also exits native fullscreen if the window was pulled
                # into a fullscreen Space (works when in the same script).
                f'  set bounds of newWindow to {{100, 100, 900, 600}}\n'
                f'  set windowId to id of newWindow\n'
                f'  tell current session of newWindow\n'
                f'    set name to "{s.terminal_title}"\n'
                f'    set refCols to columns\n'
                f'    set refRows to rows\n'
                f'  end tell\n'
                f'  set cellW to 800.0 / refCols\n'
                f'  set cellH to 500.0 / refRows\n'
                f'  set newW to round ({cols} * cellW)\n'
                f'  set newH to round ({rows} * cellH)\n'
                f'  set bounds of newWindow'
                f' to {{100, 100, 100 + newW, 100 + newH}}\n'
                f'end tell\n'
                f'try\n'
                f'  delay {_APPLESCRIPT_FOCUS_DELAY}\n'
                f'  tell application "System Events"\n'
                f'    if frontmost of process "iTerm2" is true then\n'
                f'      set frontmost of process prevProc to true\n'
                f'    end if\n'
                f'  end tell\n'
                f'end try\n'
                f'return windowId'
            )
            result = subprocess.run(
                ["osascript", "-e", applescript],
                capture_output=True, text=True, timeout=_APPLESCRIPT_LAUNCH_TIMEOUT, env=env,
            )
            if result.returncode != 0:
                return s, f"Failed to launch iTerm2: {result.stderr.strip()}"
            raw_id = result.stdout.strip()
            if raw_id and raw_id.isdigit():
                s.iterm2_window_id = int(raw_id)
            # iTerm2 launch is async — no terminal_proc to track

    except Exception as e:
        return s, f"Failed to launch {terminal}: {e}"

    # Connect to nvim's RPC socket
    conn = _wait_for_socket(s.socket_path, timeout=_TERMINAL_SOCKET_CONNECT_TIMEOUT)
    if conn is None:
        _teardown_terminal(s)
        return s, (
            f"Failed to connect to Neovim socket (timeout)."
            f" Is {terminal} running? A macOS permission dialog may be blocking the launch."
        )

    s.nvim = conn

    _dismiss_press_enter_rpc(s)

    # Find window ID for screenshots (macOS only).
    # Three-tier cascade: terminal-specific primary → diff-based → title-based.
    # See _TERMINAL_OWNER_NAMES for Quartz kCGWindowOwnerName mapping.
    if platform.system() == "Darwin":
        if s.terminal == "kitty" and s.kitty_socket:
            s.terminal_window_id = _find_kitty_window_id(s.kitty_socket)
        elif s.terminal == "iterm2" and s.iterm2_window_id is not None:
            s.terminal_window_id = s.iterm2_window_id
        # Diff-based fallback: works for all terminals without any permissions
        if s.terminal_window_id is None and s.windows_before is not None:
            if owner:
                s.terminal_window_id = _find_new_window_id(
                    owner, s.windows_before, title=s.terminal_title,
                )
        # Title-based fallback (needs Screen Recording for kCGWindowName)
        if s.terminal_window_id is None:
            s.terminal_window_id = _find_window_id(title=s.terminal_title)

        # Discover owner PID for SIGTERM teardown (kitty, ghostty only).
        # iTerm2 is a single process shared across all windows — never SIGTERM it.
        if s.terminal_window_id is not None and s.terminal_pid is None and s.terminal != "iterm2":
            s.terminal_pid = _get_window_owner_pid(s.terminal_window_id)

    return s, None


# ---------------------------------------------------------------------------
# Terminal resize
# ---------------------------------------------------------------------------


def _resize_terminal(s: NvimSession, rows: int, cols: int) -> str | None:
    """Resize the terminal window. Returns error string or None on success."""
    if s.terminal == "kitty" and s.kitty_socket:
        try:
            kitten_bin = shutil.which("kitten") or "kitten"
            r = subprocess.run(
                [
                    kitten_bin, "@", "--to", f"unix:{s.kitty_socket}",
                    "resize-os-window",
                    "--width", str(cols), "--height", str(rows),
                    "--unit", "cells",
                ],
                capture_output=True, text=True, timeout=_KITTEN_RPC_TIMEOUT,
            )
            if r.returncode != 0:
                return f"kitten resize failed: {r.stderr.strip()}"
        except subprocess.TimeoutExpired:
            return "kitten resize timed out"
        except OSError as e:
            return f"kitten resize failed: {e}"

    elif s.terminal == "ghostty":
        # Ghostty has no remote control API. Resize nvim's view of the
        # terminal dimensions — the terminal window itself won't resize,
        # but nvim will render within the requested cols×rows area.
        try:
            from nvim_mcp.rpc import NvimRPC as _NvimRPC
            if isinstance(s.nvim, _NvimRPC):
                s.nvim.command(f"set columns={cols} lines={rows}")
            else:
                return "Cannot resize ghostty: no RPC connection"
        except _NVIM_ERRORS:
            return "Failed to resize via nvim RPC"

    elif s.terminal == "iterm2":
        # Resizing iTerm2 via AppleScript `set bounds` breaks the nvim RPC
        # socket permanently (nvim's server becomes unresponsive after the
        # PTY resize triggered by the bounds change).
        return "Resize not supported for iTerm2 (breaks RPC connection). Stop and restart with desired size."

    else:
        return f"Resize not supported for terminal: {s.terminal}"

    return None


# ---------------------------------------------------------------------------
# Terminal teardown
# ---------------------------------------------------------------------------


def _teardown_terminal(s: NvimSession) -> None:
    """Terminate the terminal emulator process."""
    if s.terminal_proc is not None:
        try:
            os.killpg(s.terminal_proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            s.terminal_proc.wait(timeout=_PROCESS_TERM_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(s.terminal_proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                s.terminal_proc.wait(timeout=_PROCESS_KILL_TIMEOUT)
            except subprocess.TimeoutExpired:
                pass
        s.terminal_proc = None

    # Kitty-specific: try graceful quit via remote control socket first
    if s.terminal == "kitty" and s.kitty_socket:
        try:
            kitten_bin = shutil.which("kitten") or "kitten"
            r = subprocess.run(
                [kitten_bin, "@", "--to", f"unix:{s.kitty_socket}", "quit"],
                capture_output=True, timeout=_KITTEN_RPC_TIMEOUT,
            )
            if r.returncode == 0:
                s.terminal_pid = None  # already quit, skip SIGTERM
        except (subprocess.TimeoutExpired, OSError):
            pass

    # Shared SIGTERM fallback for kitty and ghostty (NOT iTerm2 — it's a
    # single process shared across all windows; SIGTERM would kill every window)
    if s.terminal_pid is not None and s.terminal_proc is None and s.terminal != "iterm2":
        try:
            os.kill(s.terminal_pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    if s.terminal == "iterm2":
        # Best-effort close the iTerm2 window by ID (captured at launch)
        try:
            if s.iterm2_window_id is not None:
                script = (
                    f'tell application "iTerm2"\n'
                    f'  close window id {s.iterm2_window_id}\n'
                    f'end tell'
                )
            else:
                script = (
                    f'tell application "iTerm2"\n'
                    f'  repeat with w in windows\n'
                    f'    repeat with t in tabs of w\n'
                    f'      repeat with sess in sessions of t\n'
                    f'        if name of sess is "{s.terminal_title}" then\n'
                    f'          close w\n'
                    f'          return\n'
                    f'        end if\n'
                    f'      end repeat\n'
                    f'    end repeat\n'
                    f'  end repeat\n'
                    f'end tell'
                )
            subprocess.run(
                [
                    "osascript", "-e", script,
                ],
                capture_output=True, timeout=_ITERM2_CLOSE_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass

    # Clean up kitty socket
    if s.kitty_socket and os.path.exists(s.kitty_socket):
        try:
            os.unlink(s.kitty_socket)
        except OSError:
            pass
        s.kitty_socket = None


# ---------------------------------------------------------------------------
# Terminal screenshots
# ---------------------------------------------------------------------------


def _capture_window(window_id: int, path: str) -> str | None:
    """Capture a window screenshot. Returns error string or None on success.

    macOS: uses screencapture -l. Linux: not yet supported.
    """
    if platform.system() != "Darwin":
        return "Terminal screenshots are not yet supported on Linux."
    try:
        result = subprocess.run(
            ["screencapture", "-l", str(window_id), path],
            capture_output=True, text=True, timeout=_SCREENCAPTURE_TIMEOUT,
        )
        if result.returncode != 0:
            return f"screencapture failed: {result.stderr.strip()}"
        # Check for blank/tiny screenshots (permissions issue)
        if os.path.isfile(path) and os.path.getsize(path) < _BLANK_SCREENSHOT_THRESHOLD:
            return (
                f"Screenshot appears blank (< {_BLANK_SCREENSHOT_THRESHOLD} bytes). "
                "Check macOS Screen Recording permissions for your terminal."
            )
        return None
    except subprocess.TimeoutExpired:
        return "screencapture timed out."
    except FileNotFoundError:
        return "screencapture not found."


def _screenshot_terminal(s: NvimSession, path: str) -> str:
    """Take a screenshot of the terminal window. Returns file path or error."""
    if s.terminal_window_id is None:
        if platform.system() != "Darwin":
            return "Error: terminal screenshots are not yet supported on Linux."
        return ("Error: terminal window ID not found. Cannot capture screenshot. "
                "On macOS, grant Screen Recording permission to your terminal app "
                "(System Settings → Privacy & Security → Screen Recording).")

    err = _capture_window(s.terminal_window_id, path)
    if err:
        return f"Error: {err}"
    return os.path.abspath(path)
