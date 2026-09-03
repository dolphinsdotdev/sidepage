"""`sidepage pull <source>` — fetch a remote app's source into a managed
directory, resolve how it would run, and register it. **Nothing is
executed.**

The ordering is the design. Every step that can refuse runs before the
step that costs bandwidth, and every step that costs bandwidth runs before
anything is written to the registry:

    resolve source form  →  fetch metadata  →  refuse the un-runnable
                         →  resolve the run plan  →  download files
                         →  register  →  print the plan

`--dry-run` stops after the plan, having downloaded nothing at all. That's
the mode to reach for against a Space carrying tens of gigabytes of
weights: the metadata already knows the total size, so the cost of a pull
is answerable without paying it.

**What this deliberately does not do**, and why each is a decision rather
than an omission:

  - **No dependency installation.** Sidepage is the installer, not the
    dependency manager: `serve` hands `requirements.txt` to `uv` on first
    request and whatever happens, happens. A Space pinning 80 packages
    including TensorFlow will take minutes to resolve the first time, and
    that's the app's own weight, not something sidepage should try to
    curate, prune, or second-guess. If the resolve fails, it fails in the
    logs where a real error message is more useful than a prediction.
  - **No version pinning of the framework.** A Space's `sdk_version` is
    *displayed* in the plan but not forced onto the launcher. Honoring it
    would mean sidepage picking framework versions on the user's behalf —
    dependency management again — and the app's own requirements file
    already gets the last word.
  - **No execution of anything**: not the entrypoint, not a setup hook,
    not a build step. The only file read is the entrypoint, scanned as
    text for environment-variable names.
  - **No secret granting.** Requested env names are printed; nothing is
    bound until an explicit `serve --env`.
  - **No tunnel, no port, no network exposure.**

**Everything a manifest says is attacker-controlled.** A Space's README is
written by whoever owns the Space, and `pull` may be run by an agent that
never shows a human the output. `safe_relative_path` below is the
containment boundary for any path that arrives from one; env names are
validated against a strict pattern before display so a crafted "name"
can't smuggle instructions into a plan an agent reads.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from sidepage.config.settings import app_source_dir
from sidepage.core import hf
from sidepage.core.exceptions import SourceError, SourceNotSupportedError
from sidepage.core.target import CodeLauncher, TargetKind, detect_code_launcher

# Dependency manifests sidepage recognizes, in the order `serve` would
# actually use them. Recognition only — nothing here installs anything.
_DEPENDENCY_FILES = ("requirements.txt", "pyproject.toml", "uv.lock", "package.json")

# A conservative env-var name: what the shell and every framework agree on.
# Applied to names *scanned out of a stranger's source* before they're
# printed, so a crafted string can't turn a plan into a paragraph.
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")

# `os.environ["X"]`, `os.environ.get("X")`, `os.getenv("X")`, and the
# `environ[...]`/`getenv(...)` forms left behind by `from os import ...`.
_ENV_USE_RE = re.compile(
    r"""(?:os\.)?(?:environ\s*(?:\.\s*get\s*\(|\[)|getenv\s*\()\s*["']([A-Za-z_][A-Za-z0-9_]*)["']"""
)

# App names become a directory under `apps_dir()` and a registry key, so
# they're held to the same shape sidepage's own generated names have.
_APP_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


@dataclass(frozen=True)
class RunPlan:
    """How a pulled app would run, resolved but not run. This is what
    `pull` prints, what `--json` emits, and what a `serve` trust prompt
    shows before executing anything."""

    target_kind: TargetKind
    launcher: str  # display name — "gradio", "streamlit", "static", ...
    entrypoint: Path | None  # relative to the app directory; None for static
    dependency_file: str | None
    dependency_count: int | None
    python_version: str | None
    sdk: str | None
    sdk_version: str | None
    env_names: tuple[str, ...]
    download_bytes: int
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedSource:
    """A source form resolved to something fetchable. Only Hugging Face
    Spaces resolve today; every other form raises rather than resolving to
    a guess."""

    kind: str  # "huggingface-space"
    url: str
    default_name: str
    space: hf.HfSpace


def safe_relative_path(candidate: str, *, base: Path, label: str) -> Path:
    """Resolve `candidate` — a path that came out of a manifest, i.e. from
    a stranger — against `base`, refusing anything that escapes it.

    Refuses absolute paths, parent traversal (`../../.config/sidepage/
    vault.enc`), and symlinks pointing outside `base`. The symlink case is
    the one worth being explicit about: a repo can contain a symlink, so a
    check that only inspects the *string* would happily accept
    `app_file: link.py` where `link.py` points at something outside the
    app directory entirely. Resolving both sides and comparing is what
    actually closes that, and it's why this runs after download rather
    than only on the manifest text.

    Raises `sidepage.core.exceptions.SourceError` naming the offending
    value — never silently falls back to a default, which would let a
    malicious manifest quietly get sidepage to run something else.
    """
    if not candidate or candidate.strip() != candidate:
        raise SourceError(f"{label} {candidate!r} isn't a usable path")
    raw = Path(candidate)
    if raw.is_absolute() or raw.drive or raw.root:
        raise SourceError(
            f"{label} {candidate!r} is an absolute path — a pulled app may only reference "
            "files inside its own directory."
        )
    if any(part == ".." for part in raw.parts):
        raise SourceError(
            f"{label} {candidate!r} escapes the app directory — refusing to resolve it."
        )

    base_resolved = base.resolve()
    target = (base_resolved / raw).resolve()
    if target != base_resolved and base_resolved not in target.parents:
        raise SourceError(
            f"{label} {candidate!r} resolves outside the app directory "
            "(a symlink pointing elsewhere?) — refusing to use it."
        )
    return raw


def valid_app_name(name: str) -> bool:
    """Whether `name` is safe as both a registry key and a directory name
    under `apps_dir()`. Rejects path separators, leading dots, and
    anything long enough to be doing something other than naming an app."""
    return bool(_APP_NAME_RE.match(name)) and name not in (".", "..")


def resolve_source(source: str) -> ResolvedSource:
    """Resolve a source string to something fetchable.

    Hugging Face Spaces resolve for real. GitHub repos, MCP registry
    names, and local paths are recognized well enough to say *what* they
    are and that they aren't built yet — an unrecognized form is refused
    outright rather than guessed at, since guessing which host a bare
    `owner/repo` belongs to could download a completely different
    stranger's code than the user meant.

    Raises `sidepage.core.exceptions.SourceNotSupportedError` for
    everything that isn't a Space.
    """
    text = source.strip()

    space_ref = hf.parse_space_ref(text)
    if space_ref is not None:
        owner, name = space_ref
        space = hf.fetch_space(owner, name)
        return ResolvedSource(
            kind="huggingface-space",
            url=space.url,
            default_name=name,
            space=space,
        )

    stripped = text.removeprefix("https://").removeprefix("http://")
    if stripped.startswith("github.com/") or stripped.startswith("gh:"):
        raise SourceNotSupportedError(
            "GitHub sources aren't implemented — only Hugging Face Spaces are supported today "
            "(`hf:<owner>/<name>` or `huggingface.co/spaces/<owner>/<name>`)."
        )
    if stripped.startswith("mcp:"):
        raise SourceNotSupportedError(
            "MCP registry sources aren't implemented — only Hugging Face Spaces are supported "
            "today. Serve a local MCP server directly with `sidepage serve <script.py>`."
        )
    if text.startswith(".") or text.startswith("/") or text.startswith("~") or Path(text).exists():
        raise SourceNotSupportedError(
            f"{text!r} looks like a local path. `pull` is for remote sources — serve a local "
            "target directly with `sidepage serve <path>`, which doesn't download anything."
        )
    if "/" in stripped and stripped.count("/") == 1:
        raise SourceNotSupportedError(
            f"{text!r} is ambiguous — a bare `<owner>/<name>` could be a Hugging Face Space or a "
            f"GitHub repo, and sidepage won't guess. Write `hf:{stripped}` if you mean the Space."
        )
    raise SourceNotSupportedError(
        f"{text!r} isn't a source sidepage recognizes. Supported: "
        "`hf:<owner>/<name>` or `huggingface.co/spaces/<owner>/<name>`."
    )


def scan_env_names(source_text: str) -> tuple[str, ...]:
    """Environment-variable names a script reads, by scanning its text for
    `os.environ[...]` / `os.environ.get(...)` / `os.getenv(...)`.

    A **heuristic, and labeled as one wherever it's displayed**: it can't
    see a name built at runtime, and it will happily report one used
    inside dead code. It exists so a pull can say "this app appears to
    want FINNHUB_API_KEY" instead of the user discovering it from a
    stack trace after granting nothing. Names only — this never reads,
    resolves, or grants a value, and names that don't look like
    environment variables are dropped rather than displayed, since the
    output is read by agents as well as people.
    """
    found = {
        match.group(1)
        for match in _ENV_USE_RE.finditer(source_text)
        if _ENV_NAME_RE.match(match.group(1))
    }
    # `PORT` is injected by sidepage itself for generic targets, and HF
    # Spaces routinely read it — reporting it as something the user must
    # grant would be actively misleading.
    found.discard("PORT")
    return tuple(sorted(found))


def _dependency_file(space: hf.HfSpace) -> str | None:
    present = {f.path for f in space.files}
    return next((name for name in _DEPENDENCY_FILES if name in present), None)


def _count_requirements(path: Path) -> int | None:
    """Number of declared packages in a `requirements.txt` — a rough count
    of non-empty, non-comment lines, used only to give the user a sense of
    how heavy the first `serve` will be."""
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except OSError:
        return None
    return sum(1 for line in lines if line.strip() and not line.strip().startswith("#"))


_SDK_LAUNCHER = {
    "gradio": (TargetKind.CODE, "gradio"),
    "streamlit": (TargetKind.CODE, "streamlit"),
    "static": (TargetKind.STATIC, "static"),
}


def plan_from_space(space: hf.HfSpace, *, app_dir: Path | None = None) -> RunPlan:
    """Resolve how a Space would run.

    Callable before download (`app_dir=None`, as `--dry-run` does) — in
    which case the plan is built from metadata alone and the parts that
    need the files on disk (dependency count, env-name scan, launcher
    cross-check) are simply absent rather than guessed at.
    """
    warnings = list(hf.runtime_warnings(space))
    target_kind, launcher = _SDK_LAUNCHER[space.sdk]

    entrypoint: Path | None = None
    if target_kind is TargetKind.CODE:
        declared = space.app_file or "app.py"
        # Validated against the *metadata* here; re-validated against the
        # real directory after download, where symlinks become visible.
        if app_dir is None:
            check_base = Path("/nonexistent-sidepage-plan-base")
            entrypoint = _validate_manifest_path(declared, check_base)
        else:
            entrypoint = safe_relative_path(declared, base=app_dir, label="app_file")
            if not (app_dir / entrypoint).is_file():
                raise SourceError(
                    f"the Space declares app_file: {declared!r}, but that file isn't in the "
                    "downloaded repository."
                )
        if space.app_file is None and space.file("app.py") is None:
            warnings.append(
                "no app_file declared and no app.py in the repository — `serve` will fall back "
                "to its own detection and may not find an entrypoint."
            )

    dependency_file = _dependency_file(space)
    dependency_count: int | None = None
    env_names: tuple[str, ...] = ()
    if app_dir is not None:
        if dependency_file == "requirements.txt":
            dependency_count = _count_requirements(app_dir / dependency_file)
        if entrypoint is not None:
            try:
                env_names = scan_env_names((app_dir / entrypoint).read_text(errors="ignore"))
            except OSError:
                env_names = ()
        if target_kind is TargetKind.CODE and entrypoint is not None:
            detected = detect_code_launcher(app_dir / entrypoint)
            expected = {
                "gradio": CodeLauncher.GRADIO,
                "streamlit": CodeLauncher.STREAMLIT,
            }.get(launcher)
            if expected is not None and detected is not expected:
                warnings.append(
                    f"the Space declares sdk: {space.sdk}, but {entrypoint} looks like a "
                    f"{detected.value} target — sidepage will launch it as {detected.value}."
                )

    if space.sdk_version:
        warnings.append(
            f"the Space was built against {space.sdk} {space.sdk_version}; sidepage installs "
            "whatever the app's own dependency file resolves to, and doesn't pin it."
        )

    return RunPlan(
        target_kind=target_kind,
        launcher=launcher,
        entrypoint=entrypoint,
        dependency_file=dependency_file,
        dependency_count=dependency_count,
        python_version=space.python_version,
        sdk=space.sdk,
        sdk_version=space.sdk_version,
        env_names=env_names,
        download_bytes=space.total_bytes,
        warnings=tuple(warnings),
    )


def _validate_manifest_path(candidate: str, base: Path) -> Path:
    """String-only half of `safe_relative_path`, for planning before the
    files exist. The filesystem half (symlink containment) runs again
    after download — this catches an absolute path or a `..` escape early
    enough that a hostile manifest is refused before anything is fetched.
    """
    if not candidate or candidate.strip() != candidate:
        raise SourceError(f"app_file {candidate!r} isn't a usable path")
    raw = Path(candidate)
    if raw.is_absolute() or raw.drive or raw.root:
        raise SourceError(
            f"app_file {candidate!r} is an absolute path — a pulled app may only reference "
            "files inside its own directory."
        )
    if any(part == ".." for part in raw.parts):
        raise SourceError(f"app_file {candidate!r} escapes the app directory — refusing to pull.")
    return raw


def fetch_into(resolved: ResolvedSource, app_name: str, *, on_file=None) -> Path:
    """Download `resolved` into this app's managed directory, replacing
    whatever was there.

    Downloads into a sibling staging directory and swaps it in, so an
    interrupted or failed pull can't leave a half-updated tree behind that
    `serve` would then happily run. Always fetches the source's current
    state — there's no version tracking, so a re-pull is simply "get what's
    there now" and is the same operation as the first pull.
    """
    dest = app_source_dir(app_name)
    staging = dest.with_name(f".{dest.name}.incoming")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)

    try:
        hf.download_space(resolved.space, staging, on_file=on_file)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    if dest.exists():
        shutil.rmtree(dest)
    staging.replace(dest)
    return dest


def remove_source_tree(app_name: str) -> int | None:
    """Delete a pulled app's managed directory. Returns the number of
    bytes removed, or `None` if there was nothing there.

    **Refuses to touch anything outside `apps_dir()`** — the check that
    keeps `sidepage app delete` from ever deleting a user's own project
    directory for an app that was registered against a local path rather
    than pulled.
    """
    from sidepage.config.settings import apps_dir

    dest = app_source_dir(app_name)
    root = apps_dir().resolve()
    if not dest.exists():
        return None
    if root not in dest.resolve().parents:
        raise SourceError(
            f"{dest} isn't inside sidepage's managed app directory — refusing to delete it."
        )
    size = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
    shutil.rmtree(dest)
    return size


def format_bytes(count: int) -> str:
    """Human-readable size for the plan output — the number that tells
    someone whether a pull is a rounding error or a coffee break."""
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
