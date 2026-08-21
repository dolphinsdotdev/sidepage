"""Tests for `sidepage.core.registry` — specifically `is_alive`'s zombie
detection (a plain `os.kill(pid, 0)` succeeds for a zombie pid too, since
POSIX lets you signal one without error; `is_alive` has to distinguish
that from a genuinely-running process, or a dead-but-unreaped app would
show up in `sidepage ls` forever and confuse `sidepage stop` into
reporting a false "didn't stop within 10s").

Zombies are produced for real via `os.fork()` + `os._exit()` in the child
without the test process reaping it until after the assertion — the only
reliable way to get a real zombie state to test against. Zombies are a
POSIX-only concept (no `os.fork()` on Windows at all), so every test here
that relies on one is skipped there — `sidepage.core.registry.is_alive`
itself is still exercised on Windows via `sidepage.core._platform
.is_pid_alive`, just not through this fork-a-zombie technique. See
`tests/test_platform_compat.py` for the cross-platform liveness-probe
coverage.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sidepage.core import process, registry


@pytest.fixture
def sidepage_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SIDEPAGE_HOME", str(tmp_path))
    return tmp_path


def _wait_until_zombie(pid: int, timeout: float = 2.0) -> None:
    """Poll via `ps` (never `waitpid` — that would reap it and defeat the
    point) until `pid` shows Z state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
        )
        if result.stdout.strip().startswith("Z"):
            return
        time.sleep(0.02)
    raise TimeoutError(f"pid {pid} never became a zombie within {timeout}s")


@pytest.fixture
def zombie_pid():
    """Forks a child that exits immediately and is deliberately left
    unreaped until the test is done with it, so `os.kill(pid, 0)` still
    succeeds against it (matching the real bug) while it's a true zombie,
    not just a dead pid."""
    if sys.platform == "win32":
        pytest.skip("os.fork isn't available on Windows")
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    _wait_until_zombie(pid)
    try:
        yield pid
    finally:
        os.waitpid(pid, 0)  # reap it for real so nothing lingers after the test


def _make_app(name: str, pid: int) -> registry.RunningApp:
    return registry.RunningApp(
        name=name,
        pid=pid,
        target="t",
        target_kind="static",
        listen_port=1234,
        url="http://127.0.0.1:1234",
        tunnel_url=None,
        started_at=0.0,
    )


# --- is_alive ---


def test_is_alive_true_for_running_process() -> None:
    assert registry.is_alive(os.getpid()) is True


@pytest.mark.skipif(sys.platform == "win32", reason="os.fork isn't available on Windows")
def test_is_alive_false_for_nonexistent_pid() -> None:
    # A forked-then-reaped pid is guaranteed gone, unlike a made-up large
    # number which could theoretically collide with something real.
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
    assert registry.is_alive(pid) is False


def test_is_alive_false_for_zombie_process(zombie_pid: int) -> None:
    # The bug this guards against: a bare `os.kill(pid, 0)` would report
    # this pid as alive, since POSIX allows signaling a zombie.
    assert registry.is_alive(zombie_pid) is False


# --- list_running: pruning now covers zombies too ---


def test_list_running_prunes_zombie_entry(sidepage_home: Path, zombie_pid: int) -> None:
    registry.register(_make_app("zombie-app", zombie_pid))
    assert registry.list_running(prune_dead=True) == []
    assert registry.get("zombie-app") is None


def test_list_running_keeps_genuinely_alive_entry(sidepage_home: Path) -> None:
    registry.register(_make_app("alive-app", os.getpid()))
    apps = registry.list_running(prune_dead=True)
    assert [a.name for a in apps] == ["alive-app"]


# --- process.stop: the zombie case now takes the stale-entry path ---


def test_stop_zombie_app_reports_stale_not_timeout(
    sidepage_home: Path, zombie_pid: int, capsys: pytest.CaptureFixture[str]
) -> None:
    registry.register(_make_app("zombie-app", zombie_pid))

    process.stop("zombie-app")  # must not raise SystemExit

    assert registry.get("zombie-app") is None
    output = capsys.readouterr().err
    assert "wasn't actually running" in output
    assert "didn't stop" not in output


@pytest.mark.skipif(sys.platform == "win32", reason="os.fork isn't available on Windows")
def test_stop_fully_gone_app_reports_stale(sidepage_home: Path) -> None:
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)  # fully reaped — not a zombie, just gone

    registry.register(_make_app("gone-app", pid))
    process.stop("gone-app")

    assert registry.get("gone-app") is None


def test_stop_unknown_app_exits_nonzero(sidepage_home: Path) -> None:
    with pytest.raises(SystemExit) as exc_info:
        process.stop("does-not-exist")
    assert exc_info.value.code == 1
