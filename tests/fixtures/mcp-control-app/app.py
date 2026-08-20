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

v5 additions, kept in sync with `sidepage serve`'s own new flags:
`sidepage_serve_new`/`sidepage_serve_registered` grew typed `timeout`/
`idle_timeout`/`peers` parameters (`--timeout`/`--idle-timeout`/`--peer`,
built on top of the same `flags` free-text escape hatch rather than
replacing it — anything this fixture doesn't have a dedicated parameter
for is still reachable through `flags`). `sidepage_peers` is new and
different in kind from every other tool here: it's the one thing that
isn't a CLI subcommand at all — `GET /.sidepage/peers.json` is a route on
the *served app's own proxy*, so this tool resolves that app's URL via
`sidepage status` first, then makes the HTTP call itself.

`sidepage_proxy_new` mirrors `sidepage_serve_new` for `sidepage proxy`
(wraps an already-running local service instead of a target this fixture
launches) — same detach-and-poll-the-log background pattern via
`_start_background`, just building `proxy` argv (`--port`, `--name`)
instead of `serve` argv (a positional target). No `peers` parameter:
`--peer` doesn't apply to `proxy` (see `sidepage.commands.proxy`, which
rejects it outright rather than accepting and ignoring it) since there's
no subprocess to inject `SIDEPAGE_PEER_<ROLE>_URL` into.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
import time
import urllib.request
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
_STATUS_URL_RE = re.compile(r"^url:\s+(\S+)", re.MULTILINE)


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


def _reap(proc: subprocess.Popen, log_file: Path) -> None:
    """Waits on a detached `serve` subprocess in the background so it
    never sits as a zombie under this long-running MCP server — nothing
    else ever calls `.wait()`/`.poll()` on it again once `_start_background`
    returns, and a zombie PID defeats `sidepage stop`'s own liveness check
    (POSIX lets you signal a zombie without error, so `sidepage stop`
    can't tell it's already dead — see `sidepage.core.registry.is_alive`).
    Also surfaces a crash that happens *after* `_start_background` already
    reported "running"/"starting", which would otherwise be silent."""
    returncode = proc.wait()
    if returncode != 0:
        with log_file.open("a") as fh:
            fh.write(f"[mcp-control-app] process exited with code {returncode}\n")


def _lifecycle_flags(timeout: float | None, idle_timeout: float | None) -> list[str]:
    """`--timeout`/`--idle-timeout` (v5 §20, auto-teardown) as argv, only
    for whichever of the two was actually given — mirrors how `flags` lets
    a caller omit anything they don't need rather than requiring both."""
    flags: list[str] = []
    if timeout is not None:
        flags += ["--timeout", str(timeout)]
    if idle_timeout is not None:
        flags += ["--idle-timeout", str(idle_timeout)]
    return flags


def _peer_flags(peers: list[str] | None) -> list[str]:
    """`--peer <role>=<app-name>` (v5, repeatable) as argv. Each entry in
    `peers` is passed straight through as one `ROLE=APP-NAME` spec — real
    validation (shape, and that the named app is currently running) is
    `sidepage serve`'s own job, not re-done here."""
    flags: list[str] = []
    for peer in peers or ():
        flags += ["--peer", peer]
    return flags


