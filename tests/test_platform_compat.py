"""Tests for `sidepage.core._platform` — the cross-platform substitutes
for the POSIX-only primitives (`fcntl`, `os.kill(pid, 0)` as a liveness
probe, `signal.SIGKILL`, `start_new_session`) the rest of `sidepage.core`
used to reach for directly. See that module's docstring for why each one
had to change, not just gain a Windows branch.

Runs whichever platform's branch this test process is actually on — the
Windows-specific internals (`ctypes.windll`, `msvcrt`) can't be exercised
from a POSIX runner at all (those modules don't even import there), so
this file pins the contract on POSIX and relies on the real-Windows smoke
test (see `docs/CHECKLIST.md`) for the rest. `tests/test_windows_stop.py`
covers `sidepage.core.process.stop()`'s Windows branch, which — unlike
`is_pid_alive`/`lock_exclusive` — has no OS-specific code of its own and
so *can* be exercised for real on any platform by forcing `sys.platform`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from sidepage.core import _platform


def test_is_pid_alive_true_for_self() -> None:
    assert _platform.is_pid_alive(os.getpid()) is True


def test_is_pid_alive_false_for_dead_pid() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    assert _platform.is_pid_alive(proc.pid) is False


def test_is_pid_alive_is_a_pure_probe_not_a_kill() -> None:
    """Regression test for the finding that motivated this module: a naive
    port of `os.kill(pid, 0)` to Windows would have *killed* the process
    being checked — Windows' `os.kill` has no signal-0-as-probe concept,
    any value other than CTRL_C/CTRL_BREAK/SIGTERM maps straight to
    `TerminateProcess`. Can't exercise the Windows branch itself from
    here, but this pins the actual contract on whichever platform runs
    it: checking liveness must never end the process being checked.
    """
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        assert _platform.is_pid_alive(proc.pid) is True
        time.sleep(0.2)
        assert proc.poll() is None, "is_pid_alive killed the process it was checking"
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_lock_exclusive_serializes_two_threads(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    order: list[str] = []
    first_acquired = threading.Event()

    def hold_first() -> None:
        with open(lock_path, "w") as fh, _platform.lock_exclusive(fh):
            order.append("first-acquired")
            first_acquired.set()
            time.sleep(0.3)
            order.append("first-released")

    def try_second() -> None:
        first_acquired.wait(timeout=5)
        with open(lock_path, "w") as fh, _platform.lock_exclusive(fh):
            order.append("second-acquired")

    t1 = threading.Thread(target=hold_first)
    t2 = threading.Thread(target=try_second)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert order == ["first-acquired", "first-released", "second-acquired"]


def test_terminate_process_soft_then_hard() -> None:
    soft = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _platform.terminate_process(soft.pid, force=False)
        soft.wait(timeout=5)
        assert soft.poll() is not None
    finally:
        if soft.poll() is None:
            soft.kill()
            soft.wait(timeout=5)

    hard = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _platform.terminate_process(hard.pid, force=True)
        hard.wait(timeout=5)
        assert hard.poll() is not None
    finally:
        if hard.poll() is None:
            hard.kill()
            hard.wait(timeout=5)


def test_new_process_group_kwargs_spawns_successfully() -> None:
    kwargs = _platform.new_process_group_kwargs()
    proc = subprocess.Popen([sys.executable, "-c", "pass"], **kwargs)
    proc.wait(timeout=5)
    assert proc.returncode == 0
