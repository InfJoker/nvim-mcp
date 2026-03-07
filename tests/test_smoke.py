"""Smoke test: start nvim, exercise every tool, stop.

Usage:
    uv run python tests/test_smoke.py              # PTY mode (default)
    uv run python tests/test_smoke.py --headless   # headless mode
    uv run python tests/test_smoke.py --clean      # PTY + --clean (no config)
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
        nvim_screenshot,
        nvim_send_keys,
        nvim_start,
        nvim_stop,
    )

    # -- not running yet ---------------------------------------------------
    print("pre-start checks")
    check("is_running (before start)", nvim_is_running(), ok=lambda r: '"running": false' in r)

    # -- start -------------------------------------------------------------
    mode_label = "headless" if headless else "PTY"
    if clean:
        mode_label += "+clean"
    print(f"starting nvim ({mode_label})...")
    t0 = time.time()
    result = nvim_start(
        config="~/.config/nvim",
        clean=clean,
        headless=headless,
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

    # -- health check ------------------------------------------------------
    print("health check")
    check("health_check", nvim_health_check(), ok=lambda r: "Error" not in r)

    # -- screenshot --------------------------------------------------------
    print("screenshot")
    if headless:
        ss = nvim_screenshot()
        check("screenshot (headless=unavailable)", ss, ok=lambda r: "headless" in r.lower() or "not available" in r.lower())
    else:
        ss = nvim_screenshot()
        check("screenshot exists", ss, ok=lambda r: r.endswith(".png") and os.path.isfile(r))
        if os.path.isfile(ss):
            size = os.path.getsize(ss)
            check("screenshot size", str(size), ok=lambda _: size > 1000)
            os.unlink(ss)

    # -- stop --------------------------------------------------------------
    print("stop")
    check("nvim_stop", nvim_stop(), ok=lambda r: "stopped" in r.lower())
    check("is_running (after stop)", nvim_is_running(), ok=lambda r: '"running": false' in r)

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
