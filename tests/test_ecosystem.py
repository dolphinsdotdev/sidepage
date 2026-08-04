"""Unit tests for `sidepage.core.ecosystem.resolve_python_runner`.

Fast and pure — no subprocess, no network — since the function only
inspects the filesystem and builds an argv list. The case that matters
most here is the regression these tests pin: a project with an existing
`.venv` that's missing the launcher's own detected dependency (e.g.
Streamlit installed by hand and never captured in `requirements.txt`)
must still get that dependency, not silently launch without it.
"""

from __future__ import annotations

from pathlib import Path

from sidepage.core.ecosystem import resolve_python_runner


def test_bare_directory_falls_back_to_extra_packages(tmp_path: Path) -> None:
    cmd = resolve_python_runner(tmp_path, extra_packages=["streamlit"])
    assert cmd == ["uv", "run", "--with", "streamlit"]


def test_bare_directory_no_extra_packages(tmp_path: Path) -> None:
    cmd = resolve_python_runner(tmp_path)
    assert cmd == ["uv", "run"]


def test_requirements_txt_used_via_uv_run(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("openai\n")
    cmd = resolve_python_runner(tmp_path)
    assert cmd == ["uv", "run", "--with-requirements", str(tmp_path / "requirements.txt")]


def test_requirements_txt_plus_extra_package_layers_both(tmp_path: Path) -> None:
    """The regression this pins: a requirements.txt that doesn't declare
    the detected launcher package (e.g. it only lists `openai`, not
    `streamlit`) must still get that package layered on top."""
    (tmp_path / "requirements.txt").write_text("openai\n")
    cmd = resolve_python_runner(tmp_path, extra_packages=["streamlit"])
    assert cmd == [
        "uv",
        "run",
        "--with-requirements",
        str(tmp_path / "requirements.txt"),
        "--with",
        "streamlit",
    ]


def test_multiple_extra_packages_all_layer_on(tmp_path: Path) -> None:
    """FastAPI needs both `fastapi` and `uvicorn` — a console-script
    launcher needs its server too, not just the framework itself."""
    cmd = resolve_python_runner(tmp_path, extra_packages=["fastapi", "uvicorn"])
    assert cmd == ["uv", "run", "--with", "fastapi", "--with", "uvicorn"]


def test_existing_venv_is_never_trusted_directly(tmp_path: Path) -> None:
    """The actual bug: an earlier version of this function preferred an
    existing `.venv/bin/python` over everything else, with no way to know
    whether that venv actually had the target's dependencies installed.
    Even with a `.venv` present, the result must still route through
    `uv run` with the detected package layered in — never a bare
    `[venv_python]` command."""
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/bin/sh\n")  # doesn't need to be a real interpreter
    (tmp_path / "requirements.txt").write_text("openai\n")

    cmd = resolve_python_runner(tmp_path, extra_packages=["streamlit"])

    assert cmd[0] == "uv"
    assert str(venv_python) not in cmd
    assert "--with" in cmd and "streamlit" in cmd
