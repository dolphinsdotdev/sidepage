"""Local process + tunnel + proxy lifecycle — backs `sidepage serve` /
`sidepage stop` (spec v3 §2).

pm2-style, port-free from the caller's perspective: `serve` allocates real
OS-assigned ports (`sidepage.core.target.allocate_port`), injects one into
the wrapped process, starts the local reverse proxy
(`sidepage.core.reverse_proxy`) in front of it, brings up a tunnel per
`config.anon` (`sidepage.core.tunnel_manager`), and blocks the terminal
until interrupted.

**What's real vs. not**, since this orchestrates every other module: static,
Streamlit-flavored, FastAPI-flavored, MCP-flavored (Python), and notebook
(Jupyter Lab) targets, `open`/`token` auth, `--env` secret injection,
`--anon` tunneling, and `--domain` BYO tunneling (see
`sidepage.core.tunnel_manager.open_byo_tunnel`) all actually work.
Non-local `--scope`, `network`/`oauth` auth, and
`--guardrail` are rejected up front with a clear `NotImplementedError`
(not silently ignored) — `sidepage.commands.serve` catches that and
reports it the same way as any other not-yet-built feature. A handful of
other early rejections (`--domain` with no matching `account domain set`
configuration, `--anon`+`--domain` together, an already-registered app
name) are real validation, not missing features, so they raise
`ValueError` instead — `sidepage.commands.serve` reports those as plain
errors rather than "not yet implemented."

Deliberately foreground/single-process by contract: killing the process
(Ctrl+C, or `sidepage stop` sending SIGTERM — both routed through the same
teardown via a SIGTERM handler that raises `KeyboardInterrupt`) tears
everything down **immediately** — no grace period, confirmed default.
Multi-process/background management is out of scope for this binary
(orchestrator, §16); `--background` is explicitly ruled out.

v5 adds three more, composing with everything above rather than replacing
it: `--timeout`/`--idle-timeout` (§20) are checked inside this same
blocking loop and exit through the same `_teardown()` Ctrl+C/`stop`
already use; CODE/NOTEBOOK's `subprocess.Popen` is deferred into a
closure handed to `sidepage.core.reverse_proxy.start_proxy` as
`start_upstream`, fired on first inbound request instead of unconditionally
here (§21 Tier 1, lazy start — STATIC is untouched, already in-process);
and `--peer <role>=<app-name>` resolves each named peer once against
`sidepage.core.registry`'s live state and injects
`SIDEPAGE_PEER_<ROLE>_URL` into the wrapped subprocess's env, the same
injection mechanism `--env` already uses for vault secrets.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from sidepage.core import account, ecosystem, notebook, registry, secrets_vault, tunnel_manager
from sidepage.core.auth import AuthTier
from sidepage.core.directory_client import Scope
from sidepage.core.reverse_proxy import ProxyHandle, start_proxy, stop_proxy
from sidepage.core.static import validate_static_root
from sidepage.core.target import (
    MCP_APP_METHOD,
    CodeLauncher,
    TargetKind,
    allocate_port,
    detect_asgi_app_variable,
    detect_code_launcher,
    detect_mcp_app_variable,
    detect_mcp_package,
    detect_target_kind,
)
from sidepage.core.token_runtime import RuntimeToken, resolve_token, write_runtime_file
from sidepage.output import error, info, stdout, success


@dataclass(frozen=True)
class ServeConfig:
    target: Path
    target_kind: TargetKind | None  # None means "auto" — see sidepage.core.target
    name: str | None
    domain: str | None
    auth: AuthTier
    scope: Scope
    anon: bool = False  # Quick Tunnel, no directory entry — orthogonal to `auth`
    token: str | None = None  # explicit --token; None means env var or auto-generate
    env_secrets: tuple[str, ...] = ()  # v4 §9 — vault secret names for --env (repeatable)
    guardrail: Path | None = None  # parked, not built — see sidepage.core.guardrail
    timeout: float | None = None  # v5 §20 — absolute lifetime in seconds, from started_at
    idle_timeout: float | None = None  # v5 §20 — seconds since last proxied traffic
    peers: tuple[tuple[str, str], ...] = ()  # v5 --peer: (role, app_name) pairs, repeatable


def _build_code_launch_argv(target: Path, launcher: CodeLauncher, port: int) -> list[str]:
    if launcher is CodeLauncher.STREAMLIT:
        # extra_packages=["streamlit"] guarantees the launcher's own
        # detected requirement is present even if the target's own
        # requirements.txt doesn't declare it — see sidepage.core.ecosystem
        # for why that matters in practice.
        runner = ecosystem.resolve_python_runner(target.parent, extra_packages=["streamlit"])
        return runner + [
            "streamlit",
            "run",
            str(target),
            "--server.port",
            str(port),
            "--server.headless",
            "true",
            "--server.address",
            "127.0.0.1",
        ]

    if launcher is CodeLauncher.FASTAPI:
        # Launched via `uvicorn <module>:<app>`, not by running the script
        # directly — bypasses whatever the script's own `__main__` block
        # does (real FastAPI apps often hardcode a port there), and is the
        # standard way to run an ASGI app regardless of whether one exists
        # at all. See sidepage.core.target for the module/app-name detail.
        runner = ecosystem.resolve_python_runner(
            target.parent, extra_packages=["fastapi", "uvicorn"]
        )
        app_var = detect_asgi_app_variable(target)
        return runner + [
            "uvicorn",
            f"{target.stem}:{app_var}",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]

    if launcher is CodeLauncher.MCP:
        # Launched via `uvicorn <module>:<var>.<app-method> --factory`,
        # never by running the script directly — bypasses whatever the
        # script's own `__main__` block does, same reasoning as FastAPI
        # above. The payoff is bigger here: most MCP servers' `__main__`
        # calls `<var>.run()`, which defaults to the *stdio* transport
        # (no HTTP at all) unless the author explicitly wired up
        # `transport="streamable-http"` — calling `.streamable_http_app()`
        # / `.http_app()` directly sidesteps that choice entirely, so even
        # a script only ever authored for stdio becomes a real,
        # reverse-proxied HTTP MCP server. See sidepage.core.target for
        # which of the two recognized packages maps to which app-builder
        # method.
        package = detect_mcp_package(target)
        app_method = MCP_APP_METHOD[package]
        app_var = detect_mcp_app_variable(target)
        runner = ecosystem.resolve_python_runner(
            target.parent, extra_packages=[package.value, "uvicorn"]
        )
        return runner + [
            "uvicorn",
            f"{target.stem}:{app_var}.{app_method}",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]

    # GENERIC_PYTHON: assume the script itself reads $PORT.
    runner = ecosystem.resolve_python_runner(target.parent)
    return runner + [str(target)]


def _validate_supported(config: ServeConfig) -> account.DomainConfig | None:
    """Validate `config` and resolve `--domain` against the persisted BYO
    configuration, all before `serve` does anything else — including
    touching the target file, so a bad flag combination or an
    unconfigured domain fails immediately regardless of whether the target
    itself even exists.

    Returns the resolved `DomainConfig` when `config.domain` is set (used
    by `serve` to actually open the tunnel), or `None` otherwise.
    """
    if config.domain is not None and config.anon:
        raise ValueError(
            "--domain and --anon are mutually exclusive — --anon is Cloudflare's anonymous "
            "Quick Tunnel with no custom domain; --domain routes through your own BYO domain."
        )
    if config.scope is not Scope.LOCAL:
        raise NotImplementedError(
            f"--scope {config.scope} isn't implemented — there's no directory service to "
            "register with yet. Only the default --scope local works."
        )
    if config.auth not in (AuthTier.OPEN, AuthTier.TOKEN):
        raise NotImplementedError(
            f"--auth {config.auth} isn't implemented — only open and token are built."
        )
    if config.guardrail is not None:
        raise NotImplementedError(
            "--guardrail isn't implemented — parked, see sidepage.core.guardrail."
        )
    if config.timeout is not None and config.timeout <= 0:
        raise ValueError(f"--timeout must be a positive number of seconds, got {config.timeout}")
    if config.idle_timeout is not None and config.idle_timeout <= 0:
        raise ValueError(
            f"--idle-timeout must be a positive number of seconds, got {config.idle_timeout}"
        )

    if config.domain is None:
        return None
    domain_config = account.get_default_domain()
    if domain_config is None or domain_config.domain != config.domain:
        raise ValueError(
            f"--domain {config.domain} isn't configured — run `sidepage account domain "
            f"set {config.domain} --api-token-name <name>` first (the name must already "
            "be in the vault, see `sidepage secrets set`)."
        )
    return domain_config


def serve(config: ServeConfig) -> None:
    """Start the app and block until interrupted. See module docstring for
    what's real vs. not."""
    domain_config = _validate_supported(config)

    # Resolve to an absolute path up front — the wrapped subprocess runs
    # with `cwd=target.parent`, so any relative path built downstream
    # (e.g. --with-requirements in sidepage.core.ecosystem) would otherwise
    # be interpreted relative to the *new* cwd instead of where the user
    # actually ran `sidepage serve` from.
    target = config.target.resolve()
    target_kind = detect_target_kind(target, override=config.target_kind)
    if config.peers and target_kind is TargetKind.STATIC:
        raise ValueError(
            "--peer isn't supported for static targets — there's no subprocess to inject "
            "SIDEPAGE_PEER_<ROLE>_URL into, and the live GET /.sidepage/peers.json endpoint "
            "only exists on the code/notebook proxy route table."
        )
    app_name = config.name or target.stem or target.name
    if registry.get(app_name) is not None:
        raise ValueError(
            f"an app named {app_name!r} is already registered as running — "
            f"`sidepage stop {app_name}` it first, or pass --name for a different one."
        )

    token: str | None = None
    if config.auth is AuthTier.TOKEN:
        token = resolve_token(explicit=config.token, env_value=os.environ.get("SIDEPAGE_TOKEN"))

    injected_env: dict[str, str] = {}
    for secret_name in config.env_secrets:
        injected_env[secret_name] = secrets_vault.get_secret(secret_name)  # fails loud if missing

    # v5 --peer: resolved once, here, against the *live* registry — fails
    # loud (registry.PeerNotFoundError) if a named peer isn't currently
    # running. Live re-resolution for a peer that restarts mid-session is
    # the separate GET /.sidepage/peers.json route in
    # sidepage.core.reverse_proxy, not this one-shot env var.
    for role, peer_app_name in config.peers:
        injected_env[f"SIDEPAGE_PEER_{role.upper()}_URL"] = registry.resolve_peer_url(
            peer_app_name
        )

    listen_port = allocate_port()
    proc: subprocess.Popen | None = None
    proxy: ProxyHandle
    tunnel = None
    launcher: CodeLauncher | None = None

    def _teardown() -> None:
        stop_proxy(proxy)
        # Unregister *before* closing the tunnel, not after: for BYO-domain,
        # tunnel_manager.close_tunnel decides whether to kill the domain's
        # shared cloudflared process by counting this domain's still-running
        # apps in the registry (sidepage.core.registry.list_running_for_domain).
        # If this app were still counted while that check runs, the very
        # last app's teardown would never see zero and the process would
        # never die — see tunnel_manager.close_tunnel's docstring.
        registry.unregister(app_name)
        if tunnel is not None:
            try:
                tunnel_manager.close_tunnel(tunnel)
            except Exception as exc:
                error(f"tunnel teardown failed: {exc}")
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        stdout.print()
        info(f"{app_name} stopped")

    def _handle_sigterm(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _handle_sigterm)

    if target_kind is TargetKind.STATIC:
        validate_static_root(target)
        proxy = start_proxy(
            app_name,
            listen_port=listen_port,
            static_root=target,
            auth=config.auth.value,
            token=token,
        )
    elif target_kind is TargetKind.CODE:
        upstream_port = allocate_port()
        launcher = detect_code_launcher(target)
        argv = _build_code_launch_argv(target, launcher, upstream_port)
        env = {**os.environ, **injected_env, "PORT": str(upstream_port)}

        def _start_code_upstream() -> None:
            # v5 §21 Tier 1 — lazy start: not called from here at all.
            # `sidepage.core.reverse_proxy._build_proxy_app` calls this
            # itself, once, on the first inbound request/WS connect.
            nonlocal proc
            proc = subprocess.Popen(argv, cwd=target.parent, env=env)

        proxy = start_proxy(
            app_name,
            listen_port=listen_port,
            upstream_port=upstream_port,
            auth=config.auth.value,
            token=token,
            start_upstream=_start_code_upstream,
            peers=config.peers,
        )
    elif target_kind is TargetKind.NOTEBOOK:
        upstream_port = allocate_port()
        argv = notebook.build_jupyter_launch_command(target, port=upstream_port)
        env = {**os.environ, **injected_env}

        def _start_notebook_upstream() -> None:
            nonlocal proc
            proc = subprocess.Popen(argv, cwd=target.parent, env=env)

        proxy = start_proxy(
            app_name,
            listen_port=listen_port,
            upstream_port=upstream_port,
            auth=config.auth.value,
            token=token,
            start_upstream=_start_notebook_upstream,
            peers=config.peers,
        )
    else:
        raise NotImplementedError(
            f"--type {target_kind} isn't implemented — only code, static, and notebook are built."
        )

    if token is not None:
        write_runtime_file(RuntimeToken(app_name=app_name, pid=os.getpid(), value=token))

    local_url = f"http://127.0.0.1:{listen_port}"
    tunnel_url = None
    if config.anon:
        info("opening anonymous Cloudflare Quick Tunnel...")
        tunnel = tunnel_manager.open_anon_tunnel(listen_port)
        tunnel_url = tunnel.url
    elif domain_config is not None:
        info(f"opening BYO-domain tunnel on {domain_config.domain}...")
        tunnel = tunnel_manager.open_byo_tunnel(
            app_name,
            domain_config.domain,
            listen_port,
            zone_id=domain_config.zone_id,
            account_id=domain_config.account_id,
            tunnel_id=domain_config.tunnel_id,
            api_token_name=domain_config.api_token_name,
            tunnel_token_name=domain_config.tunnel_token_name,
        )
        tunnel_url = tunnel.url

    started_at = time.time()
    registry.register(
        registry.RunningApp(
            name=app_name,
            pid=os.getpid(),
            target=str(target),
            target_kind=target_kind.value,
            listen_port=listen_port,
            url=local_url,
            tunnel_url=tunnel_url,
            started_at=started_at,
            domain=domain_config.domain if domain_config is not None else None,
        )
    )

    success(f"{app_name} serving at {local_url}")
    if launcher is CodeLauncher.FASTAPI:
        info(f"API docs: {local_url}/docs (FastAPI's own Swagger UI — served automatically)")
    if launcher is CodeLauncher.MCP:
        info(f"MCP endpoint: {local_url}/mcp (Streamable HTTP, bypassing the script's transport)")
    if tunnel_url:
        success(f"public URL: {tunnel_url}")
    if token is not None:
        info(f"access token: {token}")
    if config.timeout is not None:
        info(f"auto-stop after {config.timeout:g}s")
    if config.idle_timeout is not None:
        info(f"auto-stop after {config.idle_timeout:g}s of no traffic")
    info("Ctrl+C to stop")

    # §20 timeout / idle-timeout: both checked right here in the existing
    # blocking loop, both exiting through the same `break` -> `finally:
    # _teardown()` path Ctrl+C and SIGTERM already use — no separate
    # teardown route. `proxy.activity.last()` is the "time of last
    # proxied request/WS message" `sidepage.core.reverse_proxy` tracks;
    # for a still-booting app (lazy start, nothing has hit it yet) that's
    # simply `started_at`, so idle-timeout can't fire before the app has
    # ever received a single request.
    try:
        while True:
            time.sleep(1)
            now = time.time()
            if config.timeout is not None and now - started_at >= config.timeout:
                info(f"{app_name}: --timeout ({config.timeout:g}s) reached, stopping")
                break
            if config.idle_timeout is not None:
                idle_for = now - proxy.activity.last()
                if idle_for >= config.idle_timeout:
                    info(f"{app_name}: --idle-timeout ({config.idle_timeout:g}s) reached, stopping")
                    break
    except KeyboardInterrupt:
        pass
    finally:
        _teardown()


