"""`sidepage pull <source>` — fetch a remote app, resolve how it would
run, register it, print the plan, and stop.

The command layer here is presentation and policy: `sidepage.core.pull`
and `sidepage.core.hf` do the resolving and fetching. What this module
owns is the *order things are shown in* and the guarantee that the last
line a user reads is what happens next, not what already happened.

**Nothing is executed by this command.** Not the entrypoint, not a build
step, not a dependency install. The plan it prints is a description of
what `serve` would do, and `serve` will ask for confirmation before doing
it — see `sidepage.commands.serve._require_source_trust`.

`--dry-run` stops before downloading and before registering, which is the
mode that answers "how big is this and what does it want?" without paying
for the answer. `--json` emits the same plan as one object for agents;
its `source.*` fields carry strings written by whoever owns the remote
repository, and are namespaced under `source` precisely so an agent
reading them can't mistake a stranger's text for sidepage's own.
"""

from __future__ import annotations

import json as json_lib
from dataclasses import replace
from typing import Annotated

import typer

from sidepage.config.settings import app_source_dir
from sidepage.core import app_registry, hf
from sidepage.core import pull as core_pull
from sidepage.core.app_registry import AppSource
from sidepage.core.auth import AuthTier
from sidepage.core.directory_client import Scope
from sidepage.core.exceptions import (
    AppRegistrationError,
    SourceError,
    SourceNotSupportedError,
    UnrunnableSourceError,
)
from sidepage.core.pull import RunPlan
from sidepage.core.target import TargetKind
from sidepage.output import error, info, plain, warn


def _note_hardware_override(
    plan: RunPlan, resolved: core_pull.ResolvedSource, ignore_hardware: bool
) -> RunPlan:
    """Record an overridden hardware gate on the plan itself.

    Applied wherever a plan is built rather than once up front, because
    `pull` re-plans against the downloaded directory and a note attached
    only to the first plan would be silently dropped. An overridden pull
    must never read as routine — the whole point of the flag is that the
    user accepted a caveat, and the output has to keep saying so.
    """
    if not ignore_hardware or not resolved.space.hardware:
        return plan
    return replace(
        plan,
        warnings=(
            *plan.warnings,
            f"--ignore-hardware: provisioned for {resolved.space.hardware} on Hugging Face; "
            "it will run on CPU here (MPS on Apple Silicon) and may be slow or run out of memory.",
        ),
    )


def _emit_json(payload: dict) -> None:
    """One line of plain JSON on stdout.

    Deliberately not Rich's `print_json`: this output exists to be parsed,
    and Rich highlights, indents, and re-wraps to the terminal width — all
    of which are improvements for a human and hazards for a parser.
    """
    typer.echo(json_lib.dumps(payload))


def _plan_json(
    *, app_name: str, resolved: core_pull.ResolvedSource, plan: RunPlan, dry_run: bool
) -> dict:
    space = resolved.space
    return {
        "app": app_name,
        "dry_run": dry_run,
        "registered": not dry_run,
        "executed": False,
        # Everything under `source` originates with the remote repository's
        # owner, not with sidepage. Namespaced so an agent consuming this
        # can tell the two apart.
        "source": {
            "kind": resolved.kind,
            "url": resolved.url,
            "commit": space.sha,
            "sdk": plan.sdk,
            "sdk_version": plan.sdk_version,
            "python_version": plan.python_version,
            "app_file": space.app_file,
            "hardware": space.hardware,
            "upstream_stage": space.stage,
        },
        "plan": {
            "launcher": plan.launcher,
            "target_kind": plan.target_kind.value,
            "entrypoint": str(plan.entrypoint) if plan.entrypoint else None,
            "dependency_file": plan.dependency_file,
            "dependency_count": plan.dependency_count,
            "dependencies_installed": False,
            "download_bytes": plan.download_bytes,
            "env_requested": list(plan.env_names),
            "env_granted": [],
        },
        "warnings": list(plan.warnings),
        "next": f"sidepage serve {app_name}",
    }


def _print_plan(
    *, app_name: str, resolved: core_pull.ResolvedSource, plan: RunPlan, dry_run: bool
) -> None:
    space = resolved.space
    verb = "would pull" if dry_run else "pulled"
    plain.print()
    plain.print(f"  [bold]{verb}[/bold]  {app_name}")
    plain.print(f"  source  {resolved.url}")
    plain.print(f"  commit  {space.sha[:7]}")

    sdk_line = plan.sdk or "unknown"
    if plan.sdk_version:
        sdk_line += f" {plan.sdk_version}"
    if plan.python_version:
        sdk_line += f" · python {plan.python_version}"
    plain.print(f"  sdk     {sdk_line}")

    if plan.entrypoint is not None:
        plain.print(f"  entry   {plan.entrypoint}")
    else:
        plain.print("  entry   [dim](static site — served from the repository root)[/dim]")

    if plan.dependency_file:
        count = (
            f"{plan.dependency_count} packages, "
            if plan.dependency_count is not None
            else ""
        )
        plain.print(f"  deps    {plan.dependency_file} ({count}not yet installed)")
    else:
        plain.print("  deps    [dim](none declared)[/dim]")

    plain.print(f"  size    {core_pull.format_bytes(plan.download_bytes)}")

    if plan.env_names:
        for name in plan.env_names:
            plain.print(f"  env     {name}  [dim](read by the app — not set)[/dim]")
    plain.print()

    for warning in plan.warnings:
        warn(warning)
    if plan.warnings:
        plain.print()

    if dry_run:
        plain.print("  [dim]nothing was downloaded, nothing was registered.[/dim]")
        plain.print(f"    sidepage pull {resolved.url}")
        return

    plain.print("  [dim]nothing has been executed. review the code, then:[/dim]")
    plain.print(f"    sidepage serve {app_name}")
    if plan.env_names:
        # Deliberately *not* `serve <app> --env NAME`. The scan finds every
        # variable the app reads, and most are plain configuration; `--env`
        # takes a vault secret *name* and fails loud if that name isn't in
        # the vault. Suggesting it blindly produced a copy-pasteable command
        # that always failed with "no secret named 'X' in the vault", which
        # is a confusing way to learn the difference between config and a
        # credential.
        first = plan.env_names[0]
        plain.print()
        plain.print(f"  [dim]it reads {', '.join(plan.env_names)} — to set them:[/dim]")
        plain.print(f"    {first}=<value> sidepage serve {app_name}        [dim]# config[/dim]")
        plain.print(
            f"    sidepage secrets set {first} && sidepage serve {app_name} --env {first}"
            "   [dim]# credential[/dim]"
        )
    plain.print(f"  [dim]source is at {app_source_dir(app_name)}[/dim]")


