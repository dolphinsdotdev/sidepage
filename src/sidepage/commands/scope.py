"""`sidepage promote` — spec v3 §5, discovery & scope.

One directory, scope as a field — **confirmed** in v3, not split into
separate instances per scope tier (this was open in v1). `promote` widens
scope **without** issuing a new identity — same app, same directory entry,
wider visibility.
"""

from __future__ import annotations

from typing import Annotated

import typer

from sidepage.core.directory_client import Scope
from sidepage.output import not_implemented


def promote(
    app_name: Annotated[str, typer.Argument(help="App to widen the scope of.")],
    scope: Annotated[
        Scope, typer.Option("--scope", help="New (wider) scope to promote to.")
    ] = Scope.WEB,
) -> None:
    """Widen an app's directory visibility without issuing a new identity."""
    not_implemented("sidepage promote", implemented_by="sidepage.core.directory_client.promote")