def stop(app_name: str) -> None:
    """Explicit teardown of a running app by name — distinct from Ctrl+C
    but routed through the same clean-teardown path in `serve` via SIGTERM.

    Checks `registry.is_alive` up front, before ever sending a signal:
    `os.kill(pid, 0)`/`ProcessLookupError` alone can't tell a genuinely
    dead pid from a *zombie* one (exited, not yet reaped by whatever
    spawned it) — POSIX allows signaling a zombie without error. Without
    this check, a zombie app would fall through to the SIGTERM branch,
    which also can't detect it died, and `stop` would misreport "didn't
    stop within 10s" for an app that was already gone — actively
    misleading, since it implies the app is alive and unresponsive rather
    than already dead. Both "fully gone" and "zombie" now take the same,
    already-correct stale-entry path below.
    """
    app = registry.get(app_name)
    if app is None:
        error(f"no running app named {app_name!r}")
        raise SystemExit(1)

    if not registry.is_alive(app.pid):
        registry.unregister(app_name)
        info(f"{app_name} wasn't actually running (stale registry entry removed)")
        return

    try:
        os.kill(app.pid, signal.SIGTERM)
    except ProcessLookupError:
        registry.unregister(app_name)
        info(f"{app_name} wasn't actually running (stale registry entry removed)")
        return

    deadline = time.time() + 10
    while time.time() < deadline:
        if not registry.is_alive(app.pid):
            break
        time.sleep(0.2)
    else:
        error(f"{app_name} (pid {app.pid}) didn't stop within 10s")
        raise SystemExit(1)

    registry.unregister(app_name)
    success(f"{app_name} stopped")
