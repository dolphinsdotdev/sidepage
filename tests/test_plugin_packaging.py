"""Structural checks on the Claude Code plugin + marketplace manifests.

The plugin is how `sidepage-serve` actually reaches an agent — `/plugin
marketplace add kalpi-4/sidepage` then `/plugin install sidepage@sidepage`
— so a malformed manifest or a drifted version breaks installation for
everyone while every other test in the suite still passes. None of this
needs the `claude` CLI present; these are the invariants that can be
checked from the file tree alone.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN_DIR = REPO_ROOT / "plugin"
PLUGIN_MANIFEST = PLUGIN_DIR / ".claude-plugin" / "plugin.json"


def _marketplace() -> dict:
    return json.loads(MARKETPLACE.read_text())


def _plugin() -> dict:
    return json.loads(PLUGIN_MANIFEST.read_text())


def test_manifests_exist_and_parse() -> None:
    assert MARKETPLACE.is_file(), "marketplace.json must be at the repo root"
    assert PLUGIN_MANIFEST.is_file()
    _marketplace()
    _plugin()


def test_marketplace_source_points_at_the_plugin_dir() -> None:
    entries = _marketplace()["plugins"]
    assert len(entries) == 1, entries
    entry = entries[0]
    assert entry["name"] == _plugin()["name"]
    resolved = (REPO_ROOT / entry["source"]).resolve()
    assert resolved == PLUGIN_DIR.resolve(), entry["source"]


def test_skill_sits_on_the_default_scan_path() -> None:
    """`skills/` under the plugin root is scanned automatically, so the
    manifest deliberately declares no `skills` field. If the skill ever
    moves, that omission silently stops working."""
    assert (PLUGIN_DIR / "skills" / "sidepage-serve" / "SKILL.md").is_file()
    assert "skills" not in _plugin(), "skills/ is the default scan path; don't declare it"


def test_plugin_version_matches_the_package() -> None:
    """A marketplace advertising a version that `pip install sidepage`
    doesn't give you is worse than no version at all — the plugin's whole
    job is driving that CLI."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert _plugin()["version"] == pyproject["project"]["version"]


def test_plugin_ships_no_executables() -> None:
    """Two reasons, both load-bearing. A top-level `bin/` disqualifies the
    plugin from claude.ai organization distribution; and bundled scripts
    were what forced `${CLAUDE_PLUGIN_ROOT}` path juggling in SKILL.md,
    which broke the moment the skill was installed rather than run from a
    checkout. `--detach`/`--json` removed the need for both.
    """
    assert not (PLUGIN_DIR / "bin").exists(), "a top-level bin/ blocks org distribution"
    executables = [
        p
        for p in PLUGIN_DIR.rglob("*")
        if p.is_file() and (p.suffix in {".sh", ".bash", ".ps1"} or p.stat().st_mode & 0o111)
    ]
    assert not executables, executables


def test_skill_documents_the_non_blocking_path() -> None:
    """The single most important thing the skill has to convey: run
    `serve`/`proxy` bare in a dispatched task and it hangs forever."""
    skill = (PLUGIN_DIR / "skills" / "sidepage-serve" / "SKILL.md").read_text()
    assert "--detach" in skill
    assert "--json" in skill
    assert "start_site.sh" not in skill, "stale reference to the removed wrapper scripts"
    assert "stop_site.sh" not in skill
