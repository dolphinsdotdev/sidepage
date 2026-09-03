"""`sidepage proxy` — wrap an *already-running* local service with
sidepage's proxy/auth/tunnel stack, the same way `sidepage serve` wraps a
target it launches itself.

The one structural difference from `serve`: sidepage never launches or
owns the wrapped process. `--port` is assumed already listening on
`127.0.0.1`; Ctrl+C / `sidepage stop <app-name>` tear down the sidepage
side (proxy, tunnel, registry entry) only, and the service you pointed at
keeps running exactly as it was. See `proxy()`'s docstring below — the
full text is what `--help` shows — for the caveats this implies (an
app-level "only localhost can reach this" security assumption is
silently defeated, since every proxied request now arrives at the app
from 127.0.0.1; Origin/Host/CSRF checks that don't trust a proxy still
need the app's own config updated even though the real `Host`/
`X-Forwarded-*` are now forwarded correctly, see
`sidepage.core.reverse_proxy`; OAuth/SSO is close to structurally
incompatible with `--anon`'s per-run-random hostname).

`--type`/`--env`/`--guardrail`/`--peer`/`--autoregister` are declared here purely so a
user who tries them gets one specific, actionable message instead of
Typer's generic "no such option" — they're subprocess-injection concepts
that don't apply to a process `proxy` doesn't own, not features that are
merely unbuilt yet, so they're rejected outright rather than accepted and
silently ignored (see `sidepage.commands.serve` for the same posture
applied to genuinely-unbuilt flags like `--guardrail` there).
"""

from __future__ import annotations

from typing import Annotated

import typer

from sidepage.core.auth import AuthTier
from sidepage.core.directory_client import Scope
from sidepage.core.process import ProxyConfig
from sidepage.core.process import proxy as core_proxy
from sidepage.output import error, set_json_mode


