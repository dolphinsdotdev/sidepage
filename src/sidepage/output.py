"""Shared console output helpers used across all `sidepage` subcommands.

This module is part of the CLI shell itself (argument parsing / presentation),
not the SDK ("the package") that will eventually implement each command's
behavior. It exists so every command prints in a consistent style instead of
each module rolling its own `print()` calls.
"""

from __future__ import annotations

import sys

from rich.console import Console

# Windows' default console/pipe encoding is the system codepage (cp1252 and
# friends), not UTF-8 — `success()`'s ✓ (and anything else non-ASCII) would
# otherwise raise UnicodeEncodeError the first time output actually reaches
# a real Windows stdout/stderr instead of a UTF-8 pytest/CI capture. `sys.std*`
# are real `TextIOWrapper`s in that case, so reconfiguring in place fixes it
# for every print through them, Rich or not.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except ValueError:
            pass

# Two consoles, mirroring the stdout/stderr split most CLIs need: `stdout`
# for data a user might pipe (`sidepage ls | jq ...`), `stderr` for status,
# warnings, and errors that shouldn't pollute piped output.
stdout = Console()
stderr = Console(stderr=True)


def not_implemented(command: str, *, implemented_by: str) -> None:
    """Standard message for commands whose wiring exists but whose behavior
    does not yet — every command in this scaffold calls this until the
    corresponding `sidepage.core` module is built out.

    Args:
        command: the user-facing command string, e.g. "sidepage serve".
        implemented_by: dotted path of the future `core` module/class that
            will own the real behavior, e.g. "sidepage.core.process.AppRunner".
    """
    stderr.print(
        f"[yellow]not yet implemented:[/yellow] [bold]{command}[/bold]\n"
        f"  planned implementation: [dim]{implemented_by}[/dim]"
    )


def info(message: str) -> None:
    stderr.print(f"[cyan]info[/cyan] {message}")


def success(message: str) -> None:
    stdout.print(f"[green]✓[/green] {message}")


def warn(message: str) -> None:
    stderr.print(f"[yellow]warning[/yellow] {message}")


def error(message: str) -> None:
    stderr.print(f"[red]error[/red] {message}")
