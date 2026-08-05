"""Notebook serving — backs the `notebook` target kind (spec v3 §12),
`sidepage serve notebook.ipynb --auth token`.

Full Jupyter Lab exposed — editable, live kernel, execution on the dev's
own machine. Confirmed in v3: Sidepage is tunnel-only, not a compute risk
surface, so this needs no elevated risk model beyond what running Lab
locally already implies.

Port is injected the same way as every other code target
(`sidepage.core.ecosystem.resolve_python_runner`), then `jupyter lab
<notebook> --port <port> --no-browser --ip=127.0.0.1 ...`. Jupyter's own
token/password auth is disabled *because* `sidepage.core.reverse_proxy` is
now the auth boundary instead — same reasoning as every other launcher,
not new to notebooks.

**The one notebook-specific wrinkle, verified rather than assumed:**
Jupyter Server rejects cross-origin requests and WebSocket upgrades by
default (`check_origin`), comparing the `Origin` header against its own
`Host`. Through Sidepage's proxy, the browser's `Origin` is the *proxy's*
own address (`http://127.0.0.1:<listen_port>`), not Jupyter's real,
different upstream port — a mismatch that gets rejected out of the box.
Confirmed live (a real kernel start + a real `execute_request` over a
WebSocket carrying a deliberately-mismatched `Origin` header): the
rejection reproduces without `--ServerApp.allow_origin=*`
`--ServerApp.disable_check_xsrf=True`, and passing both fixes it — used
below rather than assumed. `--ip=127.0.0.1` (not Jupyter's own default,
which isn't guaranteed across versions) is the actual mitigation for the
"what if this launch command runs outside Sidepage's proxy" risk this
module used to flag as unmitigated — the wrapped process's bind address is
fully Sidepage-controlled by construction, same guarantee every other
code launcher already has, so there's no separate runtime check to add
beyond passing that flag ourselves.

**Dependencies via uv:** same pattern as every other code target — `uv
run --with jupyterlab jupyter lab`, with the target directory's own
`requirements.txt` (if any) layered underneath via
`sidepage.core.ecosystem.resolve_python_runner`. No standalone-vs-project
distinction needed: `resolve_python_runner` already degrades to a bare
`uv run --with jupyterlab` when there's no `requirements.txt` next to the
notebook, the same as it does for a Streamlit/FastAPI/MCP script with no
manifest of its own.
"""

from __future__ import annotations

from pathlib import Path

from sidepage.core import ecosystem


def build_jupyter_launch_command(notebook: Path, *, port: int) -> list[str]:
    """Construct the real `jupyter lab` launch command for `notebook`,
    binding to `port` on loopback only, with Jupyter's own auth disabled
    (the reverse proxy is the auth boundary) and its origin/XSRF checks
    relaxed enough to accept requests forwarded through that proxy — see
    this module's docstring for why both are needed, verified rather than
    assumed.

    The caller is expected to run this with `cwd=notebook.parent` (see
    `sidepage.core.process.serve`, consistent with every other code
    target) — `notebook`'s absolute path is passed through regardless, so
    Jupyter opens it directly rather than requiring a nested lookup.
    """
    runner = ecosystem.resolve_python_runner(notebook.parent, extra_packages=["jupyterlab"])
    return runner + [
        "jupyter",
        "lab",
        str(notebook),
        "--port",
        str(port),
        "--no-browser",
        "--ip=127.0.0.1",
        "--ServerApp.token=",
        "--ServerApp.password=",
        "--ServerApp.allow_origin=*",
        "--ServerApp.disable_check_xsrf=True",
    ]
