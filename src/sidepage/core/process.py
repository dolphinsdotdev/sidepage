"""Local process + tunnel + proxy lifecycle — backs `sidepage serve` /
`sidepage stop` (spec v3 §2).

pm2-style, port-free from the caller's perspective: `serve` allocates real
OS-assigned ports (`sidepage.core.target.allocate_port`), injects one into
the wrapped process, starts the local reverse proxy
(`sidepage.core.reverse_proxy`) in front of it, brings up a tunnel per
`config.anon` (`sidepage.core.tunnel_manager`), and blocks the terminal
until interrupted.

**What's real vs. not**, since this orchestrates every other module: static
and Streamlit-flavored code targets, `open`/`token` auth, `--env` secret
injection, and `--anon` tunneling all actually work. `--domain` (BYO),
non-local `--scope`, `network`/`oauth` auth, and `--guardrail` are rejected
up front with a clear `NotImplementedError` (not silently ignored) —
`sidepage.commands.serve` catches that and reports it the same way as any
other not-yet-built feature.

Deliberately foreground/single-process by contract: killing the process
(Ctrl+C, or `sidepage stop` sending SIGTERM — both routed through the same
teardown via a SIGTERM handler that raises `KeyboardInterrupt`) tears
everything down **immediately** — no grace period, confirmed default.
Multi-process/background management is out of scope for this binary
(orchestrator, §16); `--background` is explicitly ruled out.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from sidepage.core import ecosystem, registry, secrets_vault, tunnel_manager
from sidepage.core.auth import AuthTier
from sidepage.core.directory_client import Scope
from sidepage.core.reverse_proxy import ProxyHandle, start_proxy, stop_proxy
from sidepage.core.static import validate_static_root
from sidepage.core.target import (
    CodeLauncher,
    TargetKind,
    allocate_port,
    detect_code_launcher,
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


def _build_code_launch_argv(target: Path, launcher: CodeLauncher, port: int) -> list[str]:
    if launcher is CodeLauncher.STREAMLIT:
        runner = ecosystem.resolve_python_runner(target.parent, extra_package="streamlit")
        streamlit_args = [
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
        if runner[0] == "uv":
            return runner + streamlit_args
        # runner is [venv_python] — invoke streamlit as a module instead of
        # relying on a console-script entry point being on PATH.
        return runner + ["-m", "streamlit"] + streamlit_args[1:]

    # GENERIC_PYTHON: assume the script itself reads $PORT.
    runner = ecosystem.resolve_python_runner(target.parent)
    return runner + [str(target)]


def _validate_supported(config: ServeConfig) -> None:
    if config.domain is not None:
        raise NotImplementedError(
            "--domain (BYO Cloudflare domain) isn't implemented — it needs real Zone:DNS:Edit "
            "and per-tunnel credentials plus DNS automation. Use --anon for a public URL, or "
            "omit --domain/--anon to serve locally only."
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


def serve(config: ServeConfig) -> None:
    """Start the app and block until interrupted. See module docstring for
    what's real vs. not."""
    _validate_supported(config)

    # Resolve to an absolute path up front — the wrapped subprocess runs
    # with `cwd=target.parent`, so any relative path built downstream
    # (e.g. --with-requirements in sidepage.core.ecosystem) would otherwise
    # be interpreted relative to the *new* cwd instead of where the user
    # actually ran `sidepage serve` from.
    target = config.target.resolve()
    target_kind = detect_target_kind(target, override=config.target_kind)
    app_name = config.name or target.stem or target.name
    if registry.get(app_name) is not None:
        raise NotImplementedError(
            f"an app named {app_name!r} is already registered as running — "
            f"`sidepage stop {app_name}` it first, or pass --name for a different one."
        )

    token: str | None = None
    if config.auth is AuthTier.TOKEN:
        token = resolve_token(explicit=config.token, env_value=os.environ.get("SIDEPAGE_TOKEN"))

    injected_env: dict[str, str] = {}
    for secret_name in config.env_secrets:
        injected_env[secret_name] = secrets_vault.get_secret(secret_name)  # fails loud if missing

    listen_port = allocate_port()
    proc: subprocess.Popen | None = None
    proxy: ProxyHandle
    tunnel = None

    def _teardown() -> None:
        stop_proxy(proxy)
        if tunnel is not None:
            tunnel_manager.close_tunnel(tunnel)
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        registry.unregister(app_name)
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
        proc = subprocess.Popen(argv, cwd=target.parent, env=env)
        proxy = start_proxy(
            app_name,
            listen_port=listen_port,
            upstream_port=upstream_port,
            auth=config.auth.value,
            token=token,
        )
    else:
        raise NotImplementedError(
            f"--type {target_kind} isn't implemented — only code and static are built."
        )

    if token is not None:
        write_runtime_file(RuntimeToken(app_name=app_name, pid=os.getpid(), value=token))

    local_url = f"http://127.0.0.1:{listen_port}"
    tunnel_url = None
    if config.anon:
        info("opening anonymous Cloudflare Quick Tunnel...")
        tunnel = tunnel_manager.open_anon_tunnel(listen_port)
        tunnel_url = tunnel.url

    registry.register(
        registry.RunningApp(
            name=app_name,
            pid=os.getpid(),
            target=str(target),
            target_kind=target_kind.value,
            listen_port=listen_port,
            url=local_url,
            tunnel_url=tunnel_url,
            started_at=time.time(),
        )
    )

    success(f"{app_name} serving at {local_url}")
    if tunnel_url:
        success(f"public URL: {tunnel_url}")
    if token is not None:
        info(f"access token: {token}")
    info("Ctrl+C to stop")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        _teardown()


def stop(app_name: str) -> None:
    """Explicit teardown of a running app by name — distinct from Ctrl+C
    but routed through the same clean-teardown path in `serve` via SIGTERM.
    """
    app = registry.get(app_name)
    if app is None:
        error(f"no running app named {app_name!r}")
        raise SystemExit(1)

    try:
        os.kill(app.pid, signal.SIGTERM)
    except ProcessLookupError:
        registry.unregister(app_name)
        info(f"{app_name} wasn't actually running (stale registry entry removed)")
        return

    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            os.kill(app.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.2)
    else:
        error(f"{app_name} (pid {app.pid}) didn't stop within 10s")
        raise SystemExit(1)

    registry.unregister(app_name)
    success(f"{app_name} stopped")
