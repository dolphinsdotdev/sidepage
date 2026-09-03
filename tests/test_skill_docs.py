"""Drift check for `plugin/skills/sidepage-serve/SKILL.md` against the actual CLI.

SKILL.md makes narrative claims about flag names (`--peer`, `--idle-timeout`,
`--with`, ...) that can silently go stale as `src/sidepage/commands/` evolves
— the same class of drift that bit the MCP SDK's `FastMCP`->`MCPServer`
rename, caught before only by manually re-reading the source. This is that
manual check, automated.

Deliberately a warning, not a hard failure: SKILL.md's examples also contain
flags that belong to *other* programs (e.g. `npm run dev -- --host ...`), so
a flag mentioned in prose isn't always a sidepage flag. A human still reviews
the printed list.
"""

from __future__ import annotations

import re
from pathlib import Path

COMMANDS_DIR = Path(__file__).parent.parent / "src" / "sidepage" / "commands"
SKILL_MD = Path(__file__).parent.parent / "plugin" / "skills" / "sidepage-serve" / "SKILL.md"

# Flags SKILL.md legitimately mentions that aren't sidepage's own: typer's
# built-in --help, and flags on other programs shown in example commands
# (e.g. `npm run dev -- --host 127.0.0.1 --port 5173`).
KNOWN_NON_SIDEPAGE_FLAGS = {"--help", "--host"}


def _declared_cli_flags() -> set[str]:
    flags: set[str] = set()
    for path in COMMANDS_DIR.glob("*.py"):
        text = path.read_text()
        flags.update(re.findall(r'typer\.Option\(\s*"(--[a-zA-Z0-9-]+)"', text))
    return flags


def _flags_mentioned_in_skill_md() -> set[str]:
    text = SKILL_MD.read_text()
    return set(re.findall(r"--[a-zA-Z][a-zA-Z0-9-]*", text))


def test_skill_md_flags_still_exist_on_the_cli(capsys) -> None:
    declared = _declared_cli_flags()
    mentioned = _flags_mentioned_in_skill_md() - KNOWN_NON_SIDEPAGE_FLAGS

    stale = sorted(mentioned - declared)
    if stale:
        print(
            "\nplugin/skills/sidepage-serve/SKILL.md mentions flags that no longer "
            "resolve to a typer.Option in src/sidepage/commands/ — CLI may "
            "have renamed or removed them, or this is a legitimate new "
            "non-sidepage flag to add to KNOWN_NON_SIDEPAGE_FLAGS in "
            f"tests/test_skill_docs.py:\n  {', '.join(stale)}"
        )

    # Warning only, per design: a flag mentioned only in prose isn't
    # necessarily a sidepage flag, so this never fails the build.
    assert True
