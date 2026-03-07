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
uv run python tests/test_smoke.py              # PTY mode with full config
uv run python tests/test_smoke.py --clean      # PTY mode, no config
uv run python tests/test_smoke.py --headless --clean  # Headless mode
```

## How It Works

By default, spawns Neovim in a PTY with `--listen <socket>` and connects via pynvim's socket RPC. A background `pyte` virtual terminal captures the TUI output, enabling PNG screenshots via Pillow. Pass `headless=True` to use the lighter `--embed --headless` mode (no screenshot support).
