"""`sidepage serve` / `sidepage stop` — spec v3 §2, serving (pm2-style,
port-free from the caller's perspective).

`serve` infers the target kind (code/static/notebook, see
`sidepage.core.target`) from what's passed — `--type` stays as an override
for when inference is ambiguous or wrong, the same escape-hatch rationale
v1 had. No manual port handling by the caller: Sidepage allocates a real
port and injects it via `$PORT` or a recognized launcher flag. Traffic
never reaches the wrapped app directly — it goes through the local reverse
proxy first (`sidepage.core.reverse_proxy`), which sits between the tunnel
and the real app port.

Blocks the terminal. Killing the process tears down the tunnel
**immediately** — no grace period, confirmed default (graceful draining of
in-flight requests/open WS connections is deferred, not designed yet). `--background`
is explicitly ruled out in v3: the orchestrator (out of scope for this
binary, §16) owns all multi-process concerns; this binary stays
single-process/foreground by contract.

v4 adds `--env <SECRET_NAME>` (repeatable): named, per-app injection of
vault secrets (`sidepage.core.secrets_vault`, v4 §9) into the wrapped
process's environment. Nothing blanket — each name is looked up
individually and injection fails loud if a name isn't in the vault.

`--domain` is real: routes through a BYO Cloudflare tunnel
(`sidepage.core.tunnel_manager.open_byo_tunnel`) once configured via
`sidepage account domain set`. Mutually exclusive with `--anon` — one
tunnel mode at a time.

**`serve <target>` also accepts a registered app name** (registry spec
v2, `sidepage.core.app_registry`, `sidepage.commands.app_registry`): if
the positional argument matches something `sidepage app register`d, its
saved config is the base and any flags passed *this* invocation override
it field-by-field — see `sidepage.commands.app_registry.merge_with_registered`
for exactly how "was this flag explicitly passed this time" is
determined (`ctx.get_parameter_source`, not a `None`-default sentinel, so
every flag's natural default — e.g. `--auth open`, `--scope local` —
keeps working as a real default rather than colliding with "was it
passed"). An argument that doesn't match any registered name falls back
to the existing behavior unchanged: a literal target path.

v5 adds `--timeout`/`--idle-timeout` (auto-teardown, spec §20) and
`--peer <role>=<app-name>` (repeatable — injects another running served
app's URL as `SIDEPAGE_PEER_<ROLE>_URL`). All three are always taken from
*this* invocation, never from a registered app's saved config — same
treatment as `--token`, and for the same reason: none of the three is
part of `AppRegistration`, so there's nothing in the registry to merge
against.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from sidepage.commands.app_registry import merge_with_registered
from sidepage.core import app_registry
from sidepage.core.auth import AuthTier
from sidepage.core.directory_client import Scope
from sidepage.core.process import ServeConfig
from sidepage.core.process import serve as core_serve
from sidepage.core.process import stop as core_stop
from sidepage.core.target import TargetKind
from sidepage.output import error, not_implemented


def _parse_peer(spec: str) -> tuple[str, str]:
    """Parse one `--peer ROLE=APP-NAME` spec. Fails loud on a bad shape
    (missing `=`, empty role or app name) rather than silently dropping
    it or guessing — same posture as every other real validation in this
    module."""
    role, sep, app_name = spec.partition("=")
    role, app_name = role.strip(), app_name.strip()
    if not sep or not role or not app_name:
        raise ValueError(f"--peer {spec!r} must be ROLE=APP-NAME, e.g. --peer api=my-api")
    return role, app_name


class ServeTargetType(StrEnum):
    """Same set as `sidepage.core.target.TargetKind` plus AUTO, the default
    for `serve` — scaffolding via `new` always requires a concrete type;
    serving an existing target can usually infer it.
    """

    AUTO = "auto"
    CODE = "code"
    STATIC = "static"
    NOTEBOOK = "notebook"


def serve(
    ctx: typer.Context,
    target: Annotated[
        Path, typer.Argument(help="Script, directory, .ipynb, or a name from `sidepage app list`.")
    ],
    target_type: Annotated[
        ServeTargetType,
        typer.Option(
            "--type",
            help="Override auto-detection when it's ambiguous or wrong.",
        ),
    ] = ServeTargetType.AUTO,
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            help="Human-legible prefix; a dedupe suffix is appended.",
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
        typer.Option(
            "--auth", help="Auth tier, enforced by the local reverse proxy."
        ),
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
            "Independent of --auth — an anonymous tunnel can still require a token.",
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
    env: Annotated[
        list[str] | None,
        typer.Option(
            "--env",
            help="Inject a named secret from the vault (`sidepage secrets set`) as an env var "
            "in the wrapped process. Repeatable. Fails loud if the name isn't in the vault.",
        ),
    ] = None,
    guardrail: Annotated[
        Path | None,
        typer.Option(
            "--guardrail",
            help="[parked, not yet available] Pre/post-processing config.",
        ),
    ] = None,
    timeout: Annotated[
        float | None,
        typer.Option(
            "--timeout",
            help="Absolute lifetime in seconds, measured from start — torn down automatically "
            "once reached, same as Ctrl+C.",
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
    peer: Annotated[
        list[str] | None,
        typer.Option(
            "--peer",
            help="Inject another currently-running served app's URL as "
            "SIDEPAGE_PEER_<ROLE>_URL, e.g. --peer api=my-api. Repeatable. Resolved from the "
            "registry at start (fails loud if the named app isn't running); also re-resolved "
            "live via GET /.sidepage/peers.json. code/notebook targets only.",
        ),
    ] = None,
) -> None:
    """Serve a target and expose it through a tunnel. Blocks the terminal;
    Ctrl+C tears the tunnel down immediately."""
    try:
        peers = tuple(_parse_peer(p) for p in (peer or ()))
    except ValueError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    registered = app_registry.get(str(target))
    if registered is None:
        target_kind = None if target_type == ServeTargetType.AUTO else TargetKind(target_type.value)
        config = ServeConfig(
            target=target,
            target_kind=target_kind,
            name=name,
            domain=domain,
            auth=auth,
            scope=scope,
            anon=anon,
            token=token,
            env_secrets=tuple(env or ()),
            guardrail=guardrail,
            timeout=timeout,
            idle_timeout=idle_timeout,
            peers=peers,
        )
    else:
        merged = merge_with_registered(
            ctx,
            registered,
            target_type=target_type,
            name=name,
            domain=domain,
            auth=auth,
            scope=scope,
            anon=anon,
            env=list(env or ()),
            guardrail=guardrail,
        )
        if merged["name"] is None:
            # Registered but no explicit --name at registration time, and
            # none passed this invocation either: the registry key itself
            # is the natural default identity — closer to what a user
            # typing `sidepage serve abc-app` expects than falling through
            # to the underlying file's stem (ServeConfig's own default),
            # which could be a completely different, less meaningful name.
            merged["name"] = str(target)
        config = ServeConfig(
            target=registered.target,
            token=token,
            timeout=timeout,
            idle_timeout=idle_timeout,
            peers=peers,
            **merged,
        )
    try:
        core_serve(config)
    except NotImplementedError as exc:
        not_implemented(f"sidepage serve — {exc}", implemented_by="sidepage.core.process.serve")
    except Exception as exc:
        # Top-level CLI boundary — report and exit cleanly instead of a raw traceback.
        error(str(exc))
        raise typer.Exit(1) from exc


def stop(
    app_name: Annotated[str, typer.Argument(help="Name of the running app to tear down.")],
) -> None:
    """Explicit, non-interactive teardown of a running app — same immediate
    (no grace period) semantics as Ctrl+C."""
    core_stop(app_name)
