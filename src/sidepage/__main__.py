"""`python -m sidepage` — equivalent to the `sidepage` console script.

Exists so `sidepage.core.process` can re-exec itself deterministically
when detaching (`serve`/`proxy --detach`). Spawning `sys.argv[0]` is not
reliable: for a console-script install it's a generated shim whose path
varies by platform and installer, and under `python -m` it isn't an
executable at all. `[sys.executable, "-m", "sidepage", ...]` always
resolves to the same interpreter and the same package the parent is
running from, which is exactly what a detached child has to inherit.
"""

from __future__ import annotations

from sidepage.cli import app

if __name__ == "__main__":
    app()
