"""Serve targets and port-injection contract — backs `sidepage serve` and
its `--type` escape hatch (spec v3 §1, §2).

v3 narrows "what can be served" to exactly three kinds, replacing v1's
app-type list (streamlit/api/mcp) with a wrapping model that doesn't care
what's inside the process:

  - **code**     — any HTTP-serving process. Real support here covers
                   Streamlit specifically (detected by scanning the source
                   for a `streamlit` import) plus a generic Python fallback
                   that assumes the app reads `$PORT` — building a launcher
                   for every possible framework is out of scope; this
                   covers the prioritized Streamlit case and degrades
                   honestly for anything else.
  - **static**    — a directory, `index.html` as entry. See
                    `sidepage.core.static`.
  - **notebook**  — a `.ipynb`. Not implemented this round (not one of the
                    two prioritized targets); `detect_target_kind` still
                    recognizes it so `--type` reporting stays honest, but
                    `sidepage.core.notebook` remains a placeholder.

`serve` infers the target kind from the path and does not require `--type`
for these three — `--type` stays as an explicit override for when
inference is ambiguous or wrong.

### Port injection (§2)

No manual port handling by the caller. Sidepage allocates a real OS-assigned
port (`bind(0)`) and injects it into the wrapped process:
  - Streamlit: `--server.port <port> --server.headless true --server.address
    127.0.0.1` launcher flags.
  - Generic Python: `$PORT` env var, on the assumption the app reads
    `os.environ.get("PORT", ...)`.
"""

from __future__ import annotations

import socket
from enum import StrEnum
from pathlib import Path

from sidepage.core.exceptions import TargetDetectionError


class TargetKind(StrEnum):
    CODE = "code"
    STATIC = "static"
    NOTEBOOK = "notebook"


class CodeLauncher(StrEnum):
    """Which launcher pattern a `code` target uses — determines how the
    port is injected."""

    STREAMLIT = "streamlit"
    GENERIC_PYTHON = "generic_python"  # assumes $PORT


def detect_target_kind(target: Path, *, override: TargetKind | None = None) -> TargetKind:
    """Infer whether `target` is a `code` entrypoint, a `static` directory,
    or a `notebook` (`.ipynb`).

    Raises `sidepage.core.exceptions.TargetDetectionError` if `target`
    doesn't exist, or if `override` conflicts with what's on disk (e.g.
    `--type static` pointed at a `.py` file).
    """
    if not target.exists():
        raise TargetDetectionError(f"{target} does not exist")

    if override is not None:
        target_kind = override
    elif target.is_dir():
        target_kind = TargetKind.STATIC
    elif target.suffix == ".ipynb":
        target_kind = TargetKind.NOTEBOOK
    else:
        target_kind = TargetKind.CODE

    if target_kind is TargetKind.STATIC and not target.is_dir():
        raise TargetDetectionError(f"--type static requires a directory, got {target}")
    if target_kind in (TargetKind.CODE, TargetKind.NOTEBOOK) and target.is_dir():
        raise TargetDetectionError(f"--type {target_kind} requires a file, got directory {target}")
    return target_kind


def detect_code_launcher(target: Path) -> CodeLauncher:
    """Scan `target`'s source for a recognizable framework import.
    Streamlit apps get real port-flag injection; everything else falls
    back to the generic `$PORT` convention.
    """
    try:
        source = target.read_text(errors="ignore")
    except OSError:
        return CodeLauncher.GENERIC_PYTHON
    if "import streamlit" in source or "from streamlit" in source:
        return CodeLauncher.STREAMLIT
    return CodeLauncher.GENERIC_PYTHON


def allocate_port() -> int:
    """Bind to an OS-assigned free port and return its number. The socket
    is closed immediately after — a small, accepted race (something else
    could grab the port before the wrapped process binds it), same
    trade-off `bind(0)`-then-release always carries."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
