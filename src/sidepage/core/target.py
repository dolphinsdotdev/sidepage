"""Serve targets and port-injection contract — backs `sidepage serve` and
its `--type` escape hatch (spec v3 §1, §2).

v3 narrows "what can be served" to exactly three kinds, replacing v1's
app-type list (streamlit/api/mcp) with a wrapping model that doesn't care
what's inside the process:

  - **code**     — any HTTP-serving process. Real support here covers
                   Streamlit, FastAPI, Python MCP servers, and Gradio
                   specifically (detected by scanning the source for a
                   recognizable import) plus a generic Python fallback
                   that assumes the app reads `$PORT` — building a
                   launcher for every possible framework is out of scope;
                   this covers the frameworks actually asked for and
                   degrades honestly for anything else.
  - **static**    — a directory, `index.html` as entry. See
                    `sidepage.core.static`.
  - **notebook**  — a `.ipynb`, served as a full, editable Jupyter Lab
                    instance with a live kernel. Real — see
                    `sidepage.core.notebook` for the launch command and
                    why Jupyter's own origin/XSRF checks need relaxing to
                    work behind a reverse proxy at all.

`serve` infers the target kind from the path and does not require `--type`
for these three — `--type` stays as an explicit override for when
inference is ambiguous or wrong.

### Port injection (§2)

No manual port handling by the caller. Sidepage allocates a real OS-assigned
port (`bind(0)`) and injects it into the wrapped process:
  - Streamlit: `--server.port <port> --server.headless true --server.address
    127.0.0.1` launcher flags.
  - FastAPI: launched via `uvicorn <module>:<app> --host 127.0.0.1 --port
    <port>` instead of running the script directly — this bypasses
    whatever the script's own `if __name__ == "__main__":` block does
    (many real FastAPI apps hardcode a port there, e.g. `uvicorn.run(app,
    port=8000)`, which would silently ignore Sidepage's allocated port if
    the script were just executed as-is). `<module>` is the target's own
    filename stem (`app.py` → `app`), consistent with how the process is
    launched with `cwd` set to the target's directory; `<app>` is
    extracted by scanning for `<name> = FastAPI(...)`, defaulting to the
    near-universal convention `app` when no assignment is found. FastAPI
    serves its own OpenAPI docs at `/docs`/`/redoc`/`/openapi.json`
    automatically — no Sidepage-side work needed there beyond making sure
    the proxy passes those paths through like any other (it already does).
  - MCP (Python): `uvicorn <module>:<var>.<app-method> --factory --host
    127.0.0.1 --port <port>`, same bypass-the-entrypoint trick as FastAPI
    and for the same reason — most real MCP servers call `<var>.run()` in
    their own `if __name__ == "__main__":` block, which defaults to the
    stdio transport (no HTTP at all) unless the script author explicitly
    wired up `transport="streamable-http"` themselves. Calling
    `.streamable_http_app()` / `.http_app()` directly and serving *that*
    via `uvicorn --factory` sidesteps the script's own transport choice
    entirely, so **a script authored only for stdio still becomes a real,
    reverse-proxied HTTP MCP server** — the same way a FastAPI script that
    hardcodes `uvicorn.run(app, port=8000)` in `__main__` still lands on
    Sidepage's allocated port, because that block is never executed
    either. Two Python MCP packages are recognized (see
    `detect_mcp_package`): the official SDK (`mcp`, class `FastMCP` or
    `MCPServer` depending on version — both expose the same
    `.streamable_http_app()` method) and the popular third-party `fastmcp`
    package (class `FastMCP`, method `.http_app()`). Both default to
    mounting at `/mcp`, verified against the actually-resolvable current
    releases of each package rather than assumed from memory.
  - Gradio: `uvicorn <wrapper-module>:make_app --factory --host 127.0.0.1
    --port <port>`, the same generated-wrapper approach MCP uses (see
    `sidepage.core.process._write_launch_wrapper`) and for a stronger
    version of the same reason. The canonical Gradio script ends with a
    bare, *unguarded* `demo.launch()` at module level — not tucked inside
    `if __name__ == "__main__":` the way FastAPI/MCP scripts conventionally
    are — so merely importing the module to reach its ASGI app would block
    forever on Gradio's own blocking server. The generated wrapper
    neutralizes `Blocks.launch` before importing the target (capturing the
    Blocks object it was called on), then hands that object to Gradio's
    supported `gradio.mount_gradio_app()` entrypoint. That also disarms a
    hardcoded `launch(server_port=...)`/`share=True`/`ssr_mode=True`,
    none of which sidepage could otherwise override.

    Injecting `GRADIO_SERVER_PORT` instead was rejected on evidence, not
    taste: Gradio treats that env var as the *start* of a 100-port search
    (`GRADIO_NUM_PORTS`), so a busy port silently moves the app to a
    different one sidepage isn't proxying — and an explicit
    `launch(server_port=...)` in the script ignores the env var outright.
    Verified against gradio 6.26.0; see `docs/OPEN_QUESTIONS.md` for what
    hasn't been checked on older majors.
  - Generic Python: `$PORT` env var, on the assumption the app reads
    `os.environ.get("PORT", ...)`.
"""

from __future__ import annotations

import re
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
    FASTAPI = "fastapi"
    MCP = "mcp"
    GRADIO = "gradio"
    GENERIC_PYTHON = "generic_python"  # assumes $PORT


