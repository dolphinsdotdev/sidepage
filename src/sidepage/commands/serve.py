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

`--pwa`/`--pwa-*` (`sidepage.core.pwa`) makes the served app installable
to a phone home screen — synthetic manifest/service-worker routes plus
HTML `<head>` injection, entirely at the reverse-proxy layer
(`sidepage.core.reverse_proxy`), never touching the wrapped app. `--qr`
prints a terminal QR code for the resulting URL, independent of `--pwa`.

**`--pwa*` *is* part of `AppRegistration` and merges** — unlike
`--timeout`/`--idle-timeout`/`--peer`/`--qr` above, which stay
per-invocation. An installed app's name, icon, and theme are part of
what the served app is, not how one run of it behaves, so a saved config
that dropped them wouldn't reproduce the app it was saved from. It merges
as one unit rather than field by field: any explicit `--pwa*` flag this
invocation replaces the registered PWA config wholesale, and passing none
of them applies the registered one — see
`sidepage.commands.app_registry.merge_with_registered`.

`--autoregister` saves this invocation to the app registry once the app
is actually up (`sidepage.core.process.serve`), so a one-off `serve`
becomes a replayable `serve <app-name>` without a second command. Flags
the registry can't hold are reported by name rather than dropped
silently, and re-running an already-registered identical config is a
no-op with a pointer at the shorter command, not an error.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer

from sidepage import output
from sidepage.commands.app_registry import merge_with_registered
from sidepage.core import app_registry
from sidepage.core.auth import AuthTier
from sidepage.core.directory_client import Scope
from sidepage.core.exceptions import UntrustedSourceError
from sidepage.core.process import ServeConfig
from sidepage.core.process import serve as core_serve
from sidepage.core.process import stop as core_stop
from sidepage.core.pwa import PwaDisplay, PwaOptions
from sidepage.core.target import TargetKind
from sidepage.output import error, not_implemented, plain, warn


def _require_source_trust(
    app_name: str, registered: app_registry.AppRegistration, *, waived: bool
) -> None:
    """Gate execution of code sidepage downloaded, until a human approves
    the exact commit about to run.

    **Why this exists:** `sidepage pull` fetches a stranger's code, and
    `serve` runs it on a machine that also holds an encrypted secrets
    vault, the user's own projects, and their shell credentials. Hugging
    Face puts a warning in front of the equivalent action for the same
    reason. A convenience that lets `serve <arbitrary-url>` execute
    without a gate isn't a convenience, it's a remote-code-execution path
    with a friendly name.

    **Trust attaches to a commit, not a name.** `AppSource.trusted_commit`
    records what was approved; `pull` always fetches the source's current
    state and rewrites the entry, so newly downloaded code arrives with a
    different `commit` and no `trusted_commit`, and this prompt re-arms
    by construction. Approving `serve foo` once does not silently approve
    whatever `foo` becomes later.

    **No prompt means no execution.** Without a terminal — an agent, a CI
    job, a piped invocation — there is nobody to ask, so this raises
    rather than defaulting to yes. `--trust-remote-code` is the explicit
    waiver, which keeps the decision a thing someone typed on purpose.

    Locally-registered apps (`source is None`) never reach any of this:
    sidepage didn't download them, and gating a user's own file would be
    theater.
    """
    source = registered.source
    if source is None or source.trusted_commit == source.commit:
        return

    if waived:
        warn(
            f"{app_name}: running downloaded code from {source.url} @ {source.commit[:7]} "
            "without review (--trust-remote-code)"
        )
        app_registry.set_trusted_commit(app_name, source.commit)
        return

    previously_trusted = source.trusted_commit is not None
    plain.print()
    plain.print("  [bold yellow]about to run code sidepage downloaded[/bold yellow]")
    plain.print(f"  source   {source.url}")
    plain.print(f"  commit   {source.commit[:7]}")
    if source.hf is not None:
        sdk = source.hf.sdk or "unknown"
        if source.hf.sdk_version:
            sdk += f" {source.hf.sdk_version}"
        plain.print(f"  sdk      {sdk}")
    plain.print(f"  entry    {registered.target}")
    if source.env_requested:
        plain.print(f"  wants    {', '.join(source.env_requested)}  [dim](not granted)[/dim]")
    if previously_trusted:
        plain.print(
            f"  [yellow]changed[/yellow]  you approved {source.trusted_commit[:7]} before; "
            "this is different code"
        )
    plain.print()

    if not output.is_interactive():
        raise UntrustedSourceError(
            f"{app_name} runs code downloaded from {source.url} and hasn't been approved at "
            f"commit {source.commit[:7]}. There's no terminal here to confirm at, so sidepage "
            "won't execute it. Review the source, then re-run with --trust-remote-code."
        )

    if not typer.confirm("  run it?", default=False):
        raise UntrustedSourceError(f"{app_name} was not started")
    app_registry.set_trusted_commit(app_name, source.commit)


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


