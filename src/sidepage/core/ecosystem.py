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

  **Always goes through `uv run`, never reuses an existing `.venv`
  directly.** An earlier version of this module preferred
  `project_dir/.venv/bin/python` when present, on the theory that an
  existing venv is already correctly set up. In practice, real project
  folders routinely have a `.venv` that's missing something — most often a
  framework package (e.g. Streamlit) installed by hand at some point and
  never captured in `requirements.txt`. Trusting that venv silently
  produced `ModuleNotFoundError` at launch with no attempt to recover.
  `uv run` fixes this by construction: it always layers the *detected*
  launcher requirement(s) (`extra_packages`, e.g. `["streamlit"]` for a
  Streamlit target or `["fastapi", "uvicorn"]` for a FastAPI one — see
  `sidepage.core.target.detect_code_launcher`) on top of whatever
  `requirements.txt` declares, on every launch, so the dependencies
  Sidepage itself knows the target needs are never silently missing
  regardless of whether the project's own manifest is complete. uv's
  caching keeps repeat launches cheap after the first resolve.

  This does mean packages installed by hand into a project's `.venv` but
  *not* captured in `requirements.txt`/`pyproject.toml` and *not* the
  detected launcher package won't be picked up — there's no way to know
  about those without either trusting the venv blindly (the failure mode
  this fixes) or statically analyzing every import in the target, which
  isn't implemented. Detected launcher packages are covered; anything else
  undeclared is a gap in the target's own manifest, not something Sidepage
  can paper over.
- **JavaScript** — detect-and-defer across `package-lock.json` /
  `yarn.lock` / `pnpm-lock.yaml`; no canonical manager assumed the way
  Python now assumes uv. Not implemented — no JS target has been
  prioritized yet.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path


class JsPackageManager(StrEnum):
    NPM = "npm"
    YARN = "yarn"
    PNPM = "pnpm"


def resolve_python_runner(project_dir: Path, *, extra_packages: Sequence[str] = ()) -> list[str]:
    """Build the `uv run` command prefix used to run Python for a
    `code`/`notebook` target rooted at `project_dir`.

    Layers, combined rather than either/or:
      - `--with-requirements requirements.txt`, if `project_dir` has one.
      - `--with <package>` for each of `extra_packages` — the launcher's
        own detected requirement(s) (e.g. `["streamlit"]`, or
        `["fastapi", "uvicorn"]`). Applied on top of, not instead of,
        `requirements.txt` — see this module's docstring for why that
        matters: it's what keeps a stale/incomplete manifest from silently
        producing a missing-module crash at launch.

    Always returns a `["uv", "run", ...]` prefix — no existing `.venv` is
    ever reused directly, deliberately (see module docstring).
    """
    cmd = ["uv", "run"]
    requirements = project_dir / "requirements.txt"
    if requirements.is_file():
        cmd += ["--with-requirements", str(requirements)]
    for package in extra_packages:
        cmd += ["--with", package]
    return cmd


def detect_js_package_manager(project_dir: Path) -> JsPackageManager | None:
    """Infer the JS package manager for `project_dir` from its lockfile
    (`package-lock.json` / `yarn.lock` / `pnpm-lock.yaml`). Returns `None`
    when no lockfile is present — callers should not assume a default the
    way `resolve_python_runner` assumes uv.

    Not implemented.
    """
    raise NotImplementedError
