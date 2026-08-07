"""Unit tests for `sidepage.core.target`'s code-launcher detection —
purely source-scanning, no subprocess/network I/O, so these run instantly
unlike `test_serve_fastapi.py`/`test_serve_mcp.py`'s real end-to-end
checks.

MCP detection is the newest and most detail-sensitive part of this module
(two recognized packages, two different app-builder method names, and the
official SDK renamed its own class between versions — see
`sidepage.core.target`'s docstring) — these tests exist to pin that
behavior down independent of whether `mcp`/`fastmcp` are even installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidepage.core.target import (
    CodeLauncher,
    McpPackage,
    detect_asgi_app_variable,
    detect_code_launcher,
    detect_mcp_app_variable,
    detect_mcp_package,
)


def _write(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source)
    return path


# --- detect_code_launcher: precedence and fallback ---


def test_detects_streamlit(tmp_path: Path) -> None:
    target = _write(tmp_path, "app.py", "import streamlit as st\nst.write('hi')\n")
    assert detect_code_launcher(target) is CodeLauncher.STREAMLIT


def test_detects_mxlit(tmp_path: Path) -> None:
    target = _write(tmp_path, "app.py", "import mxlit as mt\nmt.title('hi')\n")
    assert detect_code_launcher(target) is CodeLauncher.MXLIT


def test_detects_fastapi(tmp_path: Path) -> None:
    target = _write(tmp_path, "app.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    assert detect_code_launcher(target) is CodeLauncher.FASTAPI


@pytest.mark.parametrize(
    "source",
    [
        "from mcp.server import MCPServer\nmcp = MCPServer('demo')\n",
        "from mcp.server.mcpserver import MCPServer\nmcp = MCPServer('demo')\n",
        "from mcp.server.fastmcp import FastMCP\nmcp = FastMCP('demo')\n",
        "from fastmcp import FastMCP\nmcp = FastMCP('demo')\n",
        "import fastmcp\nmcp = fastmcp.FastMCP('demo')\n",
    ],
)
def test_detects_mcp_for_every_recognized_import_style(tmp_path: Path, source: str) -> None:
    target = _write(tmp_path, "app.py", source)
    assert detect_code_launcher(target) is CodeLauncher.MCP


def test_fastapi_takes_precedence_over_mcp_when_mcp_is_mounted_inside_it(tmp_path: Path) -> None:
    """A script that mounts an MCP server inside a FastAPI app is still
    detected as FASTAPI — the top-level ASGI app to actually launch is the
    FastAPI one, and `uvicorn <module>:app` already serves the MCP
    sub-mount too. See detect_code_launcher's docstring."""
    source = (
        "from fastapi import FastAPI\n"
        "from mcp.server import MCPServer\n"
        "mcp = MCPServer('demo')\n"
        "app = FastAPI()\n"
        "app.mount('/mcp', mcp.streamable_http_app())\n"
    )
    target = _write(tmp_path, "app.py", source)
    assert detect_code_launcher(target) is CodeLauncher.FASTAPI


def test_generic_python_fallback_for_unrecognized_source(tmp_path: Path) -> None:
    target = _write(tmp_path, "app.py", "import os\nprint(os.environ.get('PORT'))\n")
    assert detect_code_launcher(target) is CodeLauncher.GENERIC_PYTHON


def test_generic_python_fallback_for_unreadable_target(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.py"
    assert detect_code_launcher(missing) is CodeLauncher.GENERIC_PYTHON


# --- detect_asgi_app_variable (FastAPI) ---


def test_detect_asgi_app_variable_custom_name(tmp_path: Path) -> None:
    target = _write(tmp_path, "app.py", "from fastapi import FastAPI\nmy_api = FastAPI()\n")
    assert detect_asgi_app_variable(target) == "my_api"


def test_detect_asgi_app_variable_defaults_to_app(tmp_path: Path) -> None:
    target = _write(tmp_path, "app.py", "from fastapi import FastAPI\n")  # no assignment
    assert detect_asgi_app_variable(target) == "app"


# --- detect_mcp_package ---


def test_detect_mcp_package_official_sdk(tmp_path: Path) -> None:
    target = _write(tmp_path, "app.py", "from mcp.server import MCPServer\nmcp = MCPServer('x')\n")
    assert detect_mcp_package(target) is McpPackage.OFFICIAL


def test_detect_mcp_package_official_sdk_older_fastmcp_class(tmp_path: Path) -> None:
    """The official SDK's high-level class used to be called FastMCP
    before a later major version renamed it to MCPServer — both live
    under mcp.server*, so both resolve to the OFFICIAL package (and thus
    the same .streamable_http_app() method), not the third-party
    `fastmcp` package."""
    target = _write(
        tmp_path, "app.py", "from mcp.server.fastmcp import FastMCP\nmcp = FastMCP('x')\n"
    )
    assert detect_mcp_package(target) is McpPackage.OFFICIAL


def test_detect_mcp_package_third_party_fastmcp(tmp_path: Path) -> None:
    target = _write(tmp_path, "app.py", "from fastmcp import FastMCP\nmcp = FastMCP('x')\n")
    assert detect_mcp_package(target) is McpPackage.FASTMCP


def test_detect_mcp_package_defaults_to_official_for_unreadable_target(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.py"
    assert detect_mcp_package(missing) is McpPackage.OFFICIAL


# --- detect_mcp_app_variable ---


@pytest.mark.parametrize("class_name", ["FastMCP", "MCPServer"])
def test_detect_mcp_app_variable_custom_name(tmp_path: Path, class_name: str) -> None:
    target = _write(tmp_path, "app.py", f"server = {class_name}('demo')\n")
    assert detect_mcp_app_variable(target) == "server"


def test_detect_mcp_app_variable_defaults_to_mcp(tmp_path: Path) -> None:
    target = _write(tmp_path, "app.py", "from mcp.server import MCPServer\n")  # no assignment
    assert detect_mcp_app_variable(target) == "mcp"