#: Every `--pwa*` parameter name, in `serve`'s own parameter/`ctx.params`
#: spelling. Single source of truth for both building `PwaOptions` and
#: deciding whether *this* invocation set any PWA flag at all — the
#: registry merge needs the second question answered (see
#: `sidepage.commands.app_registry.merge_with_registered`), and a list
#: that could drift from the real flag set would silently break it the
#: next time a `--pwa-*` flag is added.
PWA_PARAM_FIELDS = (
    "pwa",
    "pwa_name",
    "pwa_short_name",
    "pwa_theme",
    "pwa_bg",
    "pwa_icon",
    "pwa_display",
    "pwa_manifest",
    "pwa_force",
    "pwa_no_sw",
)


def build_pwa_options(params: Mapping[str, Any]) -> PwaOptions | None:
    """Build `PwaOptions` from a mapping keyed by `PWA_PARAM_FIELDS`, or
    `None` when `--pwa` is off.

    Takes a mapping rather than keyword arguments so the two callers can
    pass what they already have without either restating the field
    mapping: `serve` passes its own (already Typer-typed) parameters, and
    `sidepage.commands.app_registry` passes a `make_context()`-parsed
    `ctx.params` straight through, whose values are plain strings for the
    same reason `_coerce_raw_params` exists. The coercions below are
    therefore written to be idempotent — `Path(Path(...))` and
    `PwaDisplay(PwaDisplay.X)` both round-trip.
    """
    if not params["pwa"]:
        return None

    def _path(value: object) -> Path | None:
        return Path(str(value)) if value else None

    return PwaOptions(
        name=params["pwa_name"],
        short_name=params["pwa_short_name"],
        theme=params["pwa_theme"],
        bg=params["pwa_bg"],
        icon=_path(params["pwa_icon"]),
        display=PwaDisplay(params["pwa_display"]),
        manifest=_path(params["pwa_manifest"]),
        force=bool(params["pwa_force"]),
        no_sw=bool(params["pwa_no_sw"]),
    )


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
    pwa: Annotated[
        bool,
        typer.Option("--pwa", help="Make this app installable to a phone home screen."),
    ] = False,
    pwa_name: Annotated[
        str | None,
        typer.Option("--pwa-name", help="App name. [default: resolved app name]"),
    ] = None,
    pwa_short_name: Annotated[
        str | None,
        typer.Option(
            "--pwa-short-name",
            help="Home screen label. [default: --pwa-name truncated to 12 chars]",
        ),
    ] = None,
    pwa_theme: Annotated[
        str, typer.Option("--pwa-theme", help="Theme / status bar hex color.")
    ] = "#111111",
    pwa_bg: Annotated[
        str, typer.Option("--pwa-bg", help="Splash background hex color.")
    ] = "#ffffff",
    pwa_icon: Annotated[
        Path | None,
        typer.Option(
            "--pwa-icon", help="Source PNG, square, >=512px. [default: bundled sidepage mark]"
        ),
    ] = None,
    pwa_display: Annotated[
        PwaDisplay, typer.Option("--pwa-display", help="Display mode.")
    ] = PwaDisplay.STANDALONE,
    pwa_manifest: Annotated[
        Path | None,
        typer.Option(
            "--pwa-manifest",
            help="Serve this file verbatim as the manifest; ignores --pwa-name/-short-name/"
            "-theme/-bg/-icon/-display.",
        ),
    ] = None,
    pwa_force: Annotated[
        bool,
        typer.Option(
            "--pwa-force",
            help="Inject sidepage's manifest link even if the app already ships its own.",
        ),
    ] = False,
    pwa_no_sw: Annotated[
        bool, typer.Option("--pwa-no-sw", help="Manifest only, no service worker.")
    ] = False,
    qr: Annotated[
        bool,
        typer.Option("--qr", help="Print a terminal QR code for the resulting URL."),
    ] = False,
    trust_remote_code: Annotated[
        bool,
        typer.Option(
            "--trust-remote-code",
            help="Skip the confirmation prompt for an app whose code sidepage downloaded. "
            "Required in non-interactive contexts, where there's nobody to prompt.",
        ),
    ] = False,
    autoregister: Annotated[
        bool,
        typer.Option(
            "--autoregister",
            help="Also save this invocation to the app registry (as `sidepage app register` "
            "would) once it's up, so `sidepage serve <app-name>` replays it. Flags the "
            "registry doesn't store are listed explicitly rather than dropped silently.",
        ),
    ] = False,
) -> None:
    """Serve a target and expose it through a tunnel. Blocks the terminal;
    Ctrl+C tears the tunnel down immediately."""
    try:
        peers = tuple(_parse_peer(p) for p in (peer or ()))
    except ValueError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    def _explicit(field: str) -> bool:
        source = ctx.get_parameter_source(field)
        return source is not None and source.name == "COMMANDLINE"

    # --pwa-* fields that build the generated manifest's *content* — the
    # ones pwa.build_runtime actually ignores when --pwa-manifest is set
    # (see that module's docstring for why --pwa-force/--pwa-no-sw are
    # deliberately not in this list: they control the injection process,
    # not manifest content, so they stay honored even with --pwa-manifest).
    _pwa_manifest_content_fields = (
        "pwa_name",
        "pwa_short_name",
        "pwa_theme",
        "pwa_bg",
        "pwa_icon",
        "pwa_display",
    )
    _pwa_process_fields = ("pwa_manifest", "pwa_force", "pwa_no_sw")
    pwa_flags_explicit = any(
        _explicit(f) for f in (*_pwa_manifest_content_fields, *_pwa_process_fields)
    )
    if pwa_flags_explicit and not pwa:
        error("--pwa-* flags require --pwa — pass --pwa to enable PWA mode.")
        raise typer.Exit(1)
    if _explicit("pwa_manifest") and any(_explicit(f) for f in _pwa_manifest_content_fields):
        warn(
            "--pwa-manifest is served verbatim — --pwa-name/-short-name/-theme/-bg/-icon/"
            "-display are ignored"
        )

    pwa_options = build_pwa_options(
        {
            "pwa": pwa,
            "pwa_name": pwa_name,
            "pwa_short_name": pwa_short_name,
            "pwa_theme": pwa_theme,
            "pwa_bg": pwa_bg,
            "pwa_icon": pwa_icon,
            "pwa_display": pwa_display,
            "pwa_manifest": pwa_manifest,
            "pwa_force": pwa_force,
            "pwa_no_sw": pwa_no_sw,
        }
    )

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
            pwa=pwa_options,
            qr=qr,
            autoregister=autoregister,
        )
    else:
        # Before anything else in this branch: if this app's code was
        # downloaded rather than written by the user, it doesn't run
        # until a human has approved this exact commit.
        try:
            _require_source_trust(str(target), registered, waived=trust_remote_code)
        except UntrustedSourceError as exc:
            error(str(exc))
            raise typer.Exit(1) from exc

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
            pwa=pwa_options,
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
            qr=qr,
            autoregister=autoregister,
            **merged,  # includes `pwa` — merged, not taken from this invocation unconditionally
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
