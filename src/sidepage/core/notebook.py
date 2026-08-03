"""Notebook serving — backs the `notebook` target kind (spec v3 §12),
`sidepage serve notebook.ipynb --auth token`.

Full Jupyter Lab exposed — editable, live kernel, execution on the dev's
own machine. Confirmed in v3: Sidepage is tunnel-only, not a compute risk
surface, so this needs no elevated risk model beyond what running Lab
locally already implies.

Port is injected via the `jupyter lab --port <port> --no-browser
--ServerApp.token=''` launcher pattern (see `sidepage.core.target`).
Jupyter's own token auth is disabled *because* `sidepage.core.reverse_proxy`
is now the auth boundary instead.

**Flagged risk, not yet mitigated:** running that injected launcher command
outside Sidepage's proxy would leave the notebook completely open — Jupyter
auth is off and nothing else is gating it. Worth a startup safety check
(e.g. refuse to launch unless the proxy is confirmed in front of it) before
this ships; `verify_proxy_fronted` below is the placeholder for that check.

**Dependencies via uv:**
  - Notebook inside a project with `pyproject.toml`/`uv.lock` — `uv run
    --with jupyter jupyter lab`, same as any other code target
    (`sidepage.core.ecosystem`).
  - Bare standalone `.ipynb` with no project — default requires a sibling
    `pyproject.toml` (consistent with the rest of the system). `juv`
    (PEP 723-style inline notebook dependency metadata via uv) is a
    candidate for the fully-standalone case, but treated as an evaluation,
    not a commitment — don't build against it without revisiting this.
"""

from __future__ import annotations

from pathlib import Path


def build_jupyter_launch_command(notebook: Path, *, port: int) -> list[str]:
    """Construct the `jupyter lab --port <port> --no-browser
    --ServerApp.token=''` launch command for `notebook`, resolving
    dependencies per this module's docstring (project-based `uv run` vs.
    standalone-with-sibling-`pyproject.toml`).

    Not implemented.
    """
    raise NotImplementedError


def verify_proxy_fronted(port: int) -> None:
    """Startup safety check: confirm the local reverse proxy
    (`sidepage.core.reverse_proxy`) is actually in front of `port` before
    letting an unauthenticated Jupyter Lab instance keep running. Not yet
    designed beyond being named here — see this module's docstring.

    Not implemented.
    """
    raise NotImplementedError
