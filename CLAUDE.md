# CLAUDE.md

Guidance for Claude Code when working on the nvim-mcp server.

## What This Is

MCP server that spawns and controls a Neovim instance, exposing tools for lifecycle management, interaction (commands, Lua, keystrokes), state inspection, and PNG screenshots.

## Architecture

Modular package (`nvim_mcp/`) with a thin entry point (`nvim_mcp_server.py`). Three operating modes:

- **PTY mode** (default): Spawns nvim in a pseudo-terminal with `--listen <socket>`. Connects via raw msgpack-rpc (`NvimRPC` class). Background thread feeds PTY output into pyte for terminal emulation. Supports screenshots.
- **Headless mode**: Uses `pynvim.attach("child", argv=["nvim", "--embed", "--headless"])`. No PTY, no screenshots. Lighter weight.
- **Terminal mode**: Launches nvim inside a real terminal emulator (Kitty, Ghostty, iTerm2). Connects via the same NvimRPC socket. Screenshots via macOS `screencapture -l <window_id>`. No pyte — the terminal does all rendering.

### Module Structure

```
nvim_mcp_server.py          # Thin entry point, re-exports tool functions + __getattr__ for _session
nvim_mcp/
    __init__.py              # Imports tools to register @mcp.tool() decorators
    state.py                 # Constants, NvimRPCError, NvimSession, mcp instance, session accessors
    rpc.py                   # NvimRPC class + _NvimAPI/_NvimCurrent/_NvimCurrentBuffer
    rendering.py             # Color maps, font loading, _render_screen_to_png
    pty.py                   # PTY creation/reader/drain, pyte feeding, SGR fix, _teardown, _cleanup
    terminal.py              # Terminal launch/teardown/screenshot, window ID discovery
    tools.py                 # All 12 @mcp.tool() functions + their private helpers
```

**Dependency DAG (no cycles):**
`tools.py` → `state.py`, `rpc.py`, `pty.py`, `terminal.py`, `rendering.py`.
`terminal.py` → `state.py`, `rpc.py`. `pty.py` → `state.py`, `rpc.py` (late import of `terminal._teardown_terminal` in `_cleanup`).
`rpc.py` → `state.py`. `rendering.py` → (no intra-package imports). `state.py` → (no intra-package imports, late import of `rpc.NvimRPC` in `_rpc_timeout`).

### Key Components

| Component | Module | Purpose |
|---|---|---|
| `NvimSession` dataclass | `state.py` | All mutable session state (nvim connection, process, PTY fd, pyte screen, threading primitives) |
| `NvimRPC` class | `rpc.py` | Raw msgpack-rpc client over Unix socket. Replaces pynvim for PTY mode to avoid asyncio conflicts. |
| `_pty_reader_thread` | `pty.py` | Background daemon thread draining PTY output into pyte. Pause/resume via `Event` objects. |
| `_with_drained_pty` | `pty.py` | Context manager ensuring PTY reader is paused during screenshots and resumed after. |
| `_dismiss_press_enter` | `pty.py` | Adaptive startup: polls RPC with short timeout, sends `\r` to dismiss prompts when blocked (PTY mode). |
| `_dismiss_press_enter_rpc` | `terminal.py` | Same as above but sends `\r` via `nvim_input()` RPC (terminal mode — no PTY fd). |
| `_wait_for_lazy` | `tools.py` | Polls lazy.nvim load status. Sends `\r` via PTY or RPC input during loading. |
| `_start_terminal` | `terminal.py` | Launches terminal emulator with nvim, connects RPC, discovers window ID. |
| `_screenshot_terminal` | `terminal.py` | Captures terminal window via `screencapture -l <window_id>` (macOS). |
| `_find_window_id` | `terminal.py` | Finds macOS CGWindowID by PID or title via Quartz/osascript (polls with timeout). |
| `_find_kitty_window_id` | `terminal.py` | Gets CGWindowID from kitty via `kitten @ ls` remote control (no macOS permissions needed). |
| `_get_window_owner_pid` | `terminal.py` | Returns PID of a CGWindowID's owner via Quartz (`kCGWindowListOptionIncludingWindow`). |
| `_env_wrapped_cmd` | `terminal.py` | Wraps a command with `/usr/bin/env VAR=val` for env vars differing from `os.environ`. |
| `_teardown_terminal` | `terminal.py` | Multi-strategy terminal cleanup: process group kill (ghostty), `kitten @ quit` + PID SIGTERM (kitty), AppleScript `close window id` (iTerm2). |
| `_resize_pty` | `pty.py` | Resizes PTY (ioctl TIOCSWINSZ) and pyte screen. Must run inside `_with_drained_pty`. |
| `_resize_terminal` | `terminal.py` | Resizes terminal window: `kitten @ resize-os-window` (kitty), AppleScript (iTerm2), `set columns/lines` fallback (ghostty). |

