# CLAUDE.md

Guidance for Claude Code when working on the nvim-mcp server.

## What This Is

MCP server that spawns and controls a Neovim instance, exposing tools for lifecycle management, interaction (commands, Lua, keystrokes), state inspection, and PNG screenshots.

## Architecture

Single-file server (`nvim_mcp_server.py`) using FastMCP. Three operating modes:

- **PTY mode** (default): Spawns nvim in a pseudo-terminal with `--listen <socket>`. Connects via raw msgpack-rpc (`NvimRPC` class). Background thread feeds PTY output into pyte for terminal emulation. Supports screenshots.
- **Headless mode**: Uses `pynvim.attach("child", argv=["nvim", "--embed", "--headless"])`. No PTY, no screenshots. Lighter weight.
- **Terminal mode**: Launches nvim inside a real terminal emulator (Kitty, Ghostty, iTerm2). Connects via the same NvimRPC socket. Screenshots via macOS `screencapture -l <window_id>`. No pyte — the terminal does all rendering.

### Key Components

| Component | Purpose |
|---|---|
| `NvimSession` dataclass | All mutable session state (nvim connection, process, PTY fd, pyte screen, threading primitives) |
| `NvimRPC` class | Raw msgpack-rpc client over Unix socket. Replaces pynvim for PTY mode to avoid asyncio conflicts. |
| `_pty_reader_thread` | Background daemon thread draining PTY output into pyte. Pause/resume via `Event` objects. |
| `_with_drained_pty` | Context manager ensuring PTY reader is paused during screenshots and resumed after. |
| `_dismiss_press_enter` | Adaptive startup: polls RPC with short timeout, sends `\r` to dismiss prompts when blocked (PTY mode). |
| `_dismiss_press_enter_rpc` | Same as above but sends `\r` via `nvim_input()` RPC (terminal mode — no PTY fd). |
| `_wait_for_lazy` | Polls lazy.nvim load status. Sends `\r` via PTY or RPC input during loading. |
| `_start_terminal` | Launches terminal emulator with nvim, connects RPC, discovers window ID. |
| `_screenshot_terminal` | Captures terminal window via `screencapture -l <window_id>` (macOS). |
| `_find_window_id` | Finds macOS CGWindowID by title via Quartz/osascript (polls with timeout). |
| `_get_window_owner_pid` | Returns PID of a CGWindowID's owner via Quartz (`kCGWindowListOptionIncludingWindow`). |
| `_env_wrapped_cmd` | Wraps a command with `/usr/bin/env VAR=val` for env vars differing from `os.environ`. Used by kitty (`open -gna` loses env) and iTerm2 (AppleScript `command` string). |
| `_teardown_terminal` | Multi-strategy terminal cleanup: process group kill (ghostty), `kitten @ quit` + PID SIGTERM (kitty), AppleScript `close window id` (iTerm2). |

### Globals

Single module-level mutable: `_session: NvimSession | None`. All tool functions access it via `_require_nvim()`.

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
- `NvimRPC` exposes only the API surface used in this file. When adding new tool functions that need additional nvim API calls, add corresponding methods to `NvimRPC`.

### RPC Timeout and PTY Fallback

Interactive UI (Telescope, ToggleTerm, floating windows) blocks RPC because nvim enters input mode in the floating window and stops processing RPC messages. The server handles this with:

