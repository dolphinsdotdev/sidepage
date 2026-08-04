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
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from sidepage.core.auth import AuthTier
from sidepage.core.directory_client import Scope
from sidepage.core.process import ServeConfig
from sidepage.core.process import serve as core_serve
from sidepage.core.process import stop as core_stop
from sidepage.core.target import TargetKind
from sidepage.output import error, not_implemented


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
    target: Annotated[
        Path, typer.Argument(help="Script, directory, or .ipynb to serve.")
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
) -> None:
    """Serve a target and expose it through a tunnel. Blocks the terminal;
    Ctrl+C tears the tunnel down immediately."""
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
