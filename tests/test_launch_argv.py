"""Fast, no-server unit tests for `sidepage.core.process._build_code_launch_argv`
— the pure argv-building logic behind CODE-target launchers.

Only the `STREAMLIT` branch is covered here: it's the one that changed to
stop wildcarding `--server.enableCORS`/`Origin` and allowlist the app's
real `public_origin` instead, whenever that's known at launch time (see
`sidepage.core.process.serve`'s `public_origin` computation and
`_build_code_launch_argv`'s own docstring for the full reasoning —
verified live against a real Streamlit server, not just asserted here,
that `--server.corsAllowedOrigins` does real per-origin enforcement).
FASTAPI/MCP/GENERIC_PYTHON are untouched by that change (FastAPI's own
CORS is left entirely to the user's app; the MCP host wrapper's wildcard
is deliberately unchanged — see that decision recorded in
`sidepage.core.process`'s own module docstring / commit history — its
legitimate callers are arbitrary third-party MCP-host origins, not the
app's own, so narrowing to `public_origin` would break the case CORS was
added for in the first place).
"""

from __future__ import annotations

from pathlib import Path

from sidepage.core.process import _build_code_launch_argv
from sidepage.core.target import CodeLauncher

FIXTURES = Path(__file__).parent / "fixtures"
_STREAMLIT_TARGET = FIXTURES / "streamlit-app" / "app.py"


def test_streamlit_allowlists_known_public_origin() -> None:
    argv = _build_code_launch_argv(
        _STREAMLIT_TARGET,
        CodeLauncher.STREAMLIT,
        12345,
        "my-app",
        "https://my-app-ab12.example.com",
    )
    assert "--server.enableCORS" in argv
    assert argv[argv.index("--server.enableCORS") + 1] == "true"
    assert "--server.corsAllowedOrigins" in argv
    assert argv[argv.index("--server.corsAllowedOrigins") + 1] == "https://my-app-ab12.example.com"


def test_streamlit_falls_back_to_wide_open_when_origin_unknown() -> None:
    """The `--anon` shape: `public_origin=None` because the
    `*.trycloudflare.com` hostname isn't assigned yet at launch time."""
    argv = _build_code_launch_argv(
        _STREAMLIT_TARGET, CodeLauncher.STREAMLIT, 12345, "my-app", None
    )
    assert "--server.enableCORS" in argv
    assert argv[argv.index("--server.enableCORS") + 1] == "false"
    assert "--server.corsAllowedOrigins" not in argv


def test_streamlit_allowlists_plain_local_origin_too() -> None:
    """A plain local serve (no `--domain`/`--anon`) still gets a known,
    narrow origin — `sidepage.core.process.serve` computes it as the
    proxy's own `http://127.0.0.1:<listen_port>`, the only address this
    session is ever actually reachable at."""
    argv = _build_code_launch_argv(
        _STREAMLIT_TARGET, CodeLauncher.STREAMLIT, 12345, "my-app", "http://127.0.0.1:54321"
    )
    assert argv[argv.index("--server.corsAllowedOrigins") + 1] == "http://127.0.0.1:54321"
