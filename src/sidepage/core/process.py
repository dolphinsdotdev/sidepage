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

`proxy`/`ProxyConfig` (backing `sidepage proxy`) reuse this module's
validation, tunnel, registry, and blocking-loop machinery but skip target
detection and subprocess launch entirely — `config.port` is assumed
already listening. `_validate_common` factors out the flag-combination/
`--domain`-resolution checks shared by both `serve`'s `_validate_supported`
and `proxy`; `proxy`'s own `_teardown` deliberately never touches
`config.port`'s process, the one real behavioral difference from `serve`.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from sidepage.core import (
    _platform,
    account,
    ecosystem,
    notebook,
    registry,
    secrets_vault,
    tunnel_manager,
)
from sidepage.core.auth import AuthTier
from sidepage.core.directory_client import Scope
from sidepage.core.reverse_proxy import ProxyHandle, start_proxy, stop_proxy
from sidepage.core.static import validate_static_root
from sidepage.core.target import (
    MCP_APP_METHOD,
    CodeLauncher,
    McpPackage,
    TargetKind,
    allocate_port,
    detect_asgi_app_variable,
    detect_code_launcher,
    detect_mcp_app_variable,
    detect_mcp_package,
    detect_target_kind,
)
from sidepage.core.token_runtime import (
    ControlToken,
    RuntimeToken,
    generate_control_token,
    read_control_token_file,
    resolve_token,
    write_control_token_file,
    write_runtime_file,
)
from sidepage.output import error, info, stdout, success, warn


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


@dataclass(frozen=True)
class ProxyConfig:
    """Config for `proxy` — wraps an *already-running* local service on
    `port` instead of a target `serve` launches itself. Deliberately has
    no `target`/`target_kind`/`env_secrets`/`guardrail`/`peers` fields:
    those are all subprocess-injection concepts, and `proxy` never spawns
    or owns a process, so there's nothing to inject into. See
    `sidepage.commands.proxy` for why those flags are rejected outright
    rather than silently accepted and ignored.
    """

    port: int
    name: str
    domain: str | None
    auth: AuthTier
    scope: Scope
    anon: bool = False
    token: str | None = None
    timeout: float | None = None
    idle_timeout: float | None = None


# Both recognized MCP packages ship their own DNS-rebinding protection
# that validates the inbound `Host`/`Origin` headers against an allowlist
# of loopback values — the official SDK auto-enables it whenever
# `streamable_http_app(host=...)` defaults to (or is passed) `127.0.0.1`
# (`mcp.server.lowlevel.server.Server.streamable_http_app`); `fastmcp`'s
# `http_app(host_origin_protection="auto")` default does the same. Once a
# request comes back through Sidepage's own reverse proxy, the `Host` seen
# by the wrapped process is never `127.0.0.1` — it's the real inbound
# `Host` (`sidepage.core.reverse_proxy` forwards it literally, Caddy-style
# `override_host`), whether that's a `--domain` hostname, an `--anon`
# `*.trycloudflare.com` one, or the local proxy's own listen port. Every
# one of those trips the allowlist and the request is rejected with
# `421 Misdirected Request` — reproduced live against
# `tests/fixtures/mcp-app`, see `tests/test_serve_mcp.py`.
#
# Disabling it here is safe, not a hole: Sidepage's own reverse proxy is
# already the actual trust boundary (loopback-only upstream, `--auth` gate
# in front of it) — the same reasoning already applied to Jupyter's
# `--ServerApp.disable_check_xsrf=True` in `sidepage.core.notebook`.
#
# Both packages' default ASGI app also ships with no CORS headers at all —
# fine for a same-origin caller, but MCP Apps hosts that are themselves web
# pages (e.g. the reference `ext-apps/examples/basic-host`, reproduced live
# against `tests/fixtures/mcp-app`) connect to the MCP endpoint with a
# direct browser `fetch()`/SSE call from the host's own origin, which the
# browser blocks outright without `Access-Control-Allow-Origin`. Wrapping
# with `CORSMiddleware` here — allowing any origin — is the same trust-
# boundary reasoning as the Host/Origin bypass above: Sidepage's own proxy
# and `--auth` gate are what actually gate access, not origin checks.
# `Mcp-Session-Id` needs explicit `expose_headers`: browsers hide non-
# "simple" response headers from JS by default.
_MCP_WRAPPER_SOURCE: dict[McpPackage, str] = {
    McpPackage.OFFICIAL: (
        "from {module} import {app_var}\n"
        "from mcp.server.transport_security import TransportSecuritySettings\n"
        "from starlette.middleware.cors import CORSMiddleware\n"
        "\n"
        "\n"
        "def make_app():\n"
        "    app = {app_var}.{app_method}(\n"
        "        transport_security=TransportSecuritySettings(\n"
        "            enable_dns_rebinding_protection=False\n"
        "        )\n"
        "    )\n"
        "    return CORSMiddleware(\n"
        "        app,\n"
        "        allow_origins=['*'],\n"
        "        allow_methods=['*'],\n"
        "        allow_headers=['*'],\n"
        "        expose_headers=['Mcp-Session-Id'],\n"
        "    )\n"
    ),
    McpPackage.FASTMCP: (
        "from {module} import {app_var}\n"
        "from starlette.middleware.cors import CORSMiddleware\n"
        "\n"
        "\n"
        "def make_app():\n"
        "    app = {app_var}.{app_method}(host_origin_protection=False)\n"
        "    return CORSMiddleware(\n"
        "        app,\n"
        "        allow_origins=['*'],\n"
        "        allow_methods=['*'],\n"
        "        allow_headers=['*'],\n"
        "        expose_headers=['Mcp-Session-Id'],\n"
        "    )\n"
    ),
}