def pull(
    source: Annotated[
        str,
        typer.Argument(
            help="Remote source to fetch, e.g. huggingface.co/spaces/<owner>/<name> "
            "or hf:<owner>/<name>."
        ),
    ],
    as_name: Annotated[
        str | None,
        typer.Option("--as", help="Register under this name instead of the source's own."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Replace an existing app of the same name."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Resolve and print the plan — including download size and declared "
            "dependencies — without downloading or registering anything.",
        ),
    ] = False,
    ignore_hardware: Annotated[
        bool,
        typer.Option(
            "--ignore-hardware",
            help="Pull a Space provisioned for GPU hardware anyway. It will run on CPU (or MPS "
            "on Apple Silicon) and may be slow or exhaust memory.",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the plan as one JSON object."),
    ] = False,
) -> None:
    """Fetch a remote app and register it, without running anything.

    Downloads the source into sidepage's own managed directory, resolves
    the launcher/entrypoint/dependencies, and prints the plan. Dependencies
    are installed lazily on the first `serve`, not here; nothing from the
    remote source is executed by this command.
    """
    try:
        resolved = core_pull.resolve_source(source)
        hf.check_runnable(resolved.space, ignore_hardware=ignore_hardware)

        app_name = as_name or resolved.default_name
        if not core_pull.valid_app_name(app_name):
            error(
                f"{app_name!r} isn't a usable app name (letters, digits, dot, dash, underscore; "
                "it becomes a directory name and a registry key). Pass --as to choose one."
            )
            raise typer.Exit(1)

        existing = app_registry.get(app_name)
        if existing is not None and not force:
            error(
                f"an app named {app_name!r} is already registered — `--force` to replace it, "
                f"`--as <name>` to pull under a different name, or "
                f"`sidepage app delete {app_name}` to remove it first."
            )
            raise typer.Exit(1)

        # Planned from metadata alone first: a hostile `app_file` or an
        # unsupported layout is refused here, before a byte is fetched.
        plan = _note_hardware_override(
            core_pull.plan_from_space(resolved.space), resolved, ignore_hardware
        )

        if dry_run:
            if json_output:
                _emit_json(
                    _plan_json(app_name=app_name, resolved=resolved, plan=plan, dry_run=True)
                )
            else:
                _print_plan(app_name=app_name, resolved=resolved, plan=plan, dry_run=True)
            return

        if not json_output:
            info(
                f"downloading {len(resolved.space.files)} files "
                f"({core_pull.format_bytes(plan.download_bytes)}) from {resolved.url}"
            )

        core_pull.fetch_into(resolved, app_name)

        # Re-planned against the real directory: this is the pass that sees
        # symlinks, counts the dependency file, and scans the entrypoint
        # for environment-variable names.
        plan = _note_hardware_override(
            core_pull.plan_from_space(resolved.space, app_dir=app_source_dir(app_name)),
            resolved,
            ignore_hardware,
        )

        target = app_source_dir(app_name)
        if plan.target_kind is TargetKind.CODE and plan.entrypoint is not None:
            target = target / plan.entrypoint

        app_registry.register(
            app_name,
            target=target,
            target_kind=plan.target_kind,
            name=app_name,
            domain=None,
            auth=AuthTier.OPEN,
            scope=Scope.LOCAL,
            anon=False,
            env_secrets=(),
            guardrail=None,
            pwa=None,
            source=AppSource(
                kind=resolved.kind,
                url=resolved.url,
                commit=resolved.space.sha,
                managed=True,
                env_requested=plan.env_names,
                trusted_commit=None,
                hf=hf.to_config(resolved.space),
            ),
            replace_existing=force,
        )

        if json_output:
            _emit_json(_plan_json(app_name=app_name, resolved=resolved, plan=plan, dry_run=False))
        else:
            _print_plan(app_name=app_name, resolved=resolved, plan=plan, dry_run=False)

    except SourceNotSupportedError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc
    except UnrunnableSourceError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc
    except (SourceError, AppRegistrationError) as exc:
        error(str(exc))
        raise typer.Exit(1) from exc
