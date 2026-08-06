"""MCP server that drives the `sidepage` CLI itself — a tool-shaped mirror
of the `sidepage-serve` skill (docs/../skills), for driving sidepage from
an MCP client instead of a shell-capable agent.

Exploratory fixture, not wired into the product: lives in `tests/fixtures`
so it can be exercised the same way `tests/fixtures/mcp-app` is — served
directly (`sidepage serve tests/fixtures/mcp-control-app/app.py`), which
also means it can control the very sidepage instance that's serving it.

Same shape as the skill it mirrors:

- `sidepage serve <target>` blocks in the foreground and only exits on
  Ctrl+C or `sidepage stop`, so `sidepage_serve_new` / `sidepage_serve_registered`
  launch it detached (`start_new_session=True`, the Python analog of
  `nohup ... & disown`) and poll the log for a URL or an error, capping
  the wait the same way `scripts/start_site.sh` does — rather than a tool
  call that never returns.
- Every other command (`ls`, `status`, `usage`, `app register|list|show|unregister`,
  `secrets list`) returns immediately, so those tools just shell out and
  hand back exit code + output.

Like `tests/fixtures/mcp-app`, `__main__` calls `mcp.run()` (stdio) and is
never actually reached when sidepage wraps this script — sidepage calls
`.streamable_http_app()` directly instead (see `sidepage.core.process`,
`CodeLauncher.MCP`).
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from mcp.server import MCPServer

mcp = MCPServer("sidepage-control-mcp-server")

SIDEPAGE_BIN = "sidepage"  # assumed on PATH, same assumption the skill makes

LOG_DIR = Path(os.environ.get("SIDEPAGE_MCP_LOG_DIR", "~/.local/state/sidepage/mcp-logs")).expanduser()

_URL_RE = re.compile(
    r"https?://[^\s]+\.trycloudflare\.com[^\s]*"
    r"|https?://[a-zA-Z0-9.-]+/[^\s]*"
    r"|http://127\.0\.0\.1:[0-9]+[^\s]*"
)
_ERROR_RE = re.compile(r"error|traceback|failed to", re.IGNORECASE)


def _run_direct(argv: list[str]) -> dict:
    """Run a sidepage subcommand that returns immediately and hand back
    its exit code and combined output — mirrors calling `ls`/`status`/
    `usage`/`app *`/`secrets list` straight from a shell."""
    proc = subprocess.run(
        [SIDEPAGE_BIN, *argv],
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    return {"exit_code": proc.returncode, "output": output.strip()}


def _start_background(serve_args: list[str], *, app_name: str) -> dict:
    """Launch `sidepage serve <serve_args>` detached and poll its log for a
    URL or an error, same algorithm as `scripts/start_site.sh`."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"{app_name}.log"
    log_handle = log_file.open("w")

    proc = subprocess.Popen(
        [SIDEPAGE_BIN, "serve", *serve_args],
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    status = "starting"
    url = ""
    max_wait = 30
    waited = 0
    while waited < max_wait:
        if proc.poll() is not None:
            status = "failed"
            break

        text = log_file.read_text(errors="replace") if log_file.exists() else ""
        matches = _URL_RE.findall(text)
        if matches:
            url = matches[-1]
            status = "running"
            break
        if _ERROR_RE.search(text):
            status = "failed"
            break

        time.sleep(1)
        waited += 1

    if status == "running":
        return {"status": "running", "app": app_name, "pid": proc.pid, "log": str(log_file), "url": url}
    if status == "failed":
        tail = log_file.read_text(errors="replace").splitlines()[-20:] if log_file.exists() else []
        return {
            "status": "failed",
            "app": app_name,
            "pid": proc.pid,
            "log": str(log_file),
            "error": " ".join(tail).strip(),
        }
    return {
        "status": "starting",
        "app": app_name,
        "pid": proc.pid,
        "log": str(log_file),
        "note": f"no URL detected after {max_wait}s; check the log or call sidepage_status({app_name!r})",
    }


@mcp.tool()
def sidepage_serve_new(name: str, target: str, flags: str = "") -> dict:
    """Serve a fresh (not yet registered) target under `name`, e.g.
    flags="--auth token --anon". Backgrounds the blocking `serve` call and
    reports back once it's running, failed, or still starting."""
    return _start_background([target, "--name", name, *shlex.split(flags)], app_name=name)


@mcp.tool()
def sidepage_serve_registered(name: str, flags: str = "") -> dict:
    """Serve an app already saved via `sidepage app register`, by name.
    `flags` override the registration for this run only."""
    return _start_background([name, *shlex.split(flags)], app_name=name)


@mcp.tool()
def sidepage_stop(name: str) -> dict:
    """Stop a running app and confirm teardown via `sidepage status`."""
    stop_result = _run_direct(["stop", name])
    time.sleep(1)
    status_result = _run_direct(["status", name])
    if stop_result["exit_code"] != 0:
        return {"status": "error", "app": name, "detail": stop_result["output"]}
    return {"status": "stopped", "app": name, "status_check": status_result["output"]}


@mcp.tool()
def sidepage_ls() -> dict:
    """List every app running on this machine (`sidepage ls`)."""
    return _run_direct(["ls"])


@mcp.tool()
def sidepage_status(name: str) -> dict:
    """Reachability and connection info for one running app (`sidepage status`)."""
    return _run_direct(["status", name])


@mcp.tool()
def sidepage_usage(name: str) -> dict:
    """Request/connection counts for one app (`sidepage usage`)."""
    return _run_direct(["usage", name])


@mcp.tool()
def sidepage_app_register(spec: str, name: str) -> dict:
    """Save a reusable serve config, e.g.
    spec="./dash/app.py --auth token --anon", name="dash-weekly"."""
    return _run_direct(["app", "register", spec, name])


@mcp.tool()
def sidepage_app_list() -> dict:
    """List every registered app config."""
    return _run_direct(["app", "list"])


@mcp.tool()
def sidepage_app_show(name: str) -> dict:
    """Show one registered app's resolved config."""
    return _run_direct(["app", "show", name])


@mcp.tool()
def sidepage_app_unregister(name: str) -> dict:
    """Delete a registered app config."""
    return _run_direct(["app", "unregister", name])


@mcp.tool()
def sidepage_secrets_list() -> dict:
    """List secret names present in the vault (not their values)."""
    return _run_direct(["secrets", "list"])


if __name__ == "__main__":
    mcp.run()  # stdio by default — never reached when sidepage wraps this
