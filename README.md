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

## Tool Reference

### Lifecycle

| Tool | Params | Description |
|------|--------|-------------|
| `nvim_start` | `config`, `clean`, `headless`, `args`, `rows`, `cols` | Start a Neovim instance (PTY+socket by default, headless optional) |
| `nvim_stop` | — | Stop the running instance |
| `nvim_is_running` | — | Check if an instance is running |

### Interaction

| Tool | Params | Description |
|------|--------|-------------|
| `nvim_execute` | `command`, `timeout` | Run an Ex command, return output |
| `nvim_lua` | `code`, `timeout` | Execute Lua code, return result as JSON |
| `nvim_send_keys` | `keys`, `escape`, `timeout` | Send keystrokes. Falls back to PTY when RPC is blocked |

### Inspection

| Tool | Params | Description |
|------|--------|-------------|
| `nvim_get_buffer` | `buffer_id` | Get buffer contents with line numbers |
| `nvim_get_state` | — | Mode, cursor, file, buffers, cwd |
| `nvim_get_messages` | `clear` | `:messages` output (error checking) |
| `nvim_get_diagnostics` | `buffer_id`, `severity` | LSP diagnostics |
| `nvim_screenshot` | `output_path` | Capture PNG screenshot (works even when RPC is blocked) |
| `nvim_health_check` | — | Comprehensive check: `:checkhealth`, `:messages`, lazy-load triggers, plugin errors, diagnostics |

## Usage Examples

### Config validation

```
nvim_start()                              # Start with default config
nvim_get_messages()                       # Check for startup errors
nvim_execute("Lazy sync")                 # Sync plugins
nvim_get_messages()                       # Check sync results
nvim_lua("return require('lazy').stats().loaded")  # Verify plugin count
nvim_stop()
```

### Plugin testing

```
nvim_start()
nvim_execute("edit test.lua")
nvim_send_keys("iprint('hello')<Esc>")
nvim_get_buffer()                         # Verify buffer contents
nvim_get_diagnostics()                    # Check for LSP errors
nvim_stop()
```

### Visual inspection with screenshots

```
nvim_start(rows=40, cols=120)             # Start with larger terminal
nvim_execute("edit ~/.config/nvim/init.lua")
nvim_screenshot()                         # Returns path to PNG
nvim_send_keys("G")                       # Go to end of file
nvim_screenshot("/tmp/bottom.png")        # Save to specific path
nvim_stop()
```

### Health check

```
nvim_start()
nvim_health_check()                       # Runs :checkhealth, triggers lazy-loaded
                                          # plugins, reports all errors in one call
nvim_stop()
```

### Recovering from blocked RPC

When interactive UI (Telescope, ToggleTerm, etc.) blocks RPC:

```
nvim_screenshot()                         # Still works — captures current screen
nvim_send_keys("<Esc>", timeout=2.0)      # Falls back to PTY write automatically
nvim_send_keys("<C-c>", timeout=2.0)      # Ctrl-C via PTY to break out
```

### Leader key

`<Leader>` is a vim mapping concept, not a raw key code. Send the actual leader key:

```
nvim_send_keys("<Space>e")                # <leader>e when leader is Space
nvim_send_keys("<Space>ff")               # <leader>ff
```

## Testing

```bash
uv run python tests/test_smoke.py              # PTY mode with full config
uv run python tests/test_smoke.py --clean      # PTY mode, no config
uv run python tests/test_smoke.py --headless --clean  # Headless mode
```

## How It Works

By default, spawns Neovim in a PTY with `--listen <socket>` and connects via pynvim's socket RPC. A background `pyte` virtual terminal captures the TUI output, enabling PNG screenshots via Pillow. Pass `headless=True` to use the lighter `--embed --headless` mode (no screenshot support).
