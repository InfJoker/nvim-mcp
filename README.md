# nvim-mcp

Playwright-style MCP server for Neovim. Control nvim programmatically from AI coding assistants (Claude Code) via MCP tools.

## Requirements

- Python 3.11+
- Neovim 0.10+
- [`uv`](https://github.com/astral-sh/uv) (recommended) or `pip`

## Install

Clone the repo and register in `~/.claude/.mcp.json`:

```json
{
  "mcpServers": {
    "neovim": {
      "command": "uv",
      "args": [
        "run", "--project", "/path/to/nvim-mcp",
        "python", "/path/to/nvim-mcp/nvim_mcp_server.py"
      ]
    }
  }
}
```

Dependencies are managed automatically by `uv run --project`.

## Tools

### Lifecycle

| Tool | Params |
|------|--------|
| `nvim_start` | `config`, `clean`, `headless`, `terminal`, `args`, `rows`, `cols` |
| `nvim_stop` | — |
| `nvim_is_running` | — |

### Interaction

| Tool | Params |
|------|--------|
| `nvim_execute` | `command`, `timeout` |
| `nvim_lua` | `code`, `timeout` |
| `nvim_send_keys` | `keys`, `escape`, `timeout` |

### Inspection

| Tool | Params |
|------|--------|
| `nvim_get_buffer` | `buffer_id` |
| `nvim_get_state` | — |
| `nvim_get_messages` | `clear` |
| `nvim_get_diagnostics` | `buffer_id`, `severity` |
| `nvim_screenshot` | `output_path` |
| `nvim_health_check` | — |

Tool descriptions and usage guidance are in the tool docstrings (visible to AI models via MCP).

## Testing

```bash
uv run python tests/test_smoke.py                        # PTY mode with full config
uv run python tests/test_smoke.py --clean                # PTY mode, no config
uv run python tests/test_smoke.py --headless --clean     # Headless mode
uv run python tests/test_smoke.py --terminal kitty       # Terminal mode (Kitty)
uv run python tests/test_smoke.py --terminal ghostty     # Terminal mode (Ghostty)
uv run python tests/test_smoke.py --terminal iterm2      # Terminal mode (iTerm2, macOS)
```

Add `--clean` to any terminal mode test to skip loading your nvim config.

## How It Works

Three operating modes:

- **PTY mode** (default): Spawns nvim in a pseudo-terminal with `--listen <socket>`. Connects via msgpack-rpc. A background `pyte` virtual terminal captures TUI output for PNG screenshots.
- **Headless mode**: Uses `pynvim.attach("child", argv=["nvim", "--embed", "--headless"])`. No screenshots. Lighter weight.
- **Terminal mode** (macOS only): Launches nvim inside a real terminal emulator (Kitty, Ghostty, iTerm2). Screenshots via macOS `screencapture`. Terminals launch in the background without stealing focus. Linux support is not yet implemented.
