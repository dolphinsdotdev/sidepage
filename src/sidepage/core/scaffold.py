"""Project scaffolding — backs `sidepage new` (spec v3 §1).

v3 cuts scope drastically from v1: no more streamlit/api/mcp project
generation. Sidepage wraps existing things (see `sidepage.core.target`)
rather than generating them — "about the only scaffolding kept" is a
static-site skeleton. This resolves v1's open question (how opinionated
should `new` be) by mostly opting out of the question: there's very little
left to be opinionated about.

`--deps <manager>` is gone along with it — the only remaining scaffold
(a directory + `index.html`) has no dependencies to pin, so a
dependency-manager choice has nothing left to apply to. If a future
scaffold type needs dependency pinning again, that's a small enum to bring
back, not a design commitment to carry now.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path


class NewTargetType(StrEnum):
    """Currently single-valued — kept as an enum (rather than assumed)
    because the CLI still exposes `--type` explicitly per spec, matching
    the literal `sidepage new <name> --type static` example.
    """

    STATIC = "static"


def scaffold_project(name: str, destination: Path) -> Path:
    """Generate a minimal static-site skeleton (a directory plus a starter
    `index.html`) at `destination / name`.

    Returns the path to the generated project root.

    Not implemented.
    """
    raise NotImplementedError
