"""Static site serving — backs the `static` target kind (spec v3 §11), used
by both `sidepage new --type static` scaffolds and any directory passed
straight to `sidepage serve`.

Implementation (once built): Starlette `StaticFiles(directory=...,
html=True)` — the same stack `sidepage.core.reverse_proxy` uses, so there's
one HTTP library in play rather than two.

Missing `index.html` at the directory root is a **hard error**, not a
directory listing — `serve` should refuse to start rather than exposing
directory contents by default.
"""

from __future__ import annotations

from pathlib import Path


def validate_static_root(directory: Path) -> None:
    """Confirm `directory` has an `index.html` at its root.

    Once implemented, raises `sidepage.core.exceptions.StaticServeError`
    when it's missing — `serve` should fail before ever binding a port,
    not fall back to a directory listing.

    Not implemented.
    """
    raise NotImplementedError
