"""`sidepage app register|list|show|unregister` plus `sidepage serve
<app-name>` — registry spec v2 (`sidepage-registry-spec.md`).

**The parser-reuse mechanism, and why it's implemented here rather than
in `sidepage.core.app_registry`:** the spec's core design goal is that a
new `serve` flag automatically becomes registerable with zero changes on
this side — achieved by parsing a registration invocation string with
`serve`'s own real Click command object (`serve_cmd.make_context(...)`),
not a hand-maintained second parser. That needs a runtime import of
`sidepage.cli` (to fetch the fully-assembled Click command tree) — which
would be circular at module-load time, since `cli.py` imports this module
to wire up `sidepage app ...` in the first place. Deferred (function-body,
not module-top) imports below exist specifically to break that cycle;
`sidepage.core.app_registry` itself stays free of any `typer`/`click`
import, consistent with every other `core` module never importing from
`commands` or `cli`.

**A wrinkle worth recording:** this installed Typer version (0.27.1) fully
vendors its own fork of Click (`typer._click`) rather than depending on
the real `click` package for its command/context machinery — confirmed by
observing that errors raised from `Command.make_context(...)` here are
`typer._click.exceptions.*` instances, not `click.exceptions.*` (the two
are unrelated classes, even though a real `click` distribution is also
separately installed and importable). That's why parsing failures below
are caught as a broad `Exception` rather than the specific/expected
`click.ClickException` a normal Click integration would use — there's no
public Typer symbol for that private base class to catch instead, and
reaching into the private `typer._click` module by name would be more
fragile than a broad catch scoped tightly around one parse call.

**`sidepage serve <app-name>` merge semantics** (`merge_with_registered`
below) are the same for a real invocation and for `sidepage app show
--with`'s preview — both ultimately answer "for each mergeable field, did
*this* invocation pass it explicitly (`ctx.get_parameter_source(...) is
COMMANDLINE`), or should the registered value apply?" `sidepage.commands.serve`
calls this directly with its own already-Typer-typed parameters; `show
--with` calls it with values coerced from a `make_context()`-parsed
string, since raw `ctx.params` values are always plain strings/primitives
regardless of the declared parameter type (confirmed live — Typer's
type-conversion happens when it calls the real command function, not
when populating `ctx.params`) — see `_coerce_raw_params`.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Annotated, Any

import typer

from sidepage import output
from sidepage.config.settings import app_source_dir
from sidepage.core import app_registry, registry
from sidepage.core import pull as core_pull
from sidepage.core.app_registry import AppRegistration
from sidepage.core.auth import AuthTier
from sidepage.core.directory_client import Scope
from sidepage.core.exceptions import (
    AppNotRegisteredError,
    AppRegistrationError,
    SourceError,
    TargetDetectionError,
)
from sidepage.core.pwa import PwaOptions
from sidepage.core.target import TargetKind, detect_target_kind
from sidepage.output import error, info, plain, stdout, success

app_app = typer.Typer(
    name="app",
    help="Save a `serve` invocation under a short name and re-run it later.",
    no_args_is_help=True,
)


def _serve_click_command():
    """The real, live `serve` Click command object — fetched at call time,
    not import time, to avoid the `cli.py` <-> this module import cycle
    described in this module's docstring."""
    import typer as typer_module

    from sidepage.cli import app as cli_app

    click_group = typer_module.main.get_command(cli_app)
    return click_group.commands["serve"]


def _make_serve_context(invocation: str, *, placeholder_target: bool = False):
    """Parse `invocation` (shell-quoted `serve` flags, e.g. `"abc.py
    --auth token"`) using `serve`'s own Click command — the single source
    of truth this whole module exists to reuse rather than duplicate. Set
    `placeholder_target=True` for a preview string that omits the target
    entirely (`sidepage app show --with`, where the target is implied by
    the already-registered app, not part of what's being previewed) —
    `serve`'s positional `target` argument is required, so a harmless
    placeholder is prepended and never read back out.
    """
    args = shlex.split(invocation)
    if placeholder_target:
        args = ["__sidepage_placeholder_target__", *args]
    try:
        return _serve_click_command().make_context("serve", args)
    except Exception as exc:
        raise AppRegistrationError(f"could not parse {invocation!r} as serve flags: {exc}") from exc