def _mcp_wrapper_path(target: Path) -> Path:
    """Deterministic per-target path for the generated wrapper module (see
    `_write_mcp_host_wrapper`) — recomputed by `serve`'s `_teardown` to
    clean it up without threading extra state through the closure."""
    return target.parent / f"_sidepage_mcp_wrapper_{target.stem}.py"


def _write_mcp_host_wrapper(
    target: Path, package: McpPackage, app_var: str, app_method: str
) -> Path:
    """Writes a small module next to `target` that imports the user's MCP
    server variable, calls the real app-builder method with the package's
    built-in Host/Origin allowlisting turned off, and wraps the result in
    permissive CORS middleware (see the `_MCP_WRAPPER_SOURCE` comment above
    for why both are necessary and safe). Written next to `target` — not
    into a temp/runtime dir — so it
    picks up the same cwd-based import resolution the plain
    `<module>:<app>` reference already relies on elsewhere in this
    function (the wrapped subprocess always runs with `cwd=target.parent`,
    see `serve`).

    Regenerated fresh on every `serve` call, not reused across runs, and
    removed again by `serve`'s `_teardown` — see `_mcp_wrapper_path`.
    """
    source = _MCP_WRAPPER_SOURCE[package].format(
        module=target.stem, app_var=app_var, app_method=app_method
    )
    wrapper_path = _mcp_wrapper_path(target)
    wrapper_path.write_text(source)
    return wrapper_path


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
        # Launched via `uvicorn <wrapper-module>:make_app --factory`, never
        # by running the script directly — bypasses whatever the script's
        # own `__main__` block does, same reasoning as FastAPI above. The
        # payoff is bigger here: most MCP servers' `__main__` calls
        # `<var>.run()`, which defaults to the *stdio* transport (no HTTP
        # at all) unless the author explicitly wired up
        # `transport="streamable-http"` — calling `.streamable_http_app()`
        # / `.http_app()` directly sidesteps that choice entirely, so even
        # a script only ever authored for stdio becomes a real,
        # reverse-proxied HTTP MCP server. See sidepage.core.target for
        # which of the two recognized packages maps to which app-builder
        # method.
        #
        # Not called via a bare `<module>:<var>.<app-method>` reference
        # (what FastAPI's branch above does) because both recognized
        # packages auto-enable Host/Origin allowlisting that only ever
        # accepts `127.0.0.1`/`localhost` — see `_write_mcp_host_wrapper`
        # — and `--factory` calls the referenced callable with zero
        # arguments, so there's no way to pass the disabling kwarg through
        # a bare reference. `_write_mcp_host_wrapper` generates a small
        # module that calls the real app-builder itself, with that
        # protection turned off, and uvicorn serves *that* module's
        # `make_app` factory instead.
        package = detect_mcp_package(target)
        app_method = MCP_APP_METHOD[package]
        app_var = detect_mcp_app_variable(target)
        runner = ecosystem.resolve_python_runner(
            target.parent, extra_packages=[package.value, "uvicorn"]
        )
        wrapper_path = _write_mcp_host_wrapper(target, package, app_var, app_method)
        return runner + [
            "uvicorn",
            f"{wrapper_path.stem}:make_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]

    # GENERIC_PYTHON: assume the script itself reads $PORT.
    runner = ecosystem.resolve_python_runner(target.parent)
    return runner + [str(target)]


def _validate_common(
    *,
    domain: str | None,
    anon: bool,
    scope: Scope,
    auth: AuthTier,
    timeout: float | None,
    idle_timeout: float | None,
) -> account.DomainConfig | None:
    """Flag-combination and `--domain` validation shared by `serve` and
    `proxy` — everything that's target-agnostic (doesn't reference a file
    to launch or a subprocess to inject into). Run before either does
    anything else, so a bad flag combination or an unconfigured domain
    fails immediately.

    Returns the resolved `DomainConfig` when `domain` is set (used by the
    caller to actually open the tunnel), or `None` otherwise.
    """
    if domain is not None and anon:
        raise ValueError(
            "--domain and --anon are mutually exclusive — --anon is Cloudflare's anonymous "
            "Quick Tunnel with no custom domain; --domain routes through your own BYO domain."
        )
    if scope is not Scope.LOCAL:
        raise NotImplementedError(
            f"--scope {scope} isn't implemented — there's no directory service to "
            "register with yet. Only the default --scope local works."
        )
    if auth not in (AuthTier.OPEN, AuthTier.TOKEN):
        raise NotImplementedError(
            f"--auth {auth} isn't implemented — only open and token are built."
        )
    if timeout is not None and timeout <= 0:
        raise ValueError(f"--timeout must be a positive number of seconds, got {timeout}")
    if idle_timeout is not None and idle_timeout <= 0:
        raise ValueError(f"--idle-timeout must be a positive number of seconds, got {idle_timeout}")

    if domain is None:
        return None
    domain_config = account.get_default_domain()
    if domain_config is None or domain_config.domain != domain:
        raise ValueError(
            f"--domain {domain} isn't configured — run `sidepage account domain "
            f"set {domain} --api-token-name <name>` first (the name must already "
            "be in the vault, see `sidepage secrets set`)."
        )
    return domain_config


def _validate_supported(config: ServeConfig) -> account.DomainConfig | None:
    """Validate `config` and resolve `--domain` against the persisted BYO
    configuration, all before `serve` does anything else — including
    touching the target file, so a bad flag combination or an
    unconfigured domain fails immediately regardless of whether the target
    itself even exists.

    Returns the resolved `DomainConfig` when `config.domain` is set (used
    by `serve` to actually open the tunnel), or `None` otherwise.
    """
    if config.guardrail is not None:
        raise NotImplementedError(
            "--guardrail isn't implemented — parked, see sidepage.core.guardrail."
        )
    return _validate_common(
        domain=config.domain,
        anon=config.anon,
        scope=config.scope,
        auth=config.auth,
        timeout=config.timeout,
        idle_timeout=config.idle_timeout,
    )


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
    # Always generated, regardless of --auth — gates POST /.sidepage/stop,
    # the cross-platform stand-in for a cross-process SIGTERM that
    # sidepage.core.process.stop() uses on Windows (see that function and
    # sidepage.core.token_runtime.ControlToken for why).
    control_token = generate_control_token()

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
        if launcher is CodeLauncher.MCP:
            # Best-effort: `_write_mcp_host_wrapper` always writes this
            # file before the subprocess referencing it is ever started,
            # so it exists by the time any teardown path can run.
            _mcp_wrapper_path(target).unlink(missing_ok=True)
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
            control_token=control_token,
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
            control_token=control_token,
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
            control_token=control_token,
            start_upstream=_start_notebook_upstream,
            peers=config.peers,
        )
    else:
        raise NotImplementedError(
            f"--type {target_kind} isn't implemented — only code, static, and notebook are built."
        )

    if token is not None:
        write_runtime_file(RuntimeToken(app_name=app_name, pid=os.getpid(), value=token))
    write_control_token_file(ControlToken(app_name=app_name, pid=os.getpid(), value=control_token))

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
            if proxy.stop_requested.is_set():
                # POST /.sidepage/stop — the cross-platform stand-in for a
                # cross-process SIGTERM, used by sidepage.core.process.stop()
                # on Windows (see that function's docstring for why the real
                # signal doesn't work there).
                break
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


