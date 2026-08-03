"""`sidepage ls` / `sidepage status` — directory queries.

Not a numbered section in the v3 spec (v1 had a "Directory queries" §10;
v3 goes straight from §9 local reverse proxy to §10 inspection with no
`ls`/`status` mention). Kept as-is since the directory model itself is
still central to v3 (§3, §5) — treated as not re-stated, not cut.

`ls` lists known apps, filterable by scope, with `--mine` limiting to the
current identity's own apps. `status` reports health/reachability plus
declared scope/auth tier — the reconciliation view between the directory's
asserted truth and actual reachability, and now also folds in tunnel
connectivity (v1 would have had a separate `tunnel status` for that; v3
drops that command group, see `sidepage.core.tunnel_manager`).
"""

from __future__ import annotations

from typing import Annotated

import typer

from sidepage.core.directory_client import Scope
from sidepage.output import not_implemented


def ls(
    scope: Annotated[
        Scope | None, typer.Option("--scope", help="Filter to one scope.")
    ] = None,
    mine: Annotated[
        bool, typer.Option("--mine", help="Limit to the current identity's own apps.")
    ] = False,
) -> None:
    """List known apps in the directory."""
    not_implemented("sidepage ls", implemented_by="sidepage.core.directory_client.list_entries")


def status(
    app_name: Annotated[str, typer.Argument(help="App to check.")],
) -> None:
    """Show health/reachability and declared scope/auth tier for an app."""
    not_implemented("sidepage status", implemented_by="sidepage.core.directory_client.get_status")
