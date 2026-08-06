"""`sidepage setup` — installs the `cloudflared` binary tunnel functionality
needs (`serve --anon`, `serve --domain`), without making it a Python
dependency or vendoring it into the wheel (it's a real Go binary, not
something pip can ship inside a pure-Python wheel).

Two locations, deliberately different from each other:

  - **The managed binary** (`sidepage.config.settings.cloudflared_binary_path`,
    `~/.cache/sidepage/bin/cloudflared`) — always written here, a fixed
    location `sidepage.core.tunnel_manager.resolve_cloudflared_binary` can
    check directly regardless of `PATH`. This is what makes `setup`
    idempotent: re-running it finds this file and skips straight to
    verification instead of re-downloading.
  - **The PATH-discoverable link** (`resolve_link_dir`) — a symlink (or, if
    the platform can't symlink, a copy) placed in a user-local or venv bin
    directory so a bare `cloudflared` command also works, per the ticket's
    "must end up discoverable on PATH" requirement. Preferred order: a
    user-local bin dir already on `PATH`, then the active venv's bin dir
    (guaranteed on `PATH` for whatever session just ran `pip install
    sidepage`), then the user-local dir anyway as a last resort — `setup`
    warns explicitly if that last case leaves nothing actually on `PATH`.
    `--system` skips straight to a system bin dir instead, and is the only
    path that might need elevated privileges.

Downloads the latest release directly from Cloudflare's GitHub releases
(`github.com/cloudflare/cloudflared/releases/latest/download/<asset>`) —
the same binaries `brew install cloudflared` and Cloudflare's own install
docs point at, just fetched without a package manager. macOS assets are a
`.tgz` wrapping the binary (code-signing/notarization reasons on
Cloudflare's end); Linux and Windows assets are the raw executable.

Deliberately does **not** run automatically from inside `serve`/tunnel
code — see `tunnel_manager.resolve_cloudflared_binary`'s docstring for why
a silent first-use network fetch would be out of character for this
project. `setup` is the one explicit, opt-in place this reaches the
network for a binary.
"""

from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from sidepage.config.settings import cloudflared_bin_dir, cloudflared_binary_path
from sidepage.core.exceptions import CloudflaredInstallError

_RELEASE_BASE = "https://github.com/cloudflare/cloudflared/releases/latest/download"

# platform.machine() spellings vary by OS/libc — normalized to the
# vocabulary Cloudflare's release asset names use.
_ARCH_ALIASES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "i386": "386",
    "i686": "386",
    "x86": "386",
    "armv7l": "arm",
    "armv6l": "arm",
}

_ASSET_NAMES: dict[tuple[str, str], str] = {
    ("linux", "amd64"): "cloudflared-linux-amd64",
    ("linux", "386"): "cloudflared-linux-386",
    ("linux", "arm"): "cloudflared-linux-arm",
    ("linux", "arm64"): "cloudflared-linux-arm64",
    ("darwin", "amd64"): "cloudflared-darwin-amd64.tgz",
    ("darwin", "arm64"): "cloudflared-darwin-arm64.tgz",
    ("windows", "amd64"): "cloudflared-windows-amd64.exe",
    ("windows", "386"): "cloudflared-windows-386.exe",
}


@dataclass(frozen=True)
class InstallResult:
    binary: Path  # the managed binary — always this path, regardless of linked_dir
    version: str  # `cloudflared --version`'s output, trimmed
    already_installed: bool  # True if nothing was downloaded this call
    linked_dir: Path | None  # where a PATH-discoverable link was placed, if any


def detect_platform() -> tuple[str, str]:
    """Normalize `platform.system()`/`platform.machine()` into the
    `(os, arch)` vocabulary `_ASSET_NAMES` is keyed on. Raises
    `CloudflaredInstallError` for anything not in that table rather than
    guessing."""
    system = platform.system().lower()
    if system not in ("linux", "darwin", "windows"):
        raise CloudflaredInstallError(f"unsupported operating system: {platform.system()!r}")
    machine = platform.machine().lower()
    arch = _ARCH_ALIASES.get(machine)
    if arch is None:
        raise CloudflaredInstallError(f"unsupported CPU architecture: {platform.machine()!r}")
    return system, arch


def _asset_name(os_name: str, arch: str) -> str:
    try:
        return _ASSET_NAMES[(os_name, arch)]
    except KeyError:
        raise CloudflaredInstallError(
            f"no cloudflared release available for {os_name}/{arch}"
        ) from None


def _venv_bin_dir() -> Path | None:
    """`None` outside a virtualenv — `sys.prefix == sys.base_prefix` is the
    standard tell, same check `venv`/`pip` themselves use."""
    if sys.prefix == sys.base_prefix:
        return None
    return Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin")


def _user_local_bin_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / "Programs" / "sidepage" / "bin"
    return Path.home() / ".local" / "bin"


def _system_bin_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("ProgramFiles") or "C:/Program Files"
        return Path(base) / "sidepage" / "bin"
    return Path("/usr/local/bin")


