# CLAUDE.md

Guidance for Claude Code when working on the nvim-mcp server.

## What This Is

MCP server that spawns and controls a Neovim instance, exposing tools for lifecycle management, interaction (commands, Lua, keystrokes), state inspection, and PNG screenshots.

## Architecture

Single-file server (`nvim_mcp_server.py`) using FastMCP. Two operating modes:

- **PTY mode** (default): Spawns nvim in a pseudo-terminal with `--listen <socket>`. Connects via raw msgpack-rpc (`NvimRPC` class). Background thread feeds PTY output into pyte for terminal emulation. Supports screenshots.
- **Headless mode**: Uses `pynvim.attach("child", argv=["nvim", "--embed", "--headless"])`. No PTY, no screenshots. Lighter weight.

### Key Components

| Component | Purpose |
|---|---|
| `NvimSession` dataclass | All mutable session state (nvim connection, process, PTY fd, pyte screen, threading primitives) |
| `NvimRPC` class | Raw msgpack-rpc client over Unix socket. Replaces pynvim for PTY mode to avoid asyncio conflicts. |
| `_pty_reader_thread` | Background daemon thread draining PTY output into pyte. Pause/resume via `Event` objects. |
| `_with_drained_pty` | Context manager ensuring PTY reader is paused during screenshots and resumed after. |
| `_dismiss_press_enter` | Adaptive startup: polls RPC with short timeout, sends `\r` to dismiss prompts when blocked. |
| `_wait_for_lazy` | Polls lazy.nvim load status. Also sends `\r` during loading since plugins can trigger new prompts. |

### Globals

Single module-level mutable: `_session: NvimSession | None`. All tool functions access it via `_require_nvim()`.

## Important Patterns

### Plugin "Press ENTER" Prompts

Neovim plugin errors trigger `wait_return()` which blocks the server's main loop including RPC. The server handles this with:

1. `--cmd "set nomore"` — prevents "-- More --" pager
2. Adaptive `\r` sending — sends carriage return through PTY whenever RPC times out
3. `set more` restored **after** lazy.nvim finishes loading, not before

This pattern applies in both `_dismiss_press_enter` (initial connect) and `_wait_for_lazy` (plugin loading phase).

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
2. **PTY fallback in `nvim_send_keys`** — when RPC times out, automatically writes raw bytes to the PTY fd via `_pty_send_raw`. This lets `<Esc>` and `<C-c>` dismiss floating windows even when RPC is frozen.
3. **Resilient `nvim_screenshot`** — tries RPC flush with short timeout (`_RPC_FLUSH_TIMEOUT`), but proceeds with screenshot regardless. The pyte screen always has the current terminal state.
4. **`_keys_to_raw` helper** — translates key notation (`<Esc>`, `<C-c>`, `<CR>`, etc.) to raw bytes for PTY writes. Case-insensitive (`<cr>` = `<CR>`). Uses `_RAW_KEY_MAP` dict (uppercase keys) and `_SPECIAL_KEY_RE` regex.
5. **`_rpc_timeout` context manager** — temporarily overrides RPC timeout, restoring the actual previous value via `NvimRPC.get_timeout()`. Used by all interaction tools and `nvim_screenshot`.
6. **`nvim_health_check`** — decomposed into 5 helpers: `_check_startup_messages`, `_run_checkhealth`, `_trigger_lazy_plugins`, `_check_plugin_errors`, `_check_diagnostics`. Each manages its own buffer cleanup.

### Process Management

nvim 0.10+ spawns a UI client (parent) + server child. To kill both:
- `subprocess.Popen(..., process_group=0)` — creates a separate process group
- `os.killpg(proc.pid, signal.SIGTERM)` — kills the entire group

## Constants

All timeout and buffer-size values are named constants at the top of the file. When tuning behavior, modify constants — don't introduce new magic numbers.

## Testing

Smoke test: `uv run python tests/test_smoke.py`

| Flag | Mode |
|---|---|
| (none) | PTY with full `~/.config/nvim` config |
| `--clean` | PTY with `--clean` (no config) |
| `--headless --clean` | Headless embed mode |

The test exercises all tools: lifecycle, inspection, interaction, and screenshots (PTY) or headless error (headless).

## Dependencies

Defined in `pyproject.toml`: `mcp[cli]`, `msgpack`, `pynvim`, `pyte`, `Pillow`.

Run with `uv run` — do not use `.venv/bin/python` directly.