def _coerce_raw_params(raw: dict) -> dict:
    """`ctx.params` from `make_context()` holds plain strings/primitives
    regardless of the real parameter type (confirmed live, not assumed —
    see module docstring) — this turns them into the same typed values a
    real `serve` invocation's function parameters would already have."""
    from sidepage.commands.serve import ServeTargetType, build_pwa_options

    return {
        "target": Path(raw["target"]),
        "target_type": ServeTargetType(raw["target_type"]),
        "name": raw["name"],
        "domain": raw["domain"],
        "auth": AuthTier(raw["auth"]),
        "scope": Scope(raw["scope"]),
        "anon": raw["anon"],
        "token": raw["token"],
        "env": list(raw["env"] or ()),
        "guardrail": Path(raw["guardrail"]) if raw["guardrail"] else None,
        # Built through `serve`'s own helper rather than restated here,
        # for the same reason this module parses with `serve`'s own Click
        # command: a new `--pwa-*` flag should need no change on this side.
        "pwa": build_pwa_options(raw),
    }


def _is_explicit(ctx, field: str) -> bool:
    source = ctx.get_parameter_source(field)
    return source is not None and source.name == "COMMANDLINE"


def merge_with_registered(
    ctx,
    registered: AppRegistration,
    *,
    target_type,
    name: str | None,
    domain: str | None,
    auth: AuthTier,
    scope: Scope,
    anon: bool,
    env: list[str],
    guardrail: Path | None,
    pwa: PwaOptions | None = None,
) -> dict[str, Any]:
    """For each mergeable field, an explicit command-line value (source
    `COMMANDLINE` on `ctx`, per `ctx.get_parameter_source`) overrides the
    registered one; otherwise the registered value applies — the spec's
    resolved merge semantics (override-wins, base config never mutated by
    a one-off override). `target`/`token` are deliberately excluded:
    `target` is implied by which registered app you're serving, not a
    mergeable flag, and `token` is never stored at all (process-scoped,
    always whatever this invocation supplies, registered or not).

    `env`'s merge is **replace, not append**: an explicit `--env` on this
    invocation replaces the registered env list entirely rather than
    accumulating with it — the spec's example only exercises a scalar
    override (`--scope`), so this is a judgment call, made for consistency
    with every other field's replace semantics rather than inventing a
    different rule for the one list-valued flag.

    **`--pwa*` merges as one unit, not field by field**: if this
    invocation passed *any* `--pwa*` flag, its whole PWA config wins;
    otherwise the registered one applies unchanged. Field-by-field
    merging would mean `--pwa-theme` alone silently inheriting a
    registered `--pwa-icon` and `--pwa-manifest`, which is both harder to
    predict and impossible to express the other way (there'd be no way to
    say "PWA, but none of the saved settings"). It also keeps `serve`'s
    existing "`--pwa-*` requires `--pwa`" validation meaningful: a
    partial override is rejected up front rather than half-applied.

    Returns a dict shaped as `ServeConfig`'s own keyword arguments
    (`target_kind`, `name`, `domain`, `auth`, `scope`, `anon`,
    `env_secrets`, `guardrail`, `pwa`) — ready to splice into
    `ServeConfig(target=..., token=..., **merged)`.
    """
    from sidepage.commands.serve import PWA_PARAM_FIELDS, ServeTargetType

    if _is_explicit(ctx, "target_type"):
        target_kind = None if target_type is ServeTargetType.AUTO else TargetKind(target_type.value)
    else:
        target_kind = registered.target_kind

    def use(field: str, live_value, registered_value):
        return live_value if _is_explicit(ctx, field) else registered_value

    return {
        "target_kind": target_kind,
        "name": use("name", name, registered.name),
        "domain": use("domain", domain, registered.domain),
        "auth": use("auth", auth, registered.auth),
        "scope": use("scope", scope, registered.scope),
        "anon": use("anon", anon, registered.anon),
        "env_secrets": tuple(env) if _is_explicit(ctx, "env") else registered.env_secrets,
        "guardrail": use("guardrail", guardrail, registered.guardrail),
        "pwa": pwa if any(_is_explicit(ctx, f) for f in PWA_PARAM_FIELDS) else registered.pwa,
    }


def _or_none(value: object, label: str = "(none)") -> str:
    return str(value) if value is not None else f"[dim]{label}[/dim]"


def _describe_pwa(pwa: PwaOptions | None) -> str:
    """One-line summary of a stored PWA config — the fields that actually
    distinguish two of them, not every default."""
    if pwa is None:
        return "[dim](off)[/dim]"
    parts = [f"name {pwa.name}" if pwa.name else "name (defaults to app-name)"]
    if pwa.manifest is not None:
        parts.append(f"manifest {pwa.manifest} (served verbatim)")
    else:
        parts.append(f"theme {pwa.theme}")
        parts.append(f"bg {pwa.bg}")
        parts.append(f"icon {pwa.icon}" if pwa.icon is not None else "icon (bundled default)")
        parts.append(f"display {pwa.display.value}")
    if pwa.force:
        parts.append("force")
    if pwa.no_sw:
        parts.append("no service worker")
    return " · ".join(parts)


