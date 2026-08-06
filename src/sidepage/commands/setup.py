"""`sidepage setup` — installs `cloudflared`, the one non-Python runtime
dependency tunnel functionality (`serve --anon` / `serve --domain`) needs.
Not in the v1/v3 spec — new for this package's `pip install sidepage`
experience, so it never has to vendor a Go binary into a Python wheel or
declare it as a Python dependency (`sidepage.core.cloudflared_installer`
has the full reasoning).

Safe to run repeatedly: idempotent by design, see
`cloudflared_installer.ensure_installed`.
"""

from __future__ import annotations

from typing import Annotated

import typer

from sidepage.core import cloudflared_installer
from sidepage.core.exceptions import CloudflaredInstallError
from sidepage.output import error, info, success, warn


def setup(
    force: Annotated[
        bool,
        typer.Option("--force", help="Reinstall even if cloudflared is already usable."),
    ] = False,
    system: Annotated[
        bool,
        typer.Option(
            "--system",
            help="Link into a system-wide bin dir (e.g. /usr/local/bin) instead of a "
            "user-local or venv one. May need elevated privileges.",
        ),
    ] = False,
) -> None:
    """Detect `cloudflared` on `PATH`; if it's missing, download the
    right release for this OS/architecture from Cloudflare, install it
    into a user-writable location, and verify it with `cloudflared
    --version`."""
    try:
        result = cloudflared_installer.ensure_installed(force=force, system=system)
    except CloudflaredInstallError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    if result.already_installed:
        info(f"cloudflared already present: {result.binary} ({result.version})")
    else:
        success(f"installed cloudflared {result.version} -> {result.binary}")

    if result.linked_dir is not None:
        if cloudflared_installer.is_dir_on_path(result.linked_dir):
            info(f"cloudflared linked into {result.linked_dir} (on PATH)")
        else:
            warn(
                f"cloudflared linked into {result.linked_dir}, which isn't on PATH yet. "
                f'Add it, e.g.: export PATH="{result.linked_dir}:$PATH"'
            )
