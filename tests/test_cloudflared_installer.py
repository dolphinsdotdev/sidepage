"""Tests for `sidepage.core.cloudflared_installer` (`sidepage setup`) —
platform/arch normalization, install-directory preference order, and the
download/unpack/verify/link pipeline `ensure_installed` drives.

No real network access: `_download` (and, where a test isn't specifically
exercising it, `_verify`) is monkeypatched to write/return canned data
instead of really hitting Cloudflare's release URL or running a real
`cloudflared --version` — the same boundary-mocking approach
`test_tunnel_byo.py` uses for the Cloudflare API rather than making real
calls.
"""

from __future__ import annotations

import io
import os
import stat
import tarfile
from pathlib import Path

import pytest

from sidepage.core import cloudflared_installer as installer
from sidepage.core.exceptions import CloudflaredInstallError


@pytest.fixture
def sidepage_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SIDEPAGE_HOME", str(tmp_path))
    return tmp_path


# --- detect_platform: pure, no I/O ---


def test_detect_platform_normalizes_known_combo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(installer.platform, "machine", lambda: "arm64")
    assert installer.detect_platform() == ("darwin", "arm64")


def test_detect_platform_normalizes_x86_64_to_amd64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer.platform, "system", lambda: "Linux")
    monkeypatch.setattr(installer.platform, "machine", lambda: "x86_64")
    assert installer.detect_platform() == ("linux", "amd64")


def test_detect_platform_unsupported_os_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer.platform, "system", lambda: "PlayStation")
    with pytest.raises(CloudflaredInstallError):
        installer.detect_platform()


def test_detect_platform_unsupported_arch_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer.platform, "system", lambda: "Linux")
    monkeypatch.setattr(installer.platform, "machine", lambda: "sparc64")
    with pytest.raises(CloudflaredInstallError):
        installer.detect_platform()


# --- resolve_link_dir: preference order ---


def test_resolve_link_dir_prefers_user_local_when_already_on_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    user_local = tmp_path / "userlocalbin"
    monkeypatch.setattr(installer, "_user_local_bin_dir", lambda: user_local)
    monkeypatch.setenv("PATH", str(user_local))
    assert installer.resolve_link_dir() == user_local


def test_resolve_link_dir_falls_back_to_venv_when_user_local_not_on_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    user_local = tmp_path / "userlocalbin"
    venv_bin = tmp_path / "venvbin"
    monkeypatch.setattr(installer, "_user_local_bin_dir", lambda: user_local)
    monkeypatch.setattr(installer, "_venv_bin_dir", lambda: venv_bin)
    monkeypatch.setenv("PATH", "/usr/bin")
    assert installer.resolve_link_dir() == venv_bin


def test_resolve_link_dir_falls_back_to_user_local_outside_venv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    user_local = tmp_path / "userlocalbin"
    monkeypatch.setattr(installer, "_user_local_bin_dir", lambda: user_local)
    monkeypatch.setattr(installer, "_venv_bin_dir", lambda: None)
    monkeypatch.setenv("PATH", "/usr/bin")
    assert installer.resolve_link_dir() == user_local


def test_resolve_link_dir_system_flag_skips_straight_to_system_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    system_dir = tmp_path / "systembin"
    monkeypatch.setattr(installer, "_system_bin_dir", lambda: system_dir)
    assert installer.resolve_link_dir(system=True) == system_dir


def test_is_dir_on_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    d = tmp_path / "bin"
    monkeypatch.setenv("PATH", f"/usr/bin{os.pathsep}{d}{os.pathsep}/bin")
    assert installer.is_dir_on_path(d) is True
    assert installer.is_dir_on_path(tmp_path / "other") is False


# --- _unpack / _verify: the parts of the pipeline that don't need a real
# network fetch to exercise meaningfully ---


def test_unpack_tgz_extracts_named_member(tmp_path: Path) -> None:
    archive = tmp_path / "cloudflared-darwin-arm64.tgz"
    payload = b"fake macho contents"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(name="cloudflared")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    dest = tmp_path / "cloudflared"
    installer._unpack(archive, "darwin", dest)

    assert dest.read_bytes() == payload
    assert dest.stat().st_mode & stat.S_IXUSR


def test_unpack_tgz_missing_cloudflared_member_raises(tmp_path: Path) -> None:
    archive = tmp_path / "bad.tgz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(name="not-the-right-file")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"abc"))

    with pytest.raises(CloudflaredInstallError):
        installer._unpack(archive, "darwin", tmp_path / "cloudflared")


def test_unpack_raw_binary_copies_and_chmods(tmp_path: Path) -> None:
    raw = tmp_path / "cloudflared-linux-amd64"
    raw.write_bytes(b"raw elf contents")
    dest = tmp_path / "cloudflared"

    installer._unpack(raw, "linux", dest)

    assert dest.read_bytes() == b"raw elf contents"
    assert dest.stat().st_mode & stat.S_IXUSR


def test_verify_nonzero_exit_raises(tmp_path: Path) -> None:
    binary = tmp_path / "cloudflared"
    binary.write_text("#!/bin/sh\nexit 1\n")
    binary.chmod(0o755)
    with pytest.raises(CloudflaredInstallError):
        installer._verify(binary)


def test_verify_returns_trimmed_output(tmp_path: Path) -> None:
    binary = tmp_path / "cloudflared"
    binary.write_text("#!/bin/sh\necho 'cloudflared version 9.9.9'\n")
    binary.chmod(0o755)
    assert installer._verify(binary) == "cloudflared version 9.9.9"


