"""Integration tests for MCP support in `sidepage serve`, against the real
`tests/fixtures/mcp-app` fixture.

The fixture's own `__main__` block calls `mcp.run()` with no transport —
stdio by default, deliberately not wired for HTTP at all (mirroring
`tests/fixtures/fastapi-app`'s hardcoded port). These tests confirm
`sidepage serve` bypasses that entrypoint entirely and serves the fixture
over real Streamable HTTP instead, through the actual reverse proxy — not
by talking to the wrapped subprocess directly.

No MCP client library is used here: the JSON-RPC/Streamable-HTTP protocol
is exercised directly over `httpx` (already a project dependency), the
same way `test_serve_fastapi.py` issues raw HTTP requests rather than
pulling in a FastAPI test client. `data: `-prefixed SSE framing and the
`Mcp-Session-Id` response header are handled by hand for the same reason —
one less moving part, and it's exactly what a real caller has to do
against the Streamable HTTP transport.
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

_MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


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


def _parse_sse_json(body: str) -> dict:
    """Streamable HTTP wraps even a single JSON-RPC response in one SSE
    frame (`event: ...\\ndata: {...}`) once the client sends `Accept:
    text/event-stream` — pull the JSON back out of the last `data:` line.
    """
    lines = [line[len("data:") :].strip() for line in body.splitlines() if line.startswith("data:")]
    assert lines, f"no SSE `data:` line found in response body: {body!r}"
    return json.loads(lines[-1])


def _mcp_initialize(base_url: str, *, headers: dict[str, str] | None = None) -> tuple[dict, str]:
    """Real handshake: POST `initialize`, then the required
    `notifications/initialized` follow-up. Returns (initialize result,
    session id) for use by later calls."""
    all_headers = {**_MCP_HEADERS, **(headers or {})}
    resp = httpx.post(
        f"{base_url}/mcp",
        headers=all_headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "sidepage-test", "version": "0"},
            },
        },
        timeout=10,
    )
    resp.raise_for_status()
    session_id = resp.headers["mcp-session-id"]
    result = _parse_sse_json(resp.text)

    ack = httpx.post(
        f"{base_url}/mcp",
        headers={**all_headers, "Mcp-Session-Id": session_id},
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        timeout=10,
    )
    assert ack.status_code == 202, ack.text
    return result, session_id


def _wait_for_mcp_ready(
    base_url: str, *, timeout: float, headers: dict[str, str] | None = None
) -> tuple[dict, str]:
    """`_mcp_initialize` fails against the holding page (not real MCP JSON)
    while the wrapped process is still starting — retry until it's up."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return _mcp_initialize(base_url, headers=headers)
        except Exception as exc:  # noqa: BLE001 — just a readiness retry loop
            last_exc = exc
            time.sleep(0.5)
    raise TimeoutError(f"MCP endpoint at {base_url} never became ready: {last_exc}")


def test_mcp_reachable_over_http_despite_stdio_main(sidepage_home: Path) -> None:
    """The flagship claim: a script whose own __main__ only ever calls
    mcp.run() (stdio, no HTTP) is still a real, reverse-proxied HTTP MCP
    server — because sidepage never executes that __main__ block at all."""
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve([str(FIXTURES / "mcp-app" / "app.py"), "--name", "mcp-init"], env=env)
    try:
        entry = _wait_for_registry_entry(sidepage_home, "mcp-init", timeout=20)
        result, session_id = _wait_for_mcp_ready(entry["url"], timeout=20)
        assert result["result"]["serverInfo"]["name"] == "fixture-mcp-server"
        assert session_id
    finally:
        _stop("mcp-init", env)
        proc.wait(timeout=15)


