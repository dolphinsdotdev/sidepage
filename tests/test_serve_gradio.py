"""Integration tests for Gradio support in `sidepage serve`, against the
real `tests/fixtures/gradio-app` and `tests/fixtures/gradio-guarded-app`
fixtures.

The first fixture's `demo.launch(server_port=8123)` sits at module level,
*unguarded* — the shape Gradio's own examples use — so importing it
naively would block forever on Gradio's blocking server, and its hardcoded
port would ignore anything sidepage injected through the environment.
These tests confirm `sidepage serve` neutralizes that call and serves the
app on its own allocated port instead, through the actual reverse proxy.

No `gradio_client` is used here: `gradio` is installed inside the
subprocess's own `uv run` environment, not this test environment, so the
prediction round trip goes over Gradio's plain two-call REST API with
`httpx` (already a project dependency) — `POST /gradio_api/call/<endpoint>`
returns an `event_id`, and `GET /gradio_api/call/<endpoint>/<event_id>`
streams the result as SSE. Same reasoning as `test_serve_mcp.py` speaking
raw JSON-RPC rather than pulling in an MCP client.

These are slow by nature: the first run resolves and downloads Gradio
through `uv`, which is why the readiness timeouts here are much larger
than the ones in the Streamlit/FastAPI suites.
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

# The port the unguarded fixture hardcodes in its own `launch()` call.
# Nothing should ever be listening here — if something is, sidepage
# executed the script's own launch instead of bypassing it.
FIXTURE_HARDCODED_PORT = 8123

# Gradio arrives through `uv run --with gradio`; a cold resolve of it and
# its dependency tree is genuinely slow the first time.
READY_TIMEOUT = 180.0


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


def _wait_for_gradio_ui(base_url: str, *, timeout: float) -> httpx.Response:
    """Poll until the real Gradio UI answers, not sidepage's own "app is
    booting" holding page — the proxy serves that with a 200 too, so the
    status code alone proves nothing. Gradio's index always carries its
    own `gradio_config` bootstrap block."""
    deadline = time.monotonic() + timeout
    last: httpx.Response | None = None
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(base_url, timeout=10)
        except httpx.TransportError:
            time.sleep(0.5)
            continue
        last = resp
        if resp.status_code == 200 and "gradio_config" in resp.text:
            return resp
        time.sleep(0.5)
    detail = f"last status {last.status_code}" if last is not None else "no response"
    raise TimeoutError(f"Gradio UI at {base_url} never became ready within {timeout}s ({detail})")


def _predict(base_url: str, endpoint: str, data: list) -> list:
    """One full prediction over Gradio's two-call REST API, through the
    proxy: POST to get an `event_id`, then read the SSE stream until the
    `complete` event carries the result."""
    join = httpx.post(
        f"{base_url}/gradio_api/call/{endpoint}",
        json={"data": data},
        timeout=15,
    )
    join.raise_for_status()
    event_id = join.json()["event_id"]

    with httpx.stream(
        "GET", f"{base_url}/gradio_api/call/{endpoint}/{event_id}", timeout=30
    ) as stream:
        stream.raise_for_status()
        for line in stream.iter_lines():
            if line.startswith("data:"):
                payload = line[len("data:") :].strip()
                if payload and payload != "null":
                    return json.loads(payload)
    raise AssertionError(f"no result event for {endpoint!r} on {base_url}")


def test_gradio_served_despite_unguarded_launch(sidepage_home: Path) -> None:
    """The flagship claim: a script whose module-level `demo.launch()`
    hardcodes its own port is still served, on sidepage's allocated port,
    because sidepage neutralizes that call before importing the module."""
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve([str(FIXTURES / "gradio-app" / "app.py"), "--name", "gradio-ui"], env=env)
    try:
        entry = _wait_for_registry_entry(sidepage_home, "gradio-ui", timeout=READY_TIMEOUT)
        resp = _wait_for_gradio_ui(entry["url"], timeout=READY_TIMEOUT)
        assert resp.status_code == 200

        # The script's own hardcoded port must never have been bound —
        # that's the difference between bypassing `launch()` and running it.
        with pytest.raises(httpx.TransportError):
            httpx.get(f"http://127.0.0.1:{FIXTURE_HARDCODED_PORT}/", timeout=2)
    finally:
        _stop("gradio-ui", env)
        proc.wait(timeout=15)


def test_gradio_prediction_round_trip(sidepage_home: Path) -> None:
    """Full functional round trip, not just that the page renders: run the
    fixture's `greet` function through Gradio's queue and check the real
    returned value, through the real proxy."""
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve([str(FIXTURES / "gradio-app" / "app.py"), "--name", "gradio-call"], env=env)
    try:
        entry = _wait_for_registry_entry(sidepage_home, "gradio-call", timeout=READY_TIMEOUT)
        _wait_for_gradio_ui(entry["url"], timeout=READY_TIMEOUT)
        assert _predict(entry["url"], "greet", ["sidepage"]) == ["Hello sidepage!"]
    finally:
        _stop("gradio-call", env)
        proc.wait(timeout=15)


def test_gradio_factory_script_is_served(sidepage_home: Path) -> None:
    """The script shape that broke the first version of this wrapper: the
    Blocks is built by a factory and `launch()` is called on its result
    inside a `__main__` guard, so nothing at module level ever holds one.
    Found by pulling a real Space
    (`JacobPEvans/mlx-benchmarks-viewer`) and watching it fail."""
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve(
        [str(FIXTURES / "gradio-factory-app" / "app.py"), "--name", "gradio-factory"], env=env
    )
    try:
        entry = _wait_for_registry_entry(sidepage_home, "gradio-factory", timeout=READY_TIMEOUT)
        _wait_for_gradio_ui(entry["url"], timeout=READY_TIMEOUT)
        assert _predict(entry["url"], "echo", ["hi"]) == ["echo: hi"]

        # The factory's own hardcoded port is neutralized like any other.
        with pytest.raises(httpx.TransportError):
            httpx.get("http://127.0.0.1:8124/", timeout=2)
    finally:
        _stop("gradio-factory", env)
        proc.wait(timeout=15)


def test_gradio_script_without_launch_resolves_demo_by_name(sidepage_home: Path) -> None:
    """Last-resort fallback: a script that never calls `launch()` at all,
    so there's nothing to capture. The wrapper scans the namespace the run
    produced — and this fixture defines a second, unrelated Blocks, so it
    has to prefer the one named `demo` rather than taking whatever it
    finds first."""
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve(
        [str(FIXTURES / "gradio-guarded-app" / "app.py"), "--name", "gradio-guarded"], env=env
    )
    try:
        entry = _wait_for_registry_entry(sidepage_home, "gradio-guarded", timeout=READY_TIMEOUT)
        _wait_for_gradio_ui(entry["url"], timeout=READY_TIMEOUT)
        assert _predict(entry["url"], "shout", ["hello"]) == ["HELLO"]
    finally:
        _stop("gradio-guarded", env)
        proc.wait(timeout=15)


def test_launch_css_is_forwarded_to_the_mounted_app(sidepage_home: Path) -> None:
    """Gradio 6 moved `css` off `Blocks` onto `launch()`/`mount_gradio_app()`.
    sidepage neutralizes `launch()` and mounts the Blocks itself, so unless
    the captured kwargs are forwarded, every Space that styles itself the
    documented way is served unstyled — silently. Found while restyling a
    real app and noticing `Blocks(css=...)` had become a no-op."""
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve(
        [str(FIXTURES / "gradio-factory-app" / "app.py"), "--name", "gradio-css"], env=env
    )
    try:
        entry = _wait_for_registry_entry(sidepage_home, "gradio-css", timeout=READY_TIMEOUT)
        resp = _wait_for_gradio_ui(entry["url"], timeout=READY_TIMEOUT)
        assert "sidepage-css-marker" in resp.text, "launch(css=...) was dropped by the wrapper"
    finally:
        _stop("gradio-css", env)
        proc.wait(timeout=15)


def test_gradio_wrapper_is_cleaned_up_on_teardown(sidepage_home: Path) -> None:
    """The generated wrapper module is per-run scratch state, not
    something left behind in the user's state directory after the app
    stops — same contract the MCP wrapper already has."""
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    wrapper = sidepage_home / "state" / "wrappers" / "_sidepage_gradio_wrapper_gradio-clean.py"
    proc = _run_serve([str(FIXTURES / "gradio-app" / "app.py"), "--name", "gradio-clean"], env=env)
    try:
        _wait_for_registry_entry(sidepage_home, "gradio-clean", timeout=READY_TIMEOUT)
        assert wrapper.exists(), "wrapper module should exist while the app is running"
    finally:
        _stop("gradio-clean", env)
        proc.wait(timeout=15)
    assert not wrapper.exists(), "wrapper module should be removed at teardown"