### Session State

Module-level `_session: NvimSession | None` in `state.py`, accessed via `get_session()` / `set_session()`. Tool functions use `_require_nvim()` to get the active connection. `nvim_mcp_server.py` provides `__getattr__` for backward-compatible `_session` attribute access.

## Important Patterns

### Plugin "Press ENTER" Prompts

Neovim plugin errors trigger `wait_return()` which blocks the server's main loop including RPC. The server handles this with:

1. `--cmd "set nomore"` — prevents "-- More --" pager
2. Adaptive `\r` sending — sends carriage return through PTY (or via `nvim_input()` in terminal mode) whenever RPC times out
3. `set more` restored **after** lazy.nvim finishes loading, not before

This pattern applies in `_dismiss_press_enter` (PTY), `_dismiss_press_enter_rpc` (terminal), and `_wait_for_lazy` (both).

### PTY Reader Thread Lifecycle

The reader thread runs continuously, but must be paused during screenshots so `_drain_pty` can be the sole fd consumer. Protocol:

1. Clear `pty_reader_active` → reader pauses
2. Wait for `pty_reader_paused` acknowledgment
3. Read remaining PTY data until quiet
4. Render screenshot under `pty_lock`
5. Set `pty_reader_active` → reader resumes

Always use `_with_drained_pty` context manager — never call `_drain_pty` directly.

### NvimRPC vs pynvim

- `pynvim.attach("socket")` uses asyncio internally, which conflicts with the PTY reader thread's `select()` calls. Use `NvimRPC` for socket connections.
- `pynvim.attach("child")` doesn't accept an `env` parameter. Must temporarily modify `os.environ` in a `try/finally` block.
- `NvimRPC` exposes only the API surface used in the codebase. When adding new tool functions that need additional nvim API calls, add corresponding methods to `NvimRPC` in `rpc.py`.

### RPC Timeout and PTY Fallback

Interactive UI (Telescope, ToggleTerm, floating windows) blocks RPC because nvim enters input mode in the floating window and stops processing RPC messages. The server handles this with:

1. **Configurable `timeout` parameter** on `nvim_execute`, `nvim_lua`, `nvim_send_keys` — defaults to `_RPC_DEFAULT_TIMEOUT` (10s).
2. **PTY/input fallback in `nvim_send_keys`** — when RPC times out, writes raw bytes to PTY fd (`_pty_send_raw`) or uses `nvim_input()` in terminal mode. This lets `<Esc>` and `<C-c>` dismiss floating windows even when RPC is frozen.
3. **Resilient `nvim_screenshot`** — tries RPC flush with short timeout (`_RPC_FLUSH_TIMEOUT`), but proceeds with screenshot regardless. The pyte screen always has the current terminal state.
4. **`_keys_to_raw` helper** — translates key notation (`<Esc>`, `<C-c>`, `<CR>`, etc.) to raw bytes for PTY writes. Case-insensitive (`<cr>` = `<CR>`). Uses `_RAW_KEY_MAP` dict (uppercase keys) and `_SPECIAL_KEY_RE` regex.
5. **`_rpc_timeout` context manager** — temporarily overrides RPC timeout, restoring the actual previous value via `NvimRPC.get_timeout()`. Used by all interaction tools and `nvim_screenshot`.
6. **`nvim_health_check`** — decomposed into 5 helpers: `_check_startup_messages`, `_run_checkhealth`, `_trigger_lazy_plugins`, `_check_plugin_errors`, `_check_diagnostics`. Each manages its own buffer cleanup.

### Terminal Resize

`nvim_resize` tool resizes the terminal across all modes:

- **PTY mode**: Resize PTY via `ioctl(TIOCSWINSZ)` + pyte `screen.resize()` inside `_with_drained_pty`, then notify nvim via `set columns/lines` RPC **after** the reader resumes. The RPC command must run outside the drained context to avoid filling the PTY OS buffer while the reader is paused.
- **Kitty**: `kitten @ resize-os-window --width {cols} --height {rows} --unit cells` via remote control socket.
- **Ghostty**: No remote control API. Falls back to nvim `set columns/lines` — nvim renders within the requested area but the terminal window itself does not resize.
- **iTerm2**: Not supported. Resizing via AppleScript `set bounds` breaks the nvim RPC connection permanently. Use `nvim_stop` + `nvim_start` with desired size instead.
- **Headless**: Not supported (no visual terminal).

Initial window size at launch:
- **PTY**: `_create_pty(rows, cols)` sets PTY size via `ioctl(TIOCSWINSZ)`.
- **Kitty**: `-o initial_window_width={cols}c -o initial_window_height={rows}c -o remember_window_size=no`.
- **Ghostty**: `--window-width={cols} --window-height={rows}`.
- **iTerm2**: AppleScript `set bounds` with cell-size measurement after window creation.