def test_mcp_tool_call_round_trip(sidepage_home: Path) -> None:
    """Full functional round trip, not just the handshake: initialize,
    then actually invoke the fixture's `add` tool and check the real
    result, through the real proxy."""
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve([str(FIXTURES / "mcp-app" / "app.py"), "--name", "mcp-tool"], env=env)
    try:
        entry = _wait_for_registry_entry(sidepage_home, "mcp-tool", timeout=20)
        _, session_id = _wait_for_mcp_ready(entry["url"], timeout=20)

        resp = httpx.post(
            f"{entry['url']}/mcp",
            headers={**_MCP_HEADERS, "Mcp-Session-Id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "add", "arguments": {"a": 3, "b": 4}},
            },
            timeout=10,
        )
        resp.raise_for_status()
        result = _parse_sse_json(resp.text)
        assert result["result"]["structuredContent"] == {"result": 7}
        assert result["result"]["isError"] is False
    finally:
        _stop("mcp-tool", env)
        proc.wait(timeout=15)


def test_mcp_endpoint_reachable_with_non_loopback_host_header(sidepage_home: Path) -> None:
    """Regression test for a real bug: both recognized MCP packages
    auto-enable Host/Origin allowlisting that only accepts
    `127.0.0.1`/`localhost` by default (the official SDK whenever
    `streamable_http_app()` defaults to `host="127.0.0.1"`), but a request
    reaching this endpoint through Sidepage's reverse proxy never actually
    carries that `Host` — the proxy forwards the real inbound `Host`
    literally (Caddy-style `override_host`, see
    `sidepage.core.reverse_proxy`), which is always the public `--domain`/
    `--anon` hostname once traffic comes back through a real tunnel. That
    used to 421 with `Invalid Host header` on *every* real request, not
    just an edge case — see `sidepage.core.process._write_mcp_host_wrapper`
    for the fix (a generated wrapper module that disables the package's
    own Host/Origin check, since Sidepage's own reverse proxy — loopback-
    only upstream, `--auth` gate in front — is already the real trust
    boundary).

    `entry["url"]` is `http://127.0.0.1:<port>` for a local (non-`--domain`/
    `--anon`) serve, so httpx's own default `Host` header would happen to
    pass the allowlist and mask the bug entirely — the explicit `Host`
    override below is what actually exercises the tunnel-forwarded case.
    """
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve([str(FIXTURES / "mcp-app" / "app.py"), "--name", "mcp-host"], env=env)
    try:
        entry = _wait_for_registry_entry(sidepage_home, "mcp-host", timeout=20)
        # Establish readiness against the default (loopback) Host first, so
        # a slow-starting upstream isn't mistaken for the bug under test.
        _wait_for_mcp_ready(entry["url"], timeout=20)

        result, session_id = _mcp_initialize(
            entry["url"], headers={"Host": "mcp-host.dolphins.dev"}
        )
        assert result["result"]["serverInfo"]["name"] == "fixture-mcp-server"
        assert session_id
    finally:
        _stop("mcp-host", env)
        proc.wait(timeout=15)


def test_mcp_endpoint_gated_by_auth_token_too(sidepage_home: Path) -> None:
    """No special-casing: the proxy's auth gate covers /mcp the same as
    any other path, same as FastAPI's /docs (test_serve_fastapi.py) and
    every other auth-tier decision this project has made."""
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve(
        [
            str(FIXTURES / "mcp-app" / "app.py"),
            "--name",
            "mcp-auth",
            "--auth",
            "token",
            "--token",
            "mcp-secret",
        ],
        env=env,
    )
    try:
        entry = _wait_for_registry_entry(sidepage_home, "mcp-auth", timeout=20)

        blocked = httpx.post(
            f"{entry['url']}/mcp",
            headers=_MCP_HEADERS,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            timeout=10,
        )
        assert blocked.status_code == 401

        result, session_id = _wait_for_mcp_ready(
            entry["url"], timeout=20, headers={"Authorization": "Bearer mcp-secret"}
        )
        assert result["result"]["serverInfo"]["name"] == "fixture-mcp-server"
        assert session_id
    finally:
        _stop("mcp-auth", env)
        proc.wait(timeout=15)
