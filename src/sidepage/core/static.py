"""Static site serving — backs the `static` target kind (spec v3 §11), used
by both `sidepage new --type static` scaffolds and any directory passed
straight to `sidepage serve`.

Implementation: Starlette `StaticFiles(directory=..., html=True)` — the
same stack `sidepage.core.reverse_proxy` uses (it mounts this directly, see
`sidepage.core.reverse_proxy._build_static_app`), so there's one HTTP
library in play rather than two.

Missing `index.html` at the directory root is a **hard error**, not a
directory listing — `serve` refuses to start rather than exposing
directory contents by default.
"""

from __future__ import annotations

from pathlib import Path

from sidepage.core.exceptions import StaticServeError


def validate_static_root(directory: Path) -> None:
    """Confirm `directory` has an `index.html` at its root.

    Raises `sidepage.core.exceptions.StaticServeError` when it's missing
    or `directory` isn't a directory at all — `serve` fails before ever
    binding a port, not fall back to a directory listing.
    """
    if not directory.is_dir():
        raise StaticServeError(f"{directory} is not a directory")
    if not (directory / "index.html").is_file():
        raise StaticServeError(f"{directory} has no index.html at its root")
