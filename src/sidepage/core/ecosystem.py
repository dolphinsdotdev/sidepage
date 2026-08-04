"""Ecosystem integration — backs how `sidepage serve` builds the launch
command for a `code` or `notebook` target (spec v3 §14). Consumed by
`sidepage.core.process.serve` and `sidepage.core.notebook`, not exposed as
its own CLI surface.

- **Python — uv is now the default dependency runner**, not merely
  preferred (v1 was deliberately agnostic). Given scaffolding was minimized
  in §1 and the import-based two-path model was dropped in favor of
  pm2-style wrapping (§2), there's less surface area left to stay agnostic
  over. Trade-off, accepted: excludes poetry/pip-only users unless they
  also maintain a `uv.lock`.

  Real implementation here goes one step further than "assume uv": if the
  target already sits next to a working `.venv` (the common case for an
  existing project — e.g. one built with `pip install -r requirements.txt`,
  not necessarily uv-managed), that venv's own interpreter is used
  directly rather than layering `uv run --with` on top of it. Absent a
  venv, a sibling `requirements.txt` is preferred over a single
  `--with <package>` guess — real apps often need more than one dependency
  (e.g. a Streamlit app that also imports pandas/numpy), and a single
  `--with` can't express that.
- **JavaScript** — detect-and-defer across `package-lock.json` /
  `yarn.lock` / `pnpm-lock.yaml`; no canonical manager assumed the way
  Python now assumes uv. Not implemented — no JS target has been
  prioritized yet.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path


class JsPackageManager(StrEnum):
    NPM = "npm"
    YARN = "yarn"
    PNPM = "pnpm"


def resolve_python_runner(project_dir: Path, *, extra_package: str | None = None) -> list[str]:
    """Build the command prefix used to run Python for a `code`/`notebook`
    target rooted at `project_dir`.

    Prefers, in order: `project_dir/.venv/bin/python` (an existing
    project's own environment); `uv run --with-requirements
    requirements.txt` (a sibling requirements file, likely more complete
    than a single guessed package); `uv run --with <extra_package>` (a
    bare script with no project files at all).
    """
    venv_python = project_dir / ".venv" / "bin" / "python"
    if venv_python.is_file():
        return [str(venv_python)]
    requirements = project_dir / "requirements.txt"
    if requirements.is_file():
        return ["uv", "run", "--with-requirements", str(requirements)]
    cmd = ["uv", "run"]
    if extra_package:
        cmd += ["--with", extra_package]
    return cmd


def detect_js_package_manager(project_dir: Path) -> JsPackageManager | None:
    """Infer the JS package manager for `project_dir` from its lockfile
    (`package-lock.json` / `yarn.lock` / `pnpm-lock.yaml`). Returns `None`
    when no lockfile is present — callers should not assume a default the
    way `resolve_python_runner` assumes uv.

    Not implemented.
    """
    raise NotImplementedError