def _print_source(source) -> None:
    """Provenance block for a pulled app. Printed as its own section
    because it answers a different question from the rest of the config:
    not "how will this run" but "whose code is this, and have I agreed to
    run it?"
    """
    plain.print(f"  source:         {source.url}")
    plain.print(f"  commit:         {source.commit[:7]}")
    if source.hf is not None:
        sdk = source.hf.sdk or "unknown"
        if source.hf.sdk_version:
            sdk += f" {source.hf.sdk_version}"
        if source.hf.python_version:
            sdk += f" · python {source.hf.python_version}"
        plain.print(f"  hf sdk:         {sdk}")
    if source.env_requested:
        plain.print(
            f"  requests env:   {', '.join(source.env_requested)} "
            "[dim](not granted — pass --env to grant)[/dim]"
        )
    if source.trusted_commit == source.commit:
        plain.print(f"  trusted:        yes, at {source.commit[:7]}")
    elif source.trusted_commit is None:
        plain.print("  trusted:        [yellow]no — `serve` will ask before running it[/yellow]")
    else:
        plain.print(
            f"  trusted:        [yellow]{source.trusted_commit[:7]} only — the code changed, "
            "`serve` will ask again[/yellow]"
        )


def _print_registration(app_name: str, r: AppRegistration) -> None:
    stdout.print(f"[bold]{app_name}[/bold]")
    stdout.print(f"  target:         {r.target}")
    stdout.print(f"  type:           {r.target_kind.value}")
    stdout.print(f"  name:           {_or_none(r.name, '(defaults to app-name)')}")
    stdout.print(f"  domain:         {_or_none(r.domain)}")
    stdout.print(f"  auth:           {r.auth.value}")
    stdout.print(f"  scope:          {r.scope.value}")
    stdout.print(f"  anon:           {r.anon}")
    stdout.print(f"  env:            {', '.join(r.env_secrets) or '[dim](none)[/dim]'}")
    stdout.print(f"  guardrail:      {_or_none(r.guardrail)}")
    stdout.print(f"  pwa:            {_describe_pwa(r.pwa)}")
    stdout.print(f"  registered_at:  {r.registered_at}")
    if r.source is not None:
        _print_source(r.source)


@app_app.command("register")
def register(
    invocation: Annotated[
        str,
        typer.Argument(
            help="The target plus any serve flags, as one shell-quoted string, "
            'e.g. "abc.py --auth token".'
        ),
    ],
    app_name: Annotated[str, typer.Argument(help="Short name to register this invocation under.")],
) -> None:
    """Save a `serve` invocation under `app_name` for later `sidepage
    serve <app-name>`. Parsed with `serve`'s own flags — a `--type` isn't
    stored as given, it's resolved (auto-detection runs now, at
    registration time, not deferred to every future `serve` call).

    Rejects a literal `--token <value>` outright: auth tokens are
    process-scoped and regenerate on each `serve` call, so persisting one
    here would quietly reintroduce a plaintext, indefinitely-stored secret
    — the exact separation the vault (`sidepage secrets`) and the runtime
    token file already establish. `--env <NAME>` is fine to store: it's a
    vault reference, not a value.
    """
    ctx = _make_serve_context(invocation)
    raw = ctx.params

    if raw["token"] is not None:
        error(
            "cannot register an app with a literal --token value.\n"
            "Auth tokens are process-scoped and regenerate on each serve — omit --token "
            "and one will be issued fresh each time this app is served."
        )
        raise typer.Exit(1)

    values = _coerce_raw_params(raw)

    try:
        target_kind = detect_target_kind(
            values["target"],
            override=None
            if values["target_type"].value == "auto"
            else TargetKind(values["target_type"].value),
        )
        registration = app_registry.register(
            app_name,
            target=values["target"].resolve(),
            target_kind=target_kind,
            name=values["name"],
            domain=values["domain"],
            auth=values["auth"],
            scope=values["scope"],
            anon=values["anon"],
            env_secrets=tuple(values["env"]),
            guardrail=values["guardrail"],
            pwa=values["pwa"],
        )
    except (AppRegistrationError, TargetDetectionError) as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    success(f"registered {app_name!r} -> {registration.target} ({registration.target_kind.value})")


@app_app.command("list")
def list_() -> None:
    """List registered app names."""
    names = app_registry.list_registered()
    if not names:
        stdout.print("[dim]no apps registered[/dim]")
        return
    for name in names:
        stdout.print(name)