def is_dir_on_path(directory: Path) -> bool:
    entries = os.environ.get("PATH", "").split(os.pathsep)
    return str(directory) in entries


def resolve_link_dir(*, system: bool = False) -> Path:
    """Where `setup` places the PATH-discoverable symlink/copy — see the
    module docstring for the preference order and reasoning. Distinct from
    `cloudflared_binary_path()`, which is always the same fixed cache
    location no matter what this returns."""
    if system:
        return _system_bin_dir()
    user_local = _user_local_bin_dir()
    if is_dir_on_path(user_local):
        return user_local
    venv_bin = _venv_bin_dir()
    if venv_bin is not None:
        return venv_bin
    return user_local


def _download(url: str, dest: Path) -> None:
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=60.0) as resp:
            resp.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
    except httpx.HTTPError as exc:
        raise CloudflaredInstallError(f"failed to download cloudflared from {url}: {exc}") from exc


def _make_executable(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _unpack(downloaded: Path, os_name: str, dest: Path) -> None:
    """`downloaded` is either the raw `cloudflared` executable (Linux and
    Windows assets) or a `.tgz` wrapping it (macOS assets) — see module
    docstring. Writes the real binary to `dest` and makes it executable
    (a no-op on Windows)."""
    if downloaded.name.endswith((".tgz", ".tar.gz")):
        with tarfile.open(downloaded) as tar:
            member = next((m for m in tar.getmembers() if m.name.endswith("cloudflared")), None)
            if member is None:
                raise CloudflaredInstallError(
                    f"downloaded archive {downloaded.name} has no `cloudflared` entry in it"
                )
            extracted = tar.extractfile(member)
            if extracted is None:
                raise CloudflaredInstallError(f"could not read {member.name} from the archive")
            dest.write_bytes(extracted.read())
    else:
        shutil.copyfile(downloaded, dest)

    if os_name != "windows":
        _make_executable(dest)


def _verify(binary: Path) -> str:
    try:
        proc = subprocess.run(
            [str(binary), "--version"], capture_output=True, text=True, timeout=15
        )
    except OSError as exc:
        raise CloudflaredInstallError(f"{binary} did not run: {exc}") from exc
    output = (proc.stdout or proc.stderr).strip()
    if proc.returncode != 0:
        raise CloudflaredInstallError(f"`{binary} --version` exited {proc.returncode}: {output}")
    return output


def _link(binary: Path, link_dir: Path, os_name: str) -> Path:
    link_dir.mkdir(parents=True, exist_ok=True)
    link_name = "cloudflared.exe" if os_name == "windows" else "cloudflared"
    link_path = link_dir / link_name
    if link_path.exists() or link_path.is_symlink():
        link_path.unlink()
    try:
        link_path.symlink_to(binary)
    except OSError:
        # No symlink privilege (common on Windows without dev mode) — fall
        # back to a real copy so the link dir still has a working binary.
        shutil.copyfile(binary, link_path)
        if os_name != "windows":
            _make_executable(link_path)
    return link_path


def is_installed() -> Path | None:
    """`PATH` lookup only — mirrors step 2 of
    `tunnel_manager.resolve_cloudflared_binary`, used here to decide
    whether `setup` has anything to do at all."""
    found = shutil.which("cloudflared")
    return Path(found) if found else None


def ensure_installed(*, force: bool = False, system: bool = False) -> InstallResult:
    """`sidepage setup`'s real work. Idempotent: a second call with the
    same arguments does no network I/O, just re-verifies and (if needed)
    re-links. Checked in this order unless `force` is set:

      1. Already on `PATH` (e.g. `brew install cloudflared`) — nothing to
         install, nothing to link either.
      2. Already in the managed cache from a prior `setup` run — skip the
         download, just verify and (re-)link.
      3. Neither — download, unpack, verify, link.
    """
    if not force:
        on_path = is_installed()
        if on_path is not None:
            return InstallResult(
                binary=on_path, version=_verify(on_path), already_installed=True, linked_dir=None
            )

        managed = cloudflared_binary_path()
        if managed.is_file():
            version = _verify(managed)
            link_dir = resolve_link_dir(system=system)
            linked = _link(managed, link_dir, platform.system().lower())
            return InstallResult(
                binary=managed, version=version, already_installed=True, linked_dir=linked.parent
            )

    os_name, arch = detect_platform()
    asset = _asset_name(os_name, arch)
    url = f"{_RELEASE_BASE}/{asset}"

    managed = cloudflared_binary_path()
    cloudflared_bin_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory() as tmp:
        downloaded = Path(tmp) / asset
        _download(url, downloaded)
        _unpack(downloaded, os_name, managed)

    version = _verify(managed)
    link_dir = resolve_link_dir(system=system)
    linked = _link(managed, link_dir, os_name)

    return InstallResult(
        binary=managed, version=version, already_installed=False, linked_dir=linked.parent
    )
