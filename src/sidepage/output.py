"""Shared console output helpers used across all `sidepage` subcommands.

This module is part of the CLI shell itself (argument parsing / presentation),
not the SDK ("the package") that will eventually implement each command's
behavior. It exists so every command prints in a consistent style instead of
each module rolling its own `print()` calls.
"""

from __future__ import annotations

import json
import sys

from rich.console import Console

# Two consoles, mirroring the stdout/stderr split most CLIs need: `stdout`
# for data a user might pipe (`sidepage ls | jq ...`), `stderr` for status,
# warnings, and errors that shouldn't pollute piped output.
stdout = Console()
stderr = Console(stderr=True)

# Rich infers styling for things that look like numbers, paths, and URLs.
# That reads well in prose and badly in a block where every value has to be
# read exactly as written — a commit sha, a version, the path of a script
# about to be executed. `plain` is the same stdout console with that
# inference off, for plan output and confirmation prompts.
plain = Console(highlight=False)


def is_interactive() -> bool:
    """Whether there's a human at a terminal to prompt.

    The single seam every confirmation gate asks, rather than each one
    calling `sys.stdin.isatty()` itself — partly for consistency, partly
    because a test that patches `sys.stdin.isatty` directly silently
    misses: Typer's `CliRunner` replaces `sys.stdin` for the duration of
    an invocation, so the patch lands on an object the command never
    sees, and a gate meant to prompt takes the non-interactive branch
    instead. Both callers here refuse rather than proceed when this is
    false, so a test that quietly exercised the wrong branch would be
    asserting the safe outcome for the wrong reason.
    """
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):  # closed or replaced stdin
        return False


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


# `--json` mode. `info`/`warn`/`error` already write to stderr, so stdout
# is nearly a clean channel already — `success` is the one helper that
# doesn't, and this flag moves it. The point is that `sidepage serve
# --json` can be parsed with `stdout | jq` without a caller having to
# filter chrome out of the stream first, while every human-readable line
# stays visible on stderr rather than being thrown away.
_json_mode = False


def set_json_mode(enabled: bool) -> None:
    """Route `success` to stderr so stdout carries only machine-readable
    output. Set once, at command entry, before anything prints."""
    global _json_mode
    _json_mode = enabled


def json_line(payload: object) -> None:
    """The one thing `--json` writes to stdout: a single line of JSON,
    terminated, flushed. Deliberately `print` and not a Rich console —
    Rich wraps, styles, and can insert soft line breaks into long values
    (a tunnel URL is easily wide enough), any of which would corrupt a
    stream something else is about to parse."""
    print(json.dumps(payload, separators=(",", ":")), flush=True)


def info(message: str) -> None:
    stderr.print(f"[cyan]info[/cyan] {message}")


def success(message: str) -> None:
    (stderr if _json_mode else stdout).print(f"[green]✓[/green] {message}")


def warn(message: str) -> None:
    stderr.print(f"[yellow]warning[/yellow] {message}")


def error(message: str) -> None:
    stderr.print(f"[red]error[/red] {message}")
