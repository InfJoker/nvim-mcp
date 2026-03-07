# nvim-mcp

Playwright-style MCP server for Neovim. Control nvim programmatically from AI coding assistants (Claude Code) via MCP tools.

## Requirements

- Python 3.11+
- Neovim 0.10+
- [`uv`](https://github.com/astral-sh/uv) (recommended) or `pip`

## Install

Clone the repo and register in your Claude Code settings (`~/.claude/settings.json`):

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
| `nvim_start` | `config`, `clean`, `headless`, `args` | Start an embedded Neovim instance |
| `nvim_stop` | — | Stop the running instance |

### Interaction

| Tool | Params | Description |
|------|--------|-------------|
| `nvim_execute` | `command` | Run an Ex command, return output |
| `nvim_lua` | `code` | Execute Lua code, return result as JSON |
| `nvim_send_keys` | `keys`, `escape` | Send keystrokes (`<CR>`, `<Esc>`, `<C-w>`, etc.) |

### Inspection

| Tool | Params | Description |
|------|--------|-------------|
| `nvim_get_buffer` | `buffer_id` | Get buffer contents with line numbers |
| `nvim_get_state` | — | Mode, cursor, file, buffers, cwd |
| `nvim_get_messages` | `clear` | `:messages` output (error checking) |
| `nvim_get_diagnostics` | `buffer_id`, `severity` | LSP diagnostics |

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

## How It Works

Embeds Neovim via `nvim --embed --headless` and connects through pynvim's RPC interface. All interaction happens through Neovim's API — no terminal/UI scraping.