def _start_background(argv: list[str], *, app_name: str, subcommand: str = "serve") -> dict:
    """Launch `sidepage <subcommand> <argv>` detached and poll its log for a
    URL or an error, same algorithm as `scripts/start_site.sh`. Shared by
    `sidepage_serve_new`/`sidepage_serve_registered` (`subcommand="serve"`,
    the default) and `sidepage_proxy_new` (`subcommand="proxy"`) — both
    block in the foreground and only exit on Ctrl+C/`stop`, so both need
    the same detach-and-poll treatment."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"{app_name}.log"
    log_handle = log_file.open("w")

    proc = subprocess.Popen(
        [SIDEPAGE_BIN, subcommand, *argv],
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log_handle.close()  # the child already has its own duped fd; ours would just leak
    threading.Thread(target=_reap, args=(proc, log_file), daemon=True).start()

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
def sidepage_serve_new(
    name: str,
    target: str,
    flags: str = "",
    timeout: float | None = None,
    idle_timeout: float | None = None,
    peers: list[str] | None = None,
) -> dict:
    """Serve a fresh (not yet registered) target under `name`, e.g.
    flags="--auth token --anon". Backgrounds the blocking `serve` call and
    reports back once it's running, failed, or still starting.

    `timeout`/`idle_timeout` (seconds) are v5 auto-teardown: `timeout` is
    an absolute lifetime from start, `idle_timeout` resets on every
    proxied request/WS message — pass either, both, or neither. `peers` is
    a list of "role=app-name" strings (v5 `--peer`, repeatable) — each
    named app must already be running (`sidepage_ls`/`sidepage_status`),
    or this call fails loud the same way the CLI flag does; not valid for
    a `static` target."""
    argv = [
        target,
        "--name",
        name,
        *shlex.split(flags),
        *_lifecycle_flags(timeout, idle_timeout),
        *_peer_flags(peers),
    ]
    return _start_background(argv, app_name=name)


@mcp.tool()
def sidepage_serve_registered(
    name: str,
    flags: str = "",
    timeout: float | None = None,
    idle_timeout: float | None = None,
    peers: list[str] | None = None,
) -> dict:
    """Serve an app already saved via `sidepage app register`, by name.
    `flags` override the registration for this run only. `timeout`/
    `idle_timeout`/`peers` — see `sidepage_serve_new`; same v5 semantics,
    same "this run only" scope (none of the three are ever part of a
    saved registration)."""
    argv = [
        name,
        *shlex.split(flags),
        *_lifecycle_flags(timeout, idle_timeout),
        *_peer_flags(peers),
    ]
    return _start_background(argv, app_name=name)


@mcp.tool()
def sidepage_proxy_new(
    port: int,
    name: str | None = None,
    flags: str = "",
    timeout: float | None = None,
    idle_timeout: float | None = None,
) -> dict:
    """Proxy an already-running local service on `port` (`sidepage proxy
    --port <port>`), e.g. flags="--auth token --anon". Backgrounds the
    blocking `proxy` call and reports back once it's running, failed, or
    still starting — same detach-and-poll treatment as `sidepage_serve_new`.

    `name` is optional for plain local use (defaults to `proxy-<port>`,
    same as the CLI) but required if `flags` includes `--domain`/`--anon`
    — `sidepage proxy` itself rejects the combination loud rather than
    silently generating one, since the name becomes part of the public
    hostname/registry entry there. No `peers` parameter: `--peer` isn't
    accepted by `proxy` at all (see `sidepage.commands.proxy`)."""
    argv = ["--port", str(port)]
    if name is not None:
        argv += ["--name", name]
    argv += shlex.split(flags)
    argv += _lifecycle_flags(timeout, idle_timeout)
    app_name = name or f"proxy-{port}"
    return _start_background(argv, app_name=app_name, subcommand="proxy")


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
def sidepage_peers(name: str) -> dict:
    """Live-reresolve every `--peer <role>=<app-name>` configured for
    running app `name`, via its own `GET /.sidepage/peers.json` (v5) —
    there's no CLI subcommand for this, it's an HTTP route on the app's
    own proxy, gated by whatever `--auth` tier that app was served with.

    Reflects *current* registry state, unlike the boot-time
    `SIDEPAGE_PEER_<ROLE>_URL` env var already baked into `name`'s own
    subprocess environment, which goes stale the moment a peer restarts
    mid-session with a fresh anon-tunnel URL. Resolves `name`'s own URL
    via `sidepage status` first, same as every other tool here that needs
    to reach a running app rather than just shell out to the CLI."""
    status_result = _run_direct(["status", name])
    match = _STATUS_URL_RE.search(status_result["output"])
    if match is None:
        return {
            "status": "error",
            "app": name,
            "detail": f"couldn't resolve a URL for {name!r} from `sidepage status` — "
            f"is it running? {status_result['output']}",
        }
    peers_url = match.group(1).rstrip("/") + "/.sidepage/peers.json"
    try:
        with urllib.request.urlopen(peers_url, timeout=10) as resp:  # noqa: S310 — local proxy only
            body = json.loads(resp.read().decode())
    except Exception as exc:
        return {"status": "error", "app": name, "detail": f"GET {peers_url} failed: {exc}"}
    return {"status": "ok", "app": name, "peers": body}


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
