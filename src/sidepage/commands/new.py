"""`sidepage new` — spec v3 §1, targets (the only scaffolding kept).

v3 cuts this down sharply from v1: no more streamlit/api/mcp project
generation, no more `--deps` (the one remaining scaffold — a static site —
has no dependencies to pin). Sidepage wraps existing things rather than
generating them; `new --type static` is "about the only scaffolding kept."
This resolves v1's open question (how opinionated should `new` be) by
mostly opting out of the question.
"""

from __future__ import annotations

from typing import Annotated

import typer

from sidepage.core.scaffold import NewTargetType
from sidepage.output import not_implemented


def new(
    name: Annotated[str, typer.Argument(help="Name of the project to create.")],
    target_type: Annotated[
        NewTargetType,
        typer.Option("--type", help="Scaffold to generate."),
    ] = NewTargetType.STATIC,
) -> None:
    """Generate a minimal static-site skeleton (directory + starter index.html)."""
    not_implemented("sidepage new", implemented_by="sidepage.core.scaffold.scaffold_project")
