"""Smoke test: start nvim, exercise every tool, stop.

Usage:
    uv run python tests/test_smoke.py                         # PTY mode (default)
    uv run python tests/test_smoke.py --headless              # headless mode
    uv run python tests/test_smoke.py --clean                 # PTY + --clean (no config)
    uv run python tests/test_smoke.py --terminal kitty --clean   # terminal mode
    uv run python tests/test_smoke.py --terminal ghostty --clean
    uv run python tests/test_smoke.py --terminal iterm2 --clean
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable

# Ensure the project root is on sys.path so nvim_mcp_server can be imported
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_failures: list[str] = []
_pass_count = 0


def check(name: str, result: str, *, ok: Callable[[str], bool] | None = None) -> None:
    """Assert a tool result looks healthy."""
    global _pass_count
    failed = False

    if ok:
        if not ok(result):
            failed = True
    elif result.startswith("Error"):
        failed = True

    if failed:
        _failures.append(f"FAIL  {name}: {result[:120]}")
        print(f"  FAIL  {name}: {result[:120]}")
    else:
        _pass_count += 1
        print(f"  ok    {name}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    headless = "--headless" in sys.argv
    clean = "--clean" in sys.argv
    terminal = ""
    if "--terminal" in sys.argv:
        idx = sys.argv.index("--terminal")
        if idx + 1 < len(sys.argv):
            terminal = sys.argv[idx + 1]
        else:
            print("Error: --terminal requires a value (kitty, ghostty, iterm2)")
            return 1

    # Reset module state in case a previous run left nvim running
    import nvim_mcp_server as _mod

    if _mod._session is not None and _mod._session.nvim is not None:
        try:
            _mod.nvim_stop()
        except Exception:
            pass

    from nvim_mcp_server import (
        nvim_execute,
        nvim_get_buffer,
        nvim_get_diagnostics,
        nvim_get_messages,
        nvim_get_state,
        nvim_health_check,
        nvim_is_running,
        nvim_lua,
        nvim_resize,
        nvim_screenshot,
        nvim_send_keys,
        nvim_start,
        nvim_stop,
    )

    # -- not running yet ---------------------------------------------------
    print("pre-start checks")
    check("is_running (before start)", nvim_is_running(), ok=lambda r: '"running": false' in r)

    # -- start -------------------------------------------------------------
    if terminal:
        mode_label = f"terminal:{terminal}"
    elif headless:
        mode_label = "headless"
    else:
        mode_label = "PTY"
    if clean:
        mode_label += "+clean"
    print(f"starting nvim ({mode_label})...")
    t0 = time.time()
    result = nvim_start(
        config="~/.config/nvim",
        clean=clean,
        headless=headless,
        terminal=terminal,
        rows=30,
        cols=100,
    )
    elapsed = time.time() - t0
    print(f"  started in {elapsed:.1f}s")
    check("nvim_start", result, ok=lambda r: "started" in r.lower())

    if "started" not in result.lower():
        print(f"\nAbort: nvim failed to start — {result}")
        return 1

    # -- lifecycle ---------------------------------------------------------
    print("lifecycle")
    check("is_running", nvim_is_running(), ok=lambda r: '"running": true' in r)
    check("double start blocked", nvim_start(), ok=lambda r: "already running" in r.lower())

    # -- state -------------------------------------------------------------
    print("inspection")
    state_raw = nvim_get_state()
    check("get_state", state_raw, ok=lambda r: "mode" in r)

    check("get_buffer", nvim_get_buffer(), ok=lambda r: "Buffer:" in r)
    check("get_messages", nvim_get_messages())
    check("get_diagnostics", nvim_get_diagnostics(), ok=lambda r: "Error" not in r)

    # -- interaction -------------------------------------------------------
    print("interaction")
    check("execute :version", nvim_execute("version"), ok=lambda r: "NVIM" in r)
    check("lua eval", nvim_lua("return 1 + 1"), ok=lambda r: "2" in r)
    check("send_keys", nvim_send_keys("<Esc>"))

    # try opening a file — may fail if Alpha/autocommands interfere
    target = os.path.join(os.path.dirname(__file__), "..", "nvim_mcp_server.py")
    target = os.path.abspath(target)
    edit_out = nvim_execute(f"edit {target}")
    time.sleep(0.3)
    if "Error" not in edit_out:
        buf = nvim_get_buffer()
        check("buffer after edit", buf, ok=lambda r: "nvim_mcp_server" in r.lower())
    else:
        check("buffer after edit (autocommand)", "skipped")

    # -- resize ------------------------------------------------------------
    print("resize")
    if headless:
        check("resize (headless=unavailable)", nvim_resize(40, 120),
              ok=lambda r: "headless" in r.lower() or "not supported" in r.lower())
    elif terminal == "iterm2":
        # iTerm2 resize breaks RPC — verify it returns an error, not a crash
        check("resize (iterm2=unsupported)", nvim_resize(40, 120),
              ok=lambda r: "not supported" in r.lower())
    else:
        check("resize", nvim_resize(40, 120), ok=lambda r: "Resized" in r)
        time.sleep(0.3)
        dims = nvim_lua("return { vim.o.lines, vim.o.columns }")
        if terminal:
            # Terminal mode: window manager may prevent resize (fullscreen, tiling).
            # Verify the tool didn't error; dims may or may not match.
            check("resize dims (terminal)", dims, ok=lambda r: "Error" not in r)
        else:
            # PTY mode: we control the terminal size directly
            check("resize dims", dims, ok=lambda r: "40" in r and "120" in r)
        # Resize back to original
        check("resize back", nvim_resize(30, 100), ok=lambda r: "Resized" in r)
        time.sleep(0.3)
        check("resize validation", nvim_resize(0, 80),
              ok=lambda r: "Error" in r)

    # -- health check ------------------------------------------------------
    print("health check")
    check("health_check", nvim_health_check(), ok=lambda r: "Error" not in r)

    # -- screenshot --------------------------------------------------------
    print("screenshot")
    if headless:
        ss = nvim_screenshot()
        check("screenshot (headless=unavailable)", ss, ok=lambda r: "headless" in r.lower() or "not available" in r.lower())
    elif terminal:
        ss = nvim_screenshot()
        if ss.startswith("Error"):
            # Terminal screenshots may fail on Linux or without permissions
            check("screenshot (terminal)", ss, ok=lambda r: "not yet supported" in r.lower() or "not found" in r.lower() or "permission" in r.lower())
        else:
            check("screenshot exists", ss, ok=lambda r: r.endswith(".png") and os.path.isfile(r))
            if os.path.isfile(ss):
                size = os.path.getsize(ss)
                check("screenshot size", str(size), ok=lambda _: size > 1000)
                os.unlink(ss)
    else:
        ss = nvim_screenshot()
        check("screenshot exists", ss, ok=lambda r: r.endswith(".png") and os.path.isfile(r))
        if os.path.isfile(ss):
            size = os.path.getsize(ss)
            check("screenshot size", str(size), ok=lambda _: size > 1000)
            os.unlink(ss)

    # -- stop --------------------------------------------------------------
    print("stop")
    # Capture terminal title before stop (for orphan check)
    _terminal_title = None
    if _mod._session is not None:
        _terminal_title = _mod._session.terminal_title
    check("nvim_stop", nvim_stop(), ok=lambda r: "stopped" in r.lower())
    check("is_running (after stop)", nvim_is_running(), ok=lambda r: '"running": false' in r)

    # Verify no orphaned terminal windows remain
    if terminal and _terminal_title and sys.platform == "darwin":
        import subprocess as _sp
        import time as _time

        _time.sleep(1)  # Allow terminal to close after nvim exit
        _orphan_found = False
        try:
            r = _sp.run(
                ["osascript", "-e",
                 f'tell application "System Events"\n'
                 f'  repeat with proc in every process\n'
                 f'    repeat with w in windows of proc\n'
                 f'      if name of w contains "{_terminal_title}" then return "found"\n'
                 f'    end repeat\n'
                 f'  end repeat\n'
                 f'end tell\n'
                 f'return "none"'],
                capture_output=True, text=True, timeout=5,
            )
            _orphan_found = "found" in r.stdout
        except Exception:
            pass
        check("no orphan window", "clean" if not _orphan_found else f"orphan: {_terminal_title}",
              ok=lambda r: r == "clean")

    # -- summary -----------------------------------------------------------
    total = _pass_count + len(_failures)
    print(f"\n{_pass_count}/{total} passed", end="")
    if _failures:
        print(f", {len(_failures)} failed:")
        for f in _failures:
            print(f"  {f}")
        return 1
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
