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
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

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


def _github_slug_from_remote() -> str | None:
    """`owner/repo` for `origin`, or None when there's nothing to compare
    against — no git, no checkout (an sdist install), no remote, or a
    remote that isn't GitHub. All of those are legitimate places to run
    the suite, so they skip rather than fail."""
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    # https://github.com/owner/repo(.git) and git@github.com:owner/repo(.git)
    match = re.search(r"github\.com[/:]([^/]+/[^/\s]+?)(?:\.git)?$", result.stdout.strip())
    return match.group(1) if match else None


def test_manifest_urls_match_the_git_remote() -> None:
    """Every published URL has to name the repo people can actually add.

    This exists because they once didn't: the slug was inferred from the
    git *user* name rather than the remote, so `/plugin marketplace add`
    pointed at a repo that wasn't this one — and nothing in the suite
    noticed, because the manifests were internally consistent and valid.
    Internal consistency is not the property that matters here.
    """
    slug = _github_slug_from_remote()
    if slug is None:
        pytest.skip("no GitHub origin remote to compare against")

    expected = f"https://github.com/{slug}"
    found: list[tuple[str, str]] = [
        ("marketplace.owner.url", _marketplace()["owner"].get("url", "")),
        ("plugin.author.url", _plugin()["author"].get("url", "")),
        ("plugin.homepage", _plugin().get("homepage", "")),
        ("plugin.repository", _plugin().get("repository", "")),
    ]
    wrong = [(field, url) for field, url in found if url and url != expected]
    assert not wrong, f"expected {expected}, got: {wrong}"


def test_readme_install_command_names_this_repo() -> None:
    """The line users copy verbatim. A wrong slug here sends them to
    someone else's repository, or to a 404."""
    slug = _github_slug_from_remote()
    if slug is None:
        pytest.skip("no GitHub origin remote to compare against")

    readme = (REPO_ROOT / "README.md").read_text()
    commands = re.findall(r"/plugin marketplace add ([^\s`]+)", readme)
    assert commands, "README no longer documents `/plugin marketplace add`"
    assert all(c == slug for c in commands), f"expected {slug}, found {commands}"


def test_skill_documents_the_non_blocking_path() -> None:
    """The single most important thing the skill has to convey: run
    `serve`/`proxy` bare in a dispatched task and it hangs forever."""
    skill = (PLUGIN_DIR / "skills" / "sidepage-serve" / "SKILL.md").read_text()
    assert "--detach" in skill
    assert "--json" in skill
    assert "start_site.sh" not in skill, "stale reference to the removed wrapper scripts"
    assert "stop_site.sh" not in skill