@app_app.command("show")
def show(
    app_name: Annotated[str, typer.Argument(help="Registered app name.")],
    with_: Annotated[
        str | None,
        typer.Option(
            "--with",
            help="Preview the effective config if these serve flags were also passed, "
            'e.g. --with "--scope web" — the same merge `serve <app-name>` would do, '
            "without actually running it.",
        ),
    ] = None,
) -> None:
    """Show a registered app's saved config — or, with `--with`, the
    effective merged config it would run with if those extra flags were
    passed to `sidepage serve <app-name>` too. Inspectable before it
    runs, so a one-off override is never a surprise."""
    registered = app_registry.get(app_name)
    if registered is None:
        error(f"no app named {app_name!r} is registered")
        raise typer.Exit(1)

    if with_ is None:
        _print_registration(app_name, registered)
        return

    ctx = _make_serve_context(with_, placeholder_target=True)
    values = _coerce_raw_params(ctx.params)
    merged = merge_with_registered(
        ctx,
        registered,
        target_type=values["target_type"],
        name=values["name"],
        domain=values["domain"],
        auth=values["auth"],
        scope=values["scope"],
        anon=values["anon"],
        env=values["env"],
        guardrail=values["guardrail"],
        pwa=values["pwa"],
    )
    if merged["name"] is None:
        merged["name"] = app_name

    info(f"effective config for {app_name!r} with --with {with_!r}:")
    preview = AppRegistration(
        target=registered.target,
        target_kind=merged["target_kind"],
        name=merged["name"],
        domain=merged["domain"],
        auth=merged["auth"],
        scope=merged["scope"],
        anon=merged["anon"],
        env_secrets=merged["env_secrets"],
        guardrail=merged["guardrail"],
        pwa=merged["pwa"],
        registered_at=registered.registered_at,
    )
    _print_registration(app_name, preview)


@app_app.command("delete")
def delete(
    app_name: Annotated[str, typer.Argument(help="Registered app name to delete.")],
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip the confirmation prompt."),
    ] = False,
) -> None:
    """Remove a registered app *and* the source sidepage downloaded for it.

    The destructive sibling of `unregister`, which only forgets the saved
    config. `delete` also removes the app's managed directory — source,
    model weights, everything `sidepage pull` put there.

    **It will never delete a directory sidepage didn't create.** An app
    registered against a path the user already had (`app register`,
    `serve --autoregister`) has no managed source tree, so `delete`
    removes its registry entry and leaves every file alone. Deleting
    someone's own project because they typed `delete` instead of
    `unregister` is not a mistake this command is willing to make.
    """
    registered = app_registry.get(app_name)
    if registered is None:
        error(f"no app named {app_name!r} is registered")
        raise typer.Exit(1)

    running = registry.get(app_name)
    if running is not None and registry.is_alive(running.pid):
        error(
            f"{app_name!r} is currently running — `sidepage stop {app_name}` first, "
            "then delete it."
        )
        raise typer.Exit(1)

    managed = registered.source is not None and registered.source.managed
    source_dir = app_source_dir(app_name) if managed else None
    has_files = source_dir is not None and source_dir.exists()

    stdout.print(f"  registry entry  {app_name}")
    if has_files:
        size = sum(f.stat().st_size for f in source_dir.rglob("*") if f.is_file())
        stdout.print(f"  source tree     {source_dir} ({core_pull.format_bytes(size)})")
    else:
        stdout.print(
            "  source tree     [dim](none — sidepage didn't download this app, "
            "no files will be removed)[/dim]"
        )

    if not yes:
        if not output.is_interactive():
            error(
                f"deleting {app_name!r} removes files and can't be undone. There's no terminal "
                "here to confirm at — re-run with --yes if that's what you want."
            )
            raise typer.Exit(1)
        if not typer.confirm("  delete?", default=False):
            info("nothing was deleted")
            return

    removed: int | None = None
    if managed:
        try:
            removed = core_pull.remove_source_tree(app_name)
        except SourceError as exc:
            error(str(exc))
            raise typer.Exit(1) from exc

    try:
        app_registry.unregister(app_name)
    except AppNotRegisteredError as exc:  # pragma: no cover - checked above
        error(str(exc))
        raise typer.Exit(1) from exc

    if removed is not None:
        success(f"deleted {app_name!r} — {core_pull.format_bytes(removed)} of source removed")
    else:
        success(f"deleted {app_name!r} (registry entry only — no downloaded source)")


@app_app.command("unregister")
def unregister(
    app_name: Annotated[str, typer.Argument(help="Registered app name to remove.")],
) -> None:
    """Forget a registered app's saved config, leaving files untouched.

    For a `sidepage pull`ed app this leaves the downloaded source on disk
    under `apps/<name>` with nothing pointing at it — use `sidepage app
    delete` to remove both.
    """
    try:
        app_registry.unregister(app_name)
    except AppNotRegisteredError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc
    success(f"unregistered {app_name!r}")
