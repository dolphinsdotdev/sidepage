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
- **JavaScript** — detect-and-defer across `package-lock.json` /
  `yarn.lock` / `pnpm-lock.yaml`; no canonical manager assumed the way
  Python now assumes uv.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path


class JsPackageManager(StrEnum):
    NPM = "npm"
    YARN = "yarn"
    PNPM = "pnpm"


def resolve_python_runner(project_dir: Path) -> list[str]:
    """Build the uv-based command prefix (e.g. `["uv", "run"]`) for a
    Python `code`/`notebook` target rooted at `project_dir`.

    Not implemented.
    """
    raise NotImplementedError


def detect_js_package_manager(project_dir: Path) -> JsPackageManager | None:
    """Infer the JS package manager for `project_dir` from its lockfile
    (`package-lock.json` / `yarn.lock` / `pnpm-lock.yaml`). Returns `None`
    when no lockfile is present — callers should not assume a default the
    way `resolve_python_runner` assumes uv.

    Not implemented.
    """
    raise NotImplementedError
