"""Integration tests for notebook (Jupyter Lab) support in `sidepage
serve`, against the real `tests/fixtures/notebook-app` fixture.

The flagship test here (`test_notebook_kernel_execution_round_trip`) is
the one that actually matters: Jupyter Server rejects cross-origin
requests and WebSocket upgrades by default, and the browser's `Origin`
through Sidepage's proxy is the *proxy's* address, not Jupyter's own
(different) upstream port — a mismatch that gets rejected without the
`--ServerApp.allow_origin=*`/`--ServerApp.disable_check_xsrf=True` flags
`sidepage.core.notebook.build_jupyter_launch_command` passes (verified
live against a real Jupyter Server before committing to that design — see
that module's docstring). This test reproduces the real shape of that
risk: it starts a kernel and opens the WebSocket *through the actual
sidepage proxy port*, deliberately sending the proxy's own origin (what a
real browser would send), and checks a real `execute_request` gets a real
stdout reply back — not just that the server started.

No Jupyter client library is used: the kernel WebSocket protocol is
exercised directly over `websockets` (already a project dependency, used
by the reverse proxy itself) and `httpx`, the same "raw protocol, no SDK"
approach `test_serve_mcp.py` uses for MCP.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import websockets

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


def _poll_until_ready(
    url: str, *, timeout: float, headers: dict[str, str] | None = None
) -> httpx.Response:
    deadline = time.monotonic() + timeout
    last: httpx.Response | None = None
    while time.monotonic() < deadline:
        try:
            last = httpx.get(url, headers=headers, timeout=5)
            if "starting…" not in last.text:
                return last
        except httpx.TransportError:
            pass
        time.sleep(0.5)
    assert last is not None, f"never got any response from {url}"
    return last


async def _execute_and_collect_stream_text(base_url: str, *, code: str) -> str:
    """Start a real kernel and run `code` through it, over a WebSocket
    opened against `base_url` (expected to be the sidepage proxy's own
    URL, not Jupyter's real upstream port) — returns the concatenated
    stdout stream text from the execution."""
    resp = httpx.post(f"{base_url}/api/kernels", json={"name": "python3"}, timeout=10)
    resp.raise_for_status()
    kernel_id = resp.json()["id"]

    ws_url = base_url.replace("http://", "ws://", 1) + f"/api/kernels/{kernel_id}/channels"
    # Deliberately the *proxy's* own origin — exactly what a real browser
    # sends, and exactly the mismatch (vs. Jupyter's real upstream port)
    # that would get rejected without the launch flags this test exists
    # to prove work.
    async with websockets.connect(ws_url, additional_headers={"Origin": base_url}) as ws:
        await ws.send(
            json.dumps(
                {
                    "header": {
                        "msg_id": "sidepage-test",
                        "msg_type": "execute_request",
                        "username": "sidepage-test",
                        "session": "sidepage-test-session",
                        "version": "5.3",
                    },
                    "parent_header": {},
                    "metadata": {},
                    "content": {"code": code, "silent": False},
                    "channel": "shell",
                }
            )
        )
        stream_text = ""
        for _ in range(30):
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if message.get("msg_type") == "stream":
                stream_text += message["content"]["text"]
            if message.get("msg_type") == "execute_reply":
                break
        return stream_text


def test_notebook_lab_ui_reachable(sidepage_home: Path) -> None:
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve(
        [str(FIXTURES / "notebook-app" / "notebook.ipynb"), "--name", "nb-ui"], env=env
    )
    try:
        entry = _wait_for_registry_entry(sidepage_home, "nb-ui", timeout=30)
        resp = _poll_until_ready(f"{entry['url']}/lab", timeout=30)
        assert resp.status_code == 200
        assert "jupyter" in resp.text.lower()
    finally:
        _stop("nb-ui", env)
        proc.wait(timeout=15)


def test_notebook_kernel_execution_round_trip(sidepage_home: Path) -> None:
    """The flagship claim: a real kernel, started and driven entirely
    through the sidepage proxy (not talking to Jupyter's own upstream
    port directly), actually executes code and returns real output —
    proving the origin/XSRF relaxation in the launch command survives
    the exact mismatch a real browser-through-proxy setup produces."""
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve(
        [str(FIXTURES / "notebook-app" / "notebook.ipynb"), "--name", "nb-exec"], env=env
    )
    try:
        entry = _wait_for_registry_entry(sidepage_home, "nb-exec", timeout=30)
        _poll_until_ready(f"{entry['url']}/lab", timeout=30)  # wait for readiness first

        stream_text = asyncio.run(
            _execute_and_collect_stream_text(entry["url"], code="print('hello from sidepage')")
        )
        assert stream_text == "hello from sidepage\n"
    finally:
        _stop("nb-exec", env)
        proc.wait(timeout=15)


def test_notebook_gated_by_auth_token_too(sidepage_home: Path) -> None:
    """No special-casing: the proxy's auth gate covers the Lab UI and its
    API the same as any other path, same as FastAPI's /docs and MCP's
    /mcp — every auth-tier decision this project has made."""
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve(
        [
            str(FIXTURES / "notebook-app" / "notebook.ipynb"),
            "--name",
            "nb-auth",
            "--auth",
            "token",
            "--token",
            "nb-secret",
        ],
        env=env,
    )
    try:
        entry = _wait_for_registry_entry(sidepage_home, "nb-auth", timeout=30)

        blocked = httpx.get(f"{entry['url']}/lab", timeout=5)
        assert blocked.status_code == 401

        allowed = _poll_until_ready(
            f"{entry['url']}/lab",
            timeout=30,
            headers={"Authorization": "Bearer nb-secret"},
        )
        assert allowed.status_code == 200
    finally:
        _stop("nb-auth", env)
        proc.wait(timeout=15)