class McpPackage(StrEnum):
    """Which Python MCP package a detected `CodeLauncher.MCP` target uses
    — determines the `uv run --with` package name and which method builds
    the ASGI app (see `detect_mcp_package`, `MCP_APP_METHOD`)."""

    OFFICIAL = "mcp"  # modelcontextprotocol/python-sdk — FastMCP or MCPServer
    FASTMCP = "fastmcp"  # third-party jlowin/fastmcp — FastMCP


MCP_APP_METHOD: dict[McpPackage, str] = {
    McpPackage.OFFICIAL: "streamable_http_app",
    McpPackage.FASTMCP: "http_app",
}


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


def _has_mcp_import(source: str) -> bool:
    return (
        "from fastmcp import" in source
        or "import fastmcp" in source
        or "from mcp.server" in source
        or "import mcp.server" in source
    )


def detect_code_launcher(target: Path) -> CodeLauncher:
    """Scan `target`'s source for a recognizable framework import.
    Streamlit, FastAPI, MCP, and Gradio apps get real launcher-specific
    port injection; everything else falls back to the generic `$PORT`
    convention.

    Checked in this order — Streamlit, then FastAPI, then MCP, then
    Gradio — so a script that mounts an MCP server *inside* a FastAPI app
    (a common real pattern, e.g. `app.mount("/mcp",
    mcp_server.streamable_http_app())`) is still correctly detected as
    FASTAPI: the top-level ASGI app to actually launch is the FastAPI one,
    and `uvicorn <module>:app` already serves everything mounted on it,
    MCP sub-route included. Only a standalone MCP script with no FastAPI
    import falls through to MCP.

    Gradio sits after FastAPI for exactly that same reason: a script that
    already calls `gradio.mount_gradio_app(app, demo, ...)` onto its own
    FastAPI instance wants *that* app served, not a second, separate mount
    of the same Blocks (which is what the GRADIO launcher would build).
    Only a standalone Gradio script falls through to GRADIO.
    """
    try:
        source = target.read_text(errors="ignore")
    except OSError:
        return CodeLauncher.GENERIC_PYTHON
    if "import streamlit" in source or "from streamlit" in source:
        return CodeLauncher.STREAMLIT
    if "from fastapi import" in source or "import fastapi" in source:
        return CodeLauncher.FASTAPI
    if _has_mcp_import(source):
        return CodeLauncher.MCP
    if "import gradio" in source or "from gradio" in source:
        return CodeLauncher.GRADIO
    return CodeLauncher.GENERIC_PYTHON


_FASTAPI_APP_ASSIGNMENT_RE = re.compile(r"^(\w+)\s*=\s*FastAPI\s*\(", re.MULTILINE)
_MCP_APP_ASSIGNMENT_RE = re.compile(r"^(\w+)\s*=\s*(?:FastMCP|MCPServer)\s*\(", re.MULTILINE)


def detect_asgi_app_variable(target: Path) -> str:
    """Best-effort scan for `<name> = FastAPI(...)` to build the
    `<module>:<name>` import string `uvicorn` needs. Defaults to `app` —
    the near-universal convention in FastAPI's own docs and most real
    apps — when no assignment is found."""
    try:
        source = target.read_text(errors="ignore")
    except OSError:
        return "app"
    match = _FASTAPI_APP_ASSIGNMENT_RE.search(source)
    return match.group(1) if match else "app"


def detect_mcp_package(target: Path) -> McpPackage:
    """Which of the two recognized Python MCP packages `target` uses —
    determines both the `uv run --with` package name and which method
    builds the ASGI app (`MCP_APP_METHOD`). Only called after
    `detect_code_launcher` has already returned `CodeLauncher.MCP`, so a
    bare `import` scan (not requiring a specific class name) is enough:
    `fastmcp` is the third-party package (`from fastmcp import ...` /
    `import fastmcp`); anything importing from `mcp.server` is treated as
    the official SDK, whether the script uses the older `FastMCP` class or
    the current `MCPServer` — both expose `.streamable_http_app()`,
    verified against the actually-resolvable 1.x and 2.x releases rather
    than assumed. Defaults to the official SDK if the source can't be
    read at all (matches `detect_code_launcher`'s own fallback shape).
    """
    try:
        source = target.read_text(errors="ignore")
    except OSError:
        return McpPackage.OFFICIAL
    if "from fastmcp import" in source or "import fastmcp" in source:
        return McpPackage.FASTMCP
    return McpPackage.OFFICIAL


def detect_mcp_app_variable(target: Path) -> str:
    """Best-effort scan for `<name> = FastMCP(...)` / `<name> =
    MCPServer(...)` to build the `<module>:<name>.<app-method>` import
    string `uvicorn --factory` needs. Defaults to `mcp` — the convention
    used throughout both the official SDK's docs/examples and the
    third-party `fastmcp` package's — when no assignment is found."""
    try:
        source = target.read_text(errors="ignore")
    except OSError:
        return "mcp"
    match = _MCP_APP_ASSIGNMENT_RE.search(source)
    return match.group(1) if match else "mcp"


def allocate_port() -> int:
    """Bind to an OS-assigned free port and return its number. The socket
    is closed immediately after — a small, accepted race (something else
    could grab the port before the wrapped process binds it), same
    trade-off `bind(0)`-then-release always carries."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