def proxy(config: ProxyConfig) -> None:
    """Wrap an already-running local service (`config.port`, always
    `127.0.0.1`) with sidepage's proxy/auth/tunnel/registry stack, without
    ever launching or owning a process. Mirrors `serve`'s shape closely —
    same validation, same tunnel modes, same SIGTERM/timeout/idle-timeout
    blocking loop — minus everything target-detection- and
    subprocess-launch-related, since there's no file to detect a kind from
    and nothing for `proxy` to spawn.

    **Teardown asymmetry with `serve`, load-bearing, do not "fix" this**:
    `serve`'s `_teardown()` kills the subprocess it spawned; this one must
    not — sidepage never started `config.port`'s service, so Ctrl+C /
    `sidepage stop <name>` tears down only the proxy, the tunnel, and the
    registry entry, leaving that service running exactly as it was. See
    `sidepage.commands.proxy` for the full set of user-facing caveats
    (Origin/CSRF, localhost-trust security, OAuth/`--anon`) this prints a
    condensed version of at startup.
    """
    domain_config = _validate_common(
        domain=config.domain,
        anon=config.anon,
        scope=config.scope,
        auth=config.auth,
        timeout=config.timeout,
        idle_timeout=config.idle_timeout,
    )

    app_name = config.name
    if registry.get(app_name) is not None:
        raise ValueError(
            f"an app named {app_name!r} is already registered as running — "
            f"`sidepage stop {app_name}` it first, or pass --name for a different one."
        )

    token: str | None = None
    if config.auth is AuthTier.TOKEN:
        token = resolve_token(explicit=config.token, env_value=os.environ.get("SIDEPAGE_TOKEN"))

    listen_port = allocate_port()
    proxy_handle: ProxyHandle
    tunnel = None
    control_token = generate_control_token()

    def _teardown() -> None:
        stop_proxy(proxy_handle)
        # Unregister before closing the tunnel — same ordering requirement
        # as serve()'s _teardown, see tunnel_manager.close_tunnel's
        # docstring (BYO-domain teardown reference-counts the registry to
        # decide whether the shared cloudflared process should die too).
        registry.unregister(app_name)
        if tunnel is not None:
            try:
                tunnel_manager.close_tunnel(tunnel)
            except Exception as exc:
                error(f"tunnel teardown failed: {exc}")
        # Deliberately no subprocess teardown here (contrast serve()'s
        # proc.terminate()/.kill()) — proxy never spawned config.port's
        # service, so there is nothing to terminate.
        stdout.print()
        warn(
            f"{app_name} stopped — the service on 127.0.0.1:{config.port} was not touched "
            "and may still be running"
        )

    def _handle_sigterm(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _handle_sigterm)

    proxy_handle = start_proxy(
        app_name,
        listen_port=listen_port,
        upstream_port=config.port,
        auth=config.auth.value,
        token=token,
        control_token=control_token,
    )

    if token is not None:
        write_runtime_file(RuntimeToken(app_name=app_name, pid=os.getpid(), value=token))
    write_control_token_file(ControlToken(app_name=app_name, pid=os.getpid(), value=control_token))

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
            target=f"127.0.0.1:{config.port}",
            target_kind="external",
            listen_port=listen_port,
            url=local_url,
            tunnel_url=tunnel_url,
            started_at=started_at,
            domain=domain_config.domain if domain_config is not None else None,
        )
    )

    success(f"{app_name} proxying 127.0.0.1:{config.port} -> {local_url}")
    if tunnel_url:
        success(f"public URL: {tunnel_url}")
    if token is not None:
        info(f"access token: {token}")
    if config.timeout is not None:
        info(f"auto-stop after {config.timeout:g}s")
    if config.idle_timeout is not None:
        info(f"auto-stop after {config.idle_timeout:g}s of no traffic")
    warn(
        f"stopping {app_name!r} only tears down this proxy — your service on "
        f"127.0.0.1:{config.port} keeps running"
    )
    warn(
        "every proxied request reaches your app from 127.0.0.1 — any localhost-only "
        "debug/admin logic (e.g. Flask's interactive debugger) is now reachable through "
        "the tunnel too, regardless of --auth"
    )
    warn(
        "your app's own Origin/Host/CSRF checks may still reject traffic even with "
        "X-Forwarded-* set correctly — see `sidepage proxy --help` for framework fixes"
    )
    if config.anon:
        warn(
            "--anon: this hostname changes every run — OAuth/SSO redirect URIs can't be "
            "registered against it; use --domain if your app does OAuth login"
        )
    info("Ctrl+C to stop")

    try:
        while True:
            time.sleep(1)
            if proxy_handle.stop_requested.is_set():
                # POST /.sidepage/stop — the cross-platform stand-in for a
                # cross-process SIGTERM, used by sidepage.core.process.stop()
                # on Windows (see that function's docstring for why the real
                # signal doesn't work there).
                break
            now = time.time()
            if config.timeout is not None and now - started_at >= config.timeout:
                info(f"{app_name}: --timeout ({config.timeout:g}s) reached, stopping")
                break
            if config.idle_timeout is not None:
                idle_for = now - proxy_handle.activity.last()
                if idle_for >= config.idle_timeout:
                    info(f"{app_name}: --idle-timeout ({config.idle_timeout:g}s) reached, stopping")
                    break
    except KeyboardInterrupt:
        pass
    finally:
        _teardown()


