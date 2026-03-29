# nvim-mcp

MCP server that spawns and controls a Neovim instance. It lets your AI agent test your Neovim configuration — open files, run commands, execute Lua, send keystrokes, inspect buffers, and capture PNG screenshots.

Three modes serve different needs. **PTY mode** runs Neovim in a pseudo-terminal with full screenshot support via terminal emulation. **Headless mode** embeds Neovim without a display, for lightweight scripting. **Terminal mode** launches Neovim inside a real GUI terminal and captures its window for screenshots.

Terminal mode supports Kitty, Ghostty, and iTerm2 on macOS. Each terminal handles window discovery, focus management, and cleanup through its own strategy. Screenshots use macOS `screencapture` and require Screen Recording permission.

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