# --- ensure_installed: the three resolution branches ---


def test_ensure_installed_noop_when_already_on_path(
    monkeypatch: pytest.MonkeyPatch, sidepage_home: Path, tmp_path: Path
) -> None:
    fake_binary = tmp_path / "cloudflared"
    monkeypatch.setattr(installer, "is_installed", lambda: fake_binary)
    monkeypatch.setattr(installer, "_verify", lambda binary: "cloudflared version 9.9.9")
    monkeypatch.setattr(
        installer,
        "_download",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not download")),
    )

    result = installer.ensure_installed()

    assert result.already_installed is True
    assert result.binary == fake_binary
    assert result.linked_dir is None
    assert result.version == "cloudflared version 9.9.9"


def test_ensure_installed_reuses_managed_cache_without_downloading(
    monkeypatch: pytest.MonkeyPatch, sidepage_home: Path, tmp_path: Path
) -> None:
    from sidepage.config.settings import cloudflared_binary_path

    managed = cloudflared_binary_path()
    managed.parent.mkdir(parents=True, exist_ok=True)
    managed.write_bytes(b"fake binary")
    managed.chmod(managed.stat().st_mode | stat.S_IXUSR)

    link_dir = tmp_path / "linkdir"
    monkeypatch.setattr(installer, "is_installed", lambda: None)
    monkeypatch.setattr(installer, "_verify", lambda binary: "cloudflared version 9.9.9")
    monkeypatch.setattr(installer, "resolve_link_dir", lambda **k: link_dir)
    monkeypatch.setattr(
        installer,
        "_download",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not download")),
    )

    result = installer.ensure_installed()

    assert result.already_installed is True
    assert result.binary == managed
    assert result.linked_dir == link_dir
    assert (link_dir / "cloudflared").is_symlink()
    assert (link_dir / "cloudflared").resolve() == managed.resolve()


def test_ensure_installed_fresh_install_downloads_unpacks_verifies_links(
    monkeypatch: pytest.MonkeyPatch, sidepage_home: Path, tmp_path: Path
) -> None:
    from sidepage.config.settings import cloudflared_binary_path

    monkeypatch.setattr(installer, "is_installed", lambda: None)
    monkeypatch.setattr(installer, "detect_platform", lambda: ("linux", "amd64"))
    monkeypatch.setattr(installer, "_verify", lambda binary: "cloudflared version 1.2.3")
    link_dir = tmp_path / "linkdir"
    monkeypatch.setattr(installer, "resolve_link_dir", lambda **k: link_dir)

    downloaded_urls = []

    def fake_download(url: str, dest: Path) -> None:
        downloaded_urls.append(url)
        dest.write_bytes(b"fake cloudflared binary")

    monkeypatch.setattr(installer, "_download", fake_download)

    result = installer.ensure_installed()

    assert downloaded_urls == [f"{installer._RELEASE_BASE}/cloudflared-linux-amd64"]
    assert result.already_installed is False
    assert result.version == "cloudflared version 1.2.3"
    managed = cloudflared_binary_path()
    assert managed.read_bytes() == b"fake cloudflared binary"
    assert managed.stat().st_mode & stat.S_IXUSR
    assert (link_dir / "cloudflared").resolve() == managed.resolve()

    # Re-running finds the managed binary from the first call and skips
    # downloading again — the idempotency the ticket asks for.
    monkeypatch.setattr(
        installer,
        "_download",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not re-download")),
    )
    second = installer.ensure_installed()
    assert second.already_installed is True


def test_ensure_installed_darwin_tgz_asset_is_extracted_not_copied_raw(
    monkeypatch: pytest.MonkeyPatch, sidepage_home: Path, tmp_path: Path
) -> None:
    monkeypatch.setattr(installer, "is_installed", lambda: None)
    monkeypatch.setattr(installer, "detect_platform", lambda: ("darwin", "arm64"))
    monkeypatch.setattr(installer, "_verify", lambda binary: "cloudflared version 4.5.6")
    monkeypatch.setattr(installer, "resolve_link_dir", lambda **k: tmp_path / "linkdir")

    def fake_download(url: str, dest: Path) -> None:
        assert url.endswith("cloudflared-darwin-arm64.tgz")
        payload = b"fake macho contents"
        with tarfile.open(dest, "w:gz") as tar:
            info = tarfile.TarInfo(name="cloudflared")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))

    monkeypatch.setattr(installer, "_download", fake_download)

    result = installer.ensure_installed()

    assert result.binary.read_bytes() == b"fake macho contents"
    assert result.binary.stat().st_mode & stat.S_IXUSR


def test_ensure_installed_force_redownloads_even_if_already_on_path(
    monkeypatch: pytest.MonkeyPatch, sidepage_home: Path, tmp_path: Path
) -> None:
    monkeypatch.setattr(installer, "is_installed", lambda: Path("/usr/bin/cloudflared"))
    monkeypatch.setattr(installer, "detect_platform", lambda: ("linux", "amd64"))
    monkeypatch.setattr(installer, "_verify", lambda binary: "cloudflared version 1.2.3")
    monkeypatch.setattr(installer, "resolve_link_dir", lambda **k: tmp_path / "linkdir")

    called = []

    def fake_download(url: str, dest: Path) -> None:
        called.append(url)
        dest.write_bytes(b"fresh binary")

    monkeypatch.setattr(installer, "_download", fake_download)

    result = installer.ensure_installed(force=True)

    assert called
    assert result.already_installed is False
