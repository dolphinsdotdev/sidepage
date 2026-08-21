"""Cross-platform substitutes for the POSIX-only primitives the rest of
`sidepage.core` used to reach for directly (`fcntl`, `os.kill(pid, 0)` as
a liveness probe, `signal.SIGKILL`) — centralized here once instead of
duplicated `ctypes`/`msvcrt` branches in both `sidepage.core.registry` and
`sidepage.core.tunnel_manager`, the two places that needed them.

**Why `os.kill(pid, 0)` specifically had to go, not just gain a Windows
branch**: on POSIX, signal `0` sends nothing — it's purely a permission/
existence probe, which is exactly why `registry.is_alive` and
`tunnel_manager._is_pid_alive` used it that way. On Windows, `os.kill()`
has no such concept: per CPython's Windows implementation, any signal
value other than `CTRL_C_EVENT`/`CTRL_BREAK_EVENT` (or `SIGTERM`, treated
as a terminate request) is passed straight to `TerminateProcess(handle,
sig)` — `sig=0` is "any other value," so `os.kill(pid, 0)` on Windows
**kills the process being checked**, with exit code 0. That "probe" runs
on every `sidepage ls`/`status`/`stop` and before every shared-tunnel
start/stop decision, so left as-is it would silently kill running apps
just by checking on them. `is_pid_alive` below never calls `os.kill` on
Windows at all.

Imports of `fcntl`/`msvcrt` are deliberately inside each branch, not at
module top — this module must itself be importable on both platforms.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import IO

_WINDOWS = sys.platform == "win32"


def is_pid_alive(pid: int) -> bool:
    """True if `pid` refers to a process that currently exists — a pure
    probe, never a signal/terminate action on either platform."""
    if _WINDOWS:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
        return True

    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@contextmanager
def lock_exclusive(fh: IO) -> Iterator[None]:
    """Hold an exclusive advisory lock on `fh` for the duration of the
    `with` block, blocking until it's acquired — same contract as POSIX
    `fcntl.flock(fh, LOCK_EX)` / `LOCK_UN`, which is exactly what this is
    on POSIX, unchanged.

    Windows has no whole-file `flock` equivalent: `msvcrt.locking()` locks
    a byte range starting at the file's current position, and its
    blocking mode (`LK_LOCK`) only retries internally for about a second
    before raising rather than blocking indefinitely — not what a
    cross-process critical section needs. Emulated instead with a
    non-blocking attempt (`LK_NBLCK`) retried in a loop until it
    succeeds, locking a single fixed byte (offset 0) so every caller
    contends for the same byte regardless of the file's current position.
    """
    if _WINDOWS:
        import msvcrt

        fh.seek(0)
        while True:
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                time.sleep(0.05)
        try:
            yield
        finally:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(fh, fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)


def terminate_process(pid: int, *, force: bool) -> None:
    """Terminate `pid`. POSIX: `SIGTERM` when `force=False`, `SIGKILL`
    when `force=True` — unchanged two-step escalation. Windows: always
    `os.kill(pid, signal.SIGTERM)` — Windows' `os.kill` already maps
    `SIGTERM` straight to `TerminateProcess`, an unconditional hard kill,
    so there's no softer/harder distinction to escalate through, and
    `signal.SIGKILL` doesn't exist in the `signal` module there at all.
    """
    if _WINDOWS:
        os.kill(pid, signal.SIGTERM)
        return
    os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)


def new_process_group_kwargs() -> dict:
    """Extra `subprocess.Popen` kwargs so a spawned process isn't tied to
    the parent's console/session and can outlive the call that started it
    — needed for the shared per-domain `cloudflared` process
    (`tunnel_manager._ensure_shared_tunnel_running`), which must keep
    running after the `serve`/`proxy` invocation that happened to start
    it exits. POSIX: `start_new_session=True` (`setsid()`). Windows:
    `creationflags=CREATE_NEW_PROCESS_GROUP` — `start_new_session` isn't
    a valid `Popen` kwarg on Windows at all.
    """
    if _WINDOWS:
        import subprocess

        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}
