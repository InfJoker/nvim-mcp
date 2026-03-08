"""Lightweight msgpack-rpc client for Neovim over Unix socket."""

from __future__ import annotations

import socket as socket_mod
import threading

import msgpack

from nvim_mcp.state import NvimRPCError, _PTY_READ_SIZE, _SOCKET_CONNECT_TIMEOUT


class NvimRPC:
    """Thin msgpack-rpc client over a Unix domain socket.

    Replaces pynvim for socket connections to avoid asyncio event-loop
    conflicts with the PTY reader thread.
    """

    def __init__(self, path: str, timeout: float = _SOCKET_CONNECT_TIMEOUT):
        self._sock = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        self._sock.settimeout(timeout)
        self._sock.connect(path)
        self._unpacker = msgpack.Unpacker(raw=False)
        self._msgid = 0
        self._lock = threading.Lock()

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def get_timeout(self) -> float | None:
        """Get the current socket timeout."""
        return self._sock.gettimeout()

    def set_timeout(self, timeout: float) -> None:
        """Set the socket timeout for RPC calls."""
        self._sock.settimeout(timeout)

    def _call(self, method: str, args: list) -> object:
        with self._lock:
            self._msgid += 1
            msgid = self._msgid
            req = msgpack.packb([0, msgid, method, args])
            self._sock.sendall(req)
            while True:
                data = self._sock.recv(_PTY_READ_SIZE)
                if not data:
                    raise EOFError("Neovim socket closed")
                self._unpacker.feed(data)
                for msg in self._unpacker:
                    if not isinstance(msg, (list, tuple)) or len(msg) < 4:
                        continue
                    mtype, mid, err, result = msg[0], msg[1], msg[2], msg[3]
                    if mtype == 1 and mid == msgid:
                        if err:
                            raise NvimRPCError(err if isinstance(err, str) else str(err))
                        return result
                    # notifications (type 2) or mismatched responses — skip

    # -- Public API matching pynvim's interface used in this file --

    def eval(self, expr: str) -> object:
        return self._call("nvim_eval", [expr])

    def command(self, cmd: str) -> None:
        self._call("nvim_command", [cmd])

    def command_output(self, cmd: str) -> str:
        result = self._call("nvim_exec2", [cmd, {"output": True}])
        if isinstance(result, dict):
            return result.get("output", "")
        return str(result) if result else ""

    def exec_lua(self, code: str, *args: object) -> object:
        return self._call("nvim_exec_lua", [code, list(args)])

    def input(self, keys: str) -> int:
        return self._call("nvim_input", [keys])

    def feedkeys(self, keys: str, mode: str, escape_ks: bool) -> None:
        self._call("nvim_feedkeys", [keys, mode, escape_ks])

    def replace_termcodes(
        self, s: str, from_part: bool, do_lt: bool, special: bool
    ) -> str:
        result = self._call("nvim_replace_termcodes", [s, from_part, do_lt, special])
        return result if isinstance(result, str) else str(result)

    @property
    def api(self) -> "_NvimAPI":
        return _NvimAPI(self)

    @property
    def current(self) -> "_NvimCurrent":
        return _NvimCurrent(self)


class _NvimAPI:
    def __init__(self, rpc: NvimRPC):
        self._rpc = rpc

    def buf_get_name(self, buf: int) -> str:
        result = self._rpc._call("nvim_buf_get_name", [buf])
        return result if isinstance(result, str) else str(result)

    def buf_get_lines(
        self, buf: int, start: int, end: int, strict: bool
    ) -> list[str]:
        return self._rpc._call("nvim_buf_get_lines", [buf, start, end, strict])


class _NvimCurrent:
    def __init__(self, rpc: NvimRPC):
        self._rpc = rpc

    @property
    def buffer(self) -> "_NvimCurrentBuffer":
        return _NvimCurrentBuffer(self._rpc)


class _NvimCurrentBuffer:
    def __init__(self, rpc: NvimRPC):
        self._rpc = rpc

    @property
    def number(self) -> int:
        return self._rpc._call("nvim_get_current_buf", [])