def _request_stop_windows(app: registry.RunningApp, app_name: str) -> None:
    """Best-effort graceful stop on Windows: `POST /.sidepage/stop` on the
    target's own reverse proxy, authenticated with its control token (see
    `sidepage.core.token_runtime.ControlToken`). Never raises — a failure
    here (missing control-token file, connection refused, timeout) just
    means the caller's usual 10s wait finds the app still alive and falls
    through to a hard kill, so this only warns rather than aborting `stop`
    outright.

    **Why POSIX doesn't need this**: `os.kill(pid, signal.SIGTERM)` sent
    from a different process there is delivered as a real signal, caught
    by the target's own `signal.signal(signal.SIGTERM, ...)` handler (see
    `serve`/`proxy` above), which raises `KeyboardInterrupt` and runs the
    normal `finally: _teardown()` path. On Windows, `os.kill(pid,
    signal.SIGTERM)` sent cross-process instead calls `TerminateProcess`
    directly — no signal is delivered, so that handler never runs, and the
    target would be hard-killed without ever unregistering itself, closing
    its tunnel, or killing a subprocess it spawned. This local HTTP call
    reaches the target's own event loop instead, so it can run that exact
    same teardown itself before exiting.
    """
    try:
        control = read_control_token_file(app_name, app.pid)
    except (FileNotFoundError, json.JSONDecodeError):
        warn(
            f"{app_name}: no control token file found for a graceful stop request — "
            "falling back to a hard kill after the usual wait"
        )
        return
    try:
        httpx.post(
            f"{app.url}/.sidepage/stop",
            headers={"X-Sidepage-Control-Token": control.value},
            timeout=5,
        )
    except httpx.HTTPError as exc:
        warn(
            f"{app_name}: couldn't reach it to request a graceful stop ({exc}) — "
            "falling back to a hard kill after the usual wait"
        )