1. **Configurable `timeout` parameter** on `nvim_execute`, `nvim_lua`, `nvim_send_keys` — defaults to `_RPC_DEFAULT_TIMEOUT` (10s).
2. **PTY/input fallback in `nvim_send_keys`** — when RPC times out, writes raw bytes to PTY fd (`_pty_send_raw`) or uses `nvim_input()` in terminal mode. This lets `<Esc>` and `<C-c>` dismiss floating windows even when RPC is frozen.
3. **Resilient `nvim_screenshot`** — tries RPC flush with short timeout (`_RPC_FLUSH_TIMEOUT`), but proceeds with screenshot regardless. The pyte screen always has the current terminal state.
4. **`_keys_to_raw` helper** — translates key notation (`<Esc>`, `<C-c>`, `<CR>`, etc.) to raw bytes for PTY writes. Case-insensitive (`<cr>` = `<CR>`). Uses `_RAW_KEY_MAP` dict (uppercase keys) and `_SPECIAL_KEY_RE` regex.
5. **`_rpc_timeout` context manager** — temporarily overrides RPC timeout, restoring the actual previous value via `NvimRPC.get_timeout()`. Used by all interaction tools and `nvim_screenshot`.
6. **`nvim_health_check`** — decomposed into 5 helpers: `_check_startup_messages`, `_run_checkhealth`, `_trigger_lazy_plugins`, `_check_plugin_errors`, `_check_diagnostics`. Each manages its own buffer cleanup.

### Process Management

nvim 0.10+ spawns a UI client (parent) + server child. To kill both:
- `subprocess.Popen(..., process_group=0)` — creates a separate process group
- `os.killpg(proc.pid, signal.SIGTERM)` — kills the entire group

Terminal teardown varies by launcher:
- **Ghostty**: Has `terminal_proc` → `os.killpg` (standard process group pattern)
- **Kitty**: No `terminal_proc` (launched via `open -gna`) → `kitten @ quit` via socket, then SIGTERM by `kitty_pid` (discovered at launch via Quartz) as fallback
- **iTerm2**: No `terminal_proc` (launched via AppleScript) → `close window id` using `iterm2_window_id` (captured at launch), session-name search as fallback

## Constants

All timeout and buffer-size values are named constants at the top of the file. When tuning behavior, modify constants — don't introduce new magic numbers.

### Terminal Mode

Launches nvim inside a real terminal emulator. Key details:

- **Supported terminals:** Kitty, Ghostty, iTerm2 (macOS only)
- **Focus steal prevention:**
  - Kitty: `open -gna kitty.app --args ...` — macOS `-g` flag prevents activation at WindowServer level. `.app` bundle resolved from binary via `os.path.realpath`. Also passes `-o close_on_child_death=yes -o macos_quit_when_last_window_closed=yes` so kitty auto-exits when nvim stops.
  - iTerm2: Atomic AppleScript saves frontmost process name via System Events, creates window, captures `iterm2_window_id`, then restores focus in a `try` block after `delay _APPLESCRIPT_FOCUS_DELAY`. Focus only restored if iTerm2 actually became frontmost.
  - Ghostty: `subprocess.Popen` directly — already doesn't steal focus.
- **Environment propagation:** `_env_wrapped_cmd` wraps nvim command with `/usr/bin/env VAR=val` for env vars that differ from `os.environ`. Needed because `open -gna` spawns via launchd (loses calling env) and iTerm2 `command` string runs in a separate shell.
- **Window ID discovery:** `_find_window_id` polls by title (`nvim-mcp-<uuid>`) via Quartz, with osascript fallback (iterates all System Events windows). All terminals use title-based lookup.
- **Kitty remote control:** `--listen-on unix:<kitty_socket>` stored on `NvimSession.kitty_socket`. Used for `kitten @ quit` during teardown.
- **Kitty PID tracking:** `NvimSession.kitty_pid` discovered at launch via `_get_window_owner_pid` (Quartz `kCGWindowListOptionIncludingWindow`). Used as SIGTERM fallback if `kitten @ quit` fails.
- **iTerm2 window ID:** `NvimSession.iterm2_window_id` (str) captured from AppleScript `return windowId`. Used for `close window id` during teardown. Distinct from `terminal_window_id` (int, CGWindowID for screenshots).
- **Screenshots:** `screencapture -l <window_id>` (macOS only). Linux returns "not yet supported"
- **No new dependencies:** Uses macOS system utilities and optionally imports `Quartz`

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

Defined in `pyproject.toml`: `mcp[cli]`, `msgpack`, `pynvim`, `pyte`, `Pillow`.

Run with `uv run` — do not use `.venv/bin/python` directly.
