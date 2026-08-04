"""Integration tests for FastAPI/uvicorn support in `sidepage serve`,
against the real `tests/fixtures/fastapi-app` fixture.

The fixture's own `__main__` block hardcodes port 9999 — deliberately,
matching real-world FastAPI apps (e.g. the one that prompted this feature)
that only ever expect to be launched via `uvicorn module:app`, not by
running the script directly. These tests confirm sidepage launches via the
`uvicorn` CLI and actually lands on its own allocated port, not 9999.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SIDEPAGE_BIN = str(Path(sys.executable).parent / "sidepage")


@pytest.fixture
def sidepage_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SIDEPAGE_HOME", str(tmp_path))
    return tmp_path


def _wait_for_registry_entry(sidepage_home: Path, name: str, *, timeout: float) -> dict:
    registry_file = sidepage_home / "state" / "running_apps.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if registry_file.exists():
            try:
                data = json.loads(registry_file.read_text())
            except json.JSONDecodeError:
                data = {}
            if name in data:
                return data[name]
        time.sleep(0.2)
    raise TimeoutError(f"{name!r} never appeared in the registry within {timeout}s")


def _run_serve(args: list[str], env: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(
        [SIDEPAGE_BIN, "serve", *args],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _stop(name: str, env: dict[str, str]) -> None:
    subprocess.run([SIDEPAGE_BIN, "stop", name], env=env, capture_output=True, timeout=15)


def _poll_until_ready(url: str, *, timeout: float) -> httpx.Response:
    deadline = time.monotonic() + timeout
    last: httpx.Response | None = None
    while time.monotonic() < deadline:
        try:
            last = httpx.get(url, timeout=5)
            if "starting…" not in last.text:
                return last
        except httpx.TransportError:
            pass
        time.sleep(0.5)
    assert last is not None, f"never got any response from {url}"
    return last


def test_fastapi_uses_allocated_port_not_hardcoded(sidepage_home: Path) -> None:
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve([str(FIXTURES / "fastapi-app" / "app.py"), "--name", "fa-port"], env=env)
    try:
        entry = _wait_for_registry_entry(sidepage_home, "fa-port", timeout=20)
        assert entry["listen_port"] != 9999  # the fixture's hardcoded __main__ port
        resp = _poll_until_ready(entry["url"], timeout=15)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
    finally:
        _stop("fa-port", env)
        proc.wait(timeout=15)


def test_fastapi_docs_route(sidepage_home: Path) -> None:
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve([str(FIXTURES / "fastapi-app" / "app.py"), "--name", "fa-docs"], env=env)
    try:
        entry = _wait_for_registry_entry(sidepage_home, "fa-docs", timeout=20)
        _poll_until_ready(entry["url"], timeout=15)  # wait for readiness first

        docs = httpx.get(f"{entry['url']}/docs", timeout=5)
        assert docs.status_code == 200
        assert "swagger" in docs.text.lower()

        openapi = httpx.get(f"{entry['url']}/openapi.json", timeout=5)
        assert openapi.status_code == 200
        assert "openapi" in openapi.json()
    finally:
        _stop("fa-docs", env)
        proc.wait(timeout=15)


def test_fastapi_post_endpoint(sidepage_home: Path) -> None:
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve([str(FIXTURES / "fastapi-app" / "app.py"), "--name", "fa-post"], env=env)
    try:
        entry = _wait_for_registry_entry(sidepage_home, "fa-post", timeout=20)
        _poll_until_ready(entry["url"], timeout=15)

        resp = httpx.post(f"{entry['url']}/echo", json={"message": "hello"}, timeout=5)
        assert resp.status_code == 200
        assert resp.json() == {"echo": "hello"}
    finally:
        _stop("fa-post", env)
        proc.wait(timeout=15)


def test_fastapi_docs_gated_by_auth_token_too(sidepage_home: Path) -> None:
    """No special-casing: the proxy's auth gate covers /docs the same as
    any other path, same as every other auth-tier decision this project
    has made."""
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve(
        [
            str(FIXTURES / "fastapi-app" / "app.py"),
            "--name",
            "fa-auth",
            "--auth",
            "token",
            "--token",
            "fa-secret",
        ],
        env=env,
    )
    try:
        entry = _wait_for_registry_entry(sidepage_home, "fa-auth", timeout=20)

        blocked = httpx.get(f"{entry['url']}/docs", timeout=5)
        assert blocked.status_code == 401

        allowed = httpx.get(
            f"{entry['url']}/docs", headers={"Authorization": "Bearer fa-secret"}, timeout=5
        )
        assert allowed.status_code == 200
    finally:
        _stop("fa-auth", env)
        proc.wait(timeout=15)