def stop(app_name: str) -> None:
    """Explicit teardown of a running app by name — distinct from Ctrl+C
    but routed through the same clean-teardown path `serve`/`proxy` use.

    Checks `registry.is_alive` up front, before ever sending a signal:
    a bare pid-existence probe alone can't tell a genuinely dead pid from
    a *zombie* one (exited, not yet reaped by whatever spawned it) on
    POSIX. Without this check, a zombie app would fall through to the
    stop branch below, which also can't detect it died, and `stop` would
    misreport "didn't stop within 10s" for an app that was already gone —
    actively misleading, since it implies the app is alive and
    unresponsive rather than already dead. Both "fully gone" and
    "zombie" now take the same, already-correct stale-entry path below.

    **POSIX**: `os.kill(pid, signal.SIGTERM)`, unchanged — delivered as a
    real signal, caught by the target's own handler, routed through its
    normal teardown. **Windows**: `os.kill`'s cross-process `SIGTERM`
    doesn't reach the target's signal handler at all (it maps straight to
    `TerminateProcess`), so a local, control-token-authenticated HTTP
    request is used instead (`_request_stop_windows`) — same teardown
    path, different delivery mechanism. Either way, if the app hasn't
    exited within the wait below, a hard kill (`_platform.terminate_process`
    on Windows; the existing "didn't stop" error on POSIX, unchanged) is
    the backstop.
    """
    app = registry.get(app_name)
    if app is None:
        error(f"no running app named {app_name!r}")
        raise SystemExit(1)

    if not registry.is_alive(app.pid):
        registry.unregister(app_name)
        info(f"{app_name} wasn't actually running (stale registry entry removed)")
        return

    if sys.platform == "win32":
        _request_stop_windows(app, app_name)
    else:
        try:
            os.kill(app.pid, signal.SIGTERM)
        except ProcessLookupError:
            registry.unregister(app_name)
            info(f"{app_name} wasn't actually running (stale registry entry removed)")
            return

    deadline = time.time() + 10
    stopped = False
    while time.time() < deadline:
        if not registry.is_alive(app.pid):
            stopped = True
            break
        time.sleep(0.2)

    if not stopped and sys.platform == "win32":
        try:
            _platform.terminate_process(app.pid, force=True)
        except OSError:
            pass
        time.sleep(0.5)
        stopped = not registry.is_alive(app.pid)

    if not stopped:
        error(f"{app_name} (pid {app.pid}) didn't stop within 10s")
        raise SystemExit(1)

    registry.unregister(app_name)
    success(f"{app_name} stopped")
