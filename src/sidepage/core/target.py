"""Serve targets and port-injection contract — backs `sidepage serve` and
its `--type` escape hatch (spec v3 §1, §2).

v3 narrows "what can be served" to exactly three kinds, replacing v1's
app-type list (streamlit/api/mcp) with a wrapping model that doesn't care
what's inside the process:

  - **code**     — any HTTP-serving process (Flask, FastAPI, MCP over HTTP,
                   a bare `streamlit run`, ...). No import, no code
                   cooperation required — see `sidepage.core.process` for
                   the pm2-style external-wrapper design this replaced.
  - **static**    — a directory, `index.html` as entry. See
                    `sidepage.core.static` for the serving contract.
  - **notebook**  — a `.ipynb`, full Jupyter Lab exposed. See
                    `sidepage.core.notebook`.

`serve` infers the target kind from what's passed and does not require
`--type` for these three — but the CLI keeps `--type` as an explicit
override (an escape hatch for when inference is ambiguous or wrong,
matching the same rationale v1 had for its own `--type` flag).

### Port injection (§2)

No manual port handling by the caller. Sidepage allocates a real OS-assigned
port (`bind(0)`) and injects it into the wrapped process one of two ways:

  - **`$PORT` env var** — works for any app already reading
    `os.environ.get("PORT", ...)`, the common convention.
  - **Known-launcher flag injection** — Sidepage recognizes launcher
    patterns and injects directly: `streamlit run app.py --server.port
    <port>`, `uvicorn app:app --port <port>`, `jupyter lab --port <port>
    --no-browser --ServerApp.token=''`.

If neither applies (hardcoded port, no env read, unrecognized launcher),
Sidepage says so explicitly rather than guessing — this module is the
natural home for that launcher-pattern table once it's implemented.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path


class TargetKind(StrEnum):
    CODE = "code"
    STATIC = "static"
    NOTEBOOK = "notebook"


class PortInjectionStrategy(StrEnum):
    """How the allocated port reaches the wrapped process."""

    ENV_VAR = "env_var"  # $PORT
    LAUNCHER_FLAG = "launcher_flag"  # recognized launcher pattern, e.g. --port <port>


def detect_target_kind(target: Path) -> TargetKind:
    """Infer whether `target` is a `code` entrypoint, a `static` directory,
    or a `notebook` (`.ipynb`) — e.g. a `.ipynb` suffix implies
    `TargetKind.NOTEBOOK`, a directory implies `TargetKind.STATIC`,
    otherwise `TargetKind.CODE`.

    Once implemented, raises `sidepage.core.exceptions.TargetDetectionError`
    when the type can't be inferred confidently and the caller didn't pass
    an explicit `--type`.

    Not implemented.
    """
    raise NotImplementedError


def resolve_port_injection(target: Path, target_kind: TargetKind) -> PortInjectionStrategy | None:
    """Determine how to hand the allocated port to `target`: recognize a
    known launcher pattern, fall back to checking whether the app reads
    `$PORT`, or return `None` when neither applies — the caller (`serve`)
    is expected to fail loudly rather than guess in that case.

    Not implemented.
    """
    raise NotImplementedError