### Process Management

nvim 0.10+ spawns a UI client (parent) + server child. To kill both:
- `subprocess.Popen(..., process_group=0)` — creates a separate process group
- `os.killpg(proc.pid, signal.SIGTERM)` — kills the entire group

Terminal teardown varies by launcher:
- **Kitty**: `kitten @ quit` via socket, then SIGTERM by `terminal_pid` as fallback
- **Ghostty**: SIGTERM by `terminal_pid` (discovered at launch via Quartz)
- **iTerm2**: AppleScript `close window id` using `iterm2_window_id` (captured at launch), session-name search as fallback. Never SIGTERM — iTerm2 is a single shared process.

## Constants

All timeout and buffer-size values are named constants in `state.py`. When tuning behavior, modify constants — don't introduce new magic numbers.

### Terminal Mode

Launches nvim inside a real terminal emulator. Key details:

- **Supported terminals:** Kitty, Ghostty, iTerm2 (macOS only)
- **Focus steal prevention:** All terminals use `open -na` with focus restoration via shared `_capture_frontmost_app()` / `_restore_focus()` helpers. `.app` bundle resolved from binary via `_resolve_app_bundle()`.
  - Kitty: `open -na kitty.app --args ...`. Passes `-o close_on_child_death=yes -o macos_quit_when_last_window_closed=yes` so kitty auto-exits when nvim stops.
  - Ghostty: `open -na Ghostty.app --args ...`. Uses `--command=` instead of `-e` to bypass v1.2.0+ execution security prompt (GHSA-q9fg-cpmh-c78x).
  - iTerm2: Atomic AppleScript with inline focus capture/restore (not `_restore_focus` — AppleScript handles it atomically).
- **Environment propagation:** `_env_wrapped_cmd` wraps nvim command with `/usr/bin/env VAR=val` for env vars that differ from `os.environ`. Needed because `open -na` spawns via launchd (loses calling env) and iTerm2 `command` string runs in a separate shell.
- **Window ID discovery:** Three-tier cascade avoids Screen Recording permission where possible:
  1. Terminal-specific primary: Kitty uses `kitten @ ls` (needs `allow_remote_control`). iTerm2 uses AppleScript `id of newWindow` (CGWindowID).
  2. Diff-based fallback (all terminals): `_find_new_window_id` snapshots window IDs before launch (`windows_before`), finds the new one via `kCGWindowOwnerName`. Prefers title match (`kCGWindowName`, needs Screen Recording) to avoid kitty helper windows; falls back to highest ID. Polls until title appears or timeout.
  3. Title-based last resort: `_find_window_id(title=...)` polls via Quartz/osascript (requires Screen Recording for `kCGWindowName`).
  - Note: `screencapture -l <window_id>` always requires Screen Recording permission regardless of how the ID was discovered.
- **Kitty remote control:** `--listen-on unix:<kitty_socket>` stored on `NvimSession.kitty_socket`. Used for `kitten @ quit` during teardown.
- **Terminal PID tracking:** `NvimSession.terminal_pid` discovered at launch via `_get_window_owner_pid` (Quartz). Used as SIGTERM fallback for kitty (if `kitten @ quit` fails) and ghostty. Shared field — no per-terminal PID fields.
- **iTerm2 window ID:** `NvimSession.iterm2_window_id` (int | None) parsed from AppleScript `return windowId`. Used for `close window id` during teardown. Also assigned to `terminal_window_id` for screenshots.
- **Screenshots:** `screencapture -l <window_id>` (macOS only). Linux returns "not yet supported"
- **macOS dependencies:** `pyobjc-framework-Quartz` for CGWindowList APIs. Also uses system `osascript` and `screencapture`.

## Testing

Smoke test: `uv run python tests/test_smoke.py`

| Flag | Mode |
|---|---|
| (none) | PTY with full `~/.config/nvim` config |
| `--clean` | PTY with `--clean` (no config) |
| `--headless --clean` | Headless embed mode |
| `--terminal kitty --clean` | Terminal mode with Kitty |
| `--terminal ghostty --clean` | Terminal mode with Ghostty |
| `--terminal iterm2 --clean` | Terminal mode with iTerm2 (macOS) |

The test exercises all tools: lifecycle, inspection, interaction, and screenshots (PTY/terminal) or headless error (headless). Terminal mode tests include an orphan window check after `nvim_stop` — verifies no leftover terminal windows by scanning System Events for the session's unique title.

## Dependencies

Defined in `pyproject.toml`: `mcp[cli]`, `msgpack`, `pynvim`, `pyte`, `Pillow`, `pyobjc-framework-Quartz` (macOS only).

Run with `uv run` — do not use `.venv/bin/python` directly.