def proxy(
    port: Annotated[
        int,
        typer.Option("--port", help="Port of the already-running local service, on 127.0.0.1."),
    ],
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            help="App name. Defaults to proxy-<port> for local-only use; required when "
            "--domain or --anon is set, since it becomes part of the public hostname/URL.",
        ),
    ] = None,
    domain: Annotated[
        str | None,
        typer.Option(
            "--domain",
            help="Bring-your-own-domain. Must match the domain already configured via "
            "`sidepage account domain set`; served at <app-name>-<id>.<domain>. Default with "
            "neither --domain nor --anon is local-only (no brokered tunnel — no backend exists).",
        ),
    ] = None,
    auth: Annotated[
        AuthTier,
        typer.Option("--auth", help="Auth tier, enforced by the local reverse proxy."),
    ] = AuthTier.OPEN,
    scope: Annotated[
        Scope,
        typer.Option("--scope", help="Directory visibility for this app."),
    ] = Scope.LOCAL,
    anon: Annotated[
        bool,
        typer.Option(
            "--anon",
            help="Anonymous Cloudflare Quick Tunnel: no broker call, no directory entry. "
            "Independent of --auth — an anonymous tunnel can still require a token. Hostname "
            "changes every run — see the warning printed at startup about OAuth/SSO apps.",
        ),
    ] = False,
    token: Annotated[
        str | None,
        typer.Option(
            "--token",
            help="Explicit token value for --auth token. Prefer SIDEPAGE_TOKEN over this "
            "(shell history / `ps aux` exposure). Auto-generated and printed once if omitted.",
        ),
    ] = None,
    timeout: Annotated[
        float | None,
        typer.Option(
            "--timeout",
            help="Absolute lifetime in seconds, measured from start — torn down automatically "
            "once reached, same as Ctrl+C. Only tears down the proxy, never the service.",
        ),
    ] = None,
    idle_timeout: Annotated[
        float | None,
        typer.Option(
            "--idle-timeout",
            help="Idle lifetime in seconds — resets on every proxied request or WebSocket "
            "message; torn down automatically once no traffic arrives within this window.",
        ),
    ] = None,
    detach: Annotated[
        bool,
        typer.Option(
            "--detach",
            "-d",
            help="Start in the background and return once the proxy is up (or has failed), "
            "instead of blocking. Output goes to a log file whose path is reported. Stop it "
            "with `sidepage stop <app-name>` — which does not stop the service on --port.",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Print one line of JSON to stdout describing the running proxy. "
            "Human-readable output moves to stderr. Works with or without --detach.",
        ),
    ] = False,
    target_type: Annotated[
        str | None,
        typer.Option(
            "--type",
            help="Not applicable to `proxy` — there's no file to detect a type from.",
        ),
    ] = None,
    env: Annotated[
        list[str] | None,
        typer.Option(
            "--env",
            help="Not applicable to `proxy` — sidepage doesn't own the process on --port.",
        ),
    ] = None,
    guardrail: Annotated[
        str | None,
        typer.Option(
            "--guardrail",
            help="Not applicable to `proxy` (also still unimplemented for `serve`).",
        ),
    ] = None,
    peer: Annotated[
        list[str] | None,
        typer.Option(
            "--peer",
            help="Not applicable to `proxy` — there's no subprocess to inject "
            "SIDEPAGE_PEER_<ROLE>_URL into.",
        ),
    ] = None,
    autoregister: Annotated[
        bool,
        typer.Option(
            "--autoregister",
            help="Not applicable to `proxy` — the app registry stores `serve` configs, "
            "which are keyed on a target `proxy` doesn't have.",
        ),
    ] = False,
) -> None:
    """Proxy an already-running local service through sidepage's reverse
    proxy/auth/tunnel stack — same auth tiers, same BYO-domain/--anon
    tunneling, same registry/`sidepage ls`/`sidepage stop` as `serve`, but
    for a process sidepage never launches and never owns.

    \b
    TEARDOWN: Ctrl+C / `sidepage stop <name>` tears down the proxy, the
    tunnel, and the registry entry only — YOUR SERVICE KEEPS RUNNING.
    Sidepage never started it, so it can't and won't stop it.

    \b
    SECURITY: every proxied request reaches your app from 127.0.0.1 (this
    proxy's own address). Any app-level logic that trusts "the connection
    came from localhost" instead of checking X-Forwarded-For — debug
    endpoints, admin panels, and pointedly Flask/Werkzeug's interactive
    debugger, a known RCE if reachable — now treats every remote caller as
    local, regardless of --auth. Disable local-only debug/admin surfaces
    before proxying them publicly.

    \b
    ORIGIN / HOST / CSRF: the real Host and X-Forwarded-Host/-Proto/-For
    are forwarded to your app on HTTP requests (see
    sidepage.core.reverse_proxy), but that only helps an app configured to
    trust them. On WebSocket connections only X-Forwarded-Host is
    forwarded, not the literal Host header — verified live that some
    WS servers (Jupyter/Tornado) reject a forwarded real hostname on the
    WS handshake outright, the same allowlist-style check Vite does below.
    One-time fixes:
      - Django: USE_X_FORWARDED_HOST=True, SECURE_PROXY_SSL_HEADER=
        ('HTTP_X_FORWARDED_PROTO','https'), add the domain to
        ALLOWED_HOSTS / CSRF_TRUSTED_ORIGINS.
      - Flask: wrap with werkzeug.middleware.proxy_fix.ProxyFix.
      - FastAPI/Starlette: add uvicorn.middleware.proxy_headers.
        ProxyHeadersMiddleware.
      - Express: app.set('trust proxy', true).
      - Rails: usually nothing extra (loopback trusted by default).
      - Vite dev server: add to server.allowedHosts — forwarding does NOT
        fix this, Vite checks the raw Host value, not X-Forwarded-Host.
      - --anon specifically: the hostname is random and changes every
        run — use a WILDCARD entry (.trycloudflare.com), not an exact
        match, or it breaks again next run.

    \b
    OAUTH / SSO: effectively incompatible with --anon — providers require
    an exact, pre-registered redirect URI and disallow wildcard
    subdomains, so a hostname that changes every run can never be
    registered. Use --domain for anything doing OAuth login.

    \b
    PROTOCOL SCOPE: HTTP/1.1 and WebSocket only — a port serving raw TCP
    or gRPC won't work through this command.

    """
    if json_output:
        set_json_mode(True)
    if target_type is not None:
        error(
            "--type doesn't apply to `proxy` — there's no file to detect a type from; "
            "proxy always treats --port as a running HTTP/WS service."
        )
        raise typer.Exit(1)
    if env:
        error(
            "--env doesn't apply to `proxy` — sidepage doesn't own the process on --port, "
            "so there's nothing to inject environment variables into. Configure the app's "
            "environment before starting it yourself."
        )
        raise typer.Exit(1)
    if guardrail is not None:
        error(
            "--guardrail doesn't apply to `proxy` — there's no subprocess request/response "
            "hook to attach to (also still unimplemented for `serve`, see "
            "`sidepage serve --help`)."
        )
        raise typer.Exit(1)
    if peer:
        error(
            "--peer doesn't apply to `proxy` — SIDEPAGE_PEER_<ROLE>_URL injection requires a "
            "subprocess sidepage controls; a --port target already has its own environment."
        )
        raise typer.Exit(1)
    if autoregister:
        error(
            "--autoregister doesn't apply to `proxy` — the app registry stores `serve` "
            "invocations, every one of which is keyed on a target to launch. A `proxy` call "
            "has no target (the service on --port is already running, and sidepage didn't "
            "start it), so there's nothing to save that `sidepage serve <app-name>` could "
            "replay."
        )
        raise typer.Exit(1)

    if name is None:
        if domain is not None or anon:
            error(
                "--name is required with --domain or --anon — it becomes part of the public "
                "hostname/registry entry, so it isn't auto-generated for those."
            )
            raise typer.Exit(1)
        name = f"proxy-{port}"

    config = ProxyConfig(
        port=port,
        name=name,
        domain=domain,
        auth=auth,
        scope=scope,
        anon=anon,
        token=token,
        timeout=timeout,
        idle_timeout=idle_timeout,
        detach=detach,
        json_output=json_output,
    )
    try:
        core_proxy(config)
    except Exception as exc:
        # Top-level CLI boundary — report and exit cleanly instead of a raw traceback.
        error(str(exc))
        raise typer.Exit(1) from exc
