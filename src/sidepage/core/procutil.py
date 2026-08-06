"""Cross-platform pid liveness/termination — the one place raw `os.kill`
is allowed, because its semantics aren't actually portable the way the
call site would suggest.

Two POSIX idioms this codebase relied on don't translate to Windows:

- `os.kill(pid, 0)` as a liveness probe. On POSIX, signal 0 sends nothing
  and just checks deliverability. On Windows, `os.kill`'s `sig` argument
  is passed straight through to `TerminateProcess(handle, sig)` for any
  value other than `CTRL_C_EVENT`/`CTRL_BREAK_EVENT` — so `os.kill(pid, 0)`
  there doesn't probe the process, it **kills it** (with exit code 0).
- `signal.SIGKILL` doesn't exist in the `signal` module on Windows at
  all — referencing it raises `AttributeError` before a kill is even
  attempted.

`pid_alive`/`terminate` give every caller (registry, tunnel_manager,
process) the same behavior on both platforms instead of each working
around this differently.
"""

from __future__ import annotations

import os
import signal
import subprocess

if os.name == "nt":
    import ctypes

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _PROCESS_TERMINATE = 0x0001
    _STILL_ACTIVE = 259

    def pid_alive(pid: int) -> bool:
        handle = ctypes.windll.kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == _STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    def terminate(pid: int, *, force: bool = False) -> None:
        """Best-effort graceful stop, or a hard kill when `force` (or the
        graceful attempt itself fails to even dispatch)."""
        if not force:
            try:
                os.kill(pid, signal.CTRL_BREAK_EVENT)
                return
            except OSError:
                pass
        handle = ctypes.windll.kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
        if not handle:
            return
        try:
            ctypes.windll.kernel32.TerminateProcess(handle, 1)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    def popen_detached_kwargs() -> dict:
        """Puts the child in its own process group so a later, unrelated
        `sidepage` invocation can still target it with CTRL_BREAK — mirrors
        what `start_new_session=True` buys on POSIX (not literally
        supported by Popen on Windows, hence the ValueError it'd raise)."""
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}

else:

    def pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists, just owned by someone else
        return True

    def terminate(pid: int, *, force: bool = False) -> None:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)

    def popen_detached_kwargs() -> dict:
        return {"start_new_session": True}
