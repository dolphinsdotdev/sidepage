"""`--pwa`/`--qr` — spec: `--pwa` mode for sidepage (§10's numbered test
list is covered directly below, referenced by number in each test name).

Split the same way `test_proxy.py` is: fast, dependency-free unit tests on
`sidepage.core.pwa`'s pure functions (manifest/icon/hex-color logic,
`inject_head_tags`), plus real-server integration tests that exercise
`PwaInjectionMiddleware` and the synthetic routes through an actual
`reverse_proxy.start_proxy()` instance and real HTTP — `sidepage.core.pwa`
is itself a pure, request-independent module by design (see its own
docstring), so most of what it does is fully testable without a server at
all; only the injection-in-flight behavior (headers, streaming, gzip)
needs one.

The "already running" upstream for the integration tests is a small
in-process `http.server`, standing in for the wrapped app the same way
`test_proxy.py`'s `_EchoHandler` does — not launched via `sidepage serve`
itself (no framework detection needed for what these tests check), so
`reverse_proxy.start_proxy()` is called directly rather than going through
the CLI or `sidepage.core.process.serve`.
"""

from __future__ import annotations

import gzip
import http.server
import json
import struct
import threading
import time
import zlib
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from sidepage.cli import app as cli_app
from sidepage.core import pwa, reverse_proxy
from sidepage.core.exceptions import PwaConfigError
from sidepage.core.target import allocate_port

runner = CliRunner()


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.fixture(autouse=True)
def _isolated_sidepage_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIDEPAGE_HOME", str(tmp_path))


def _make_png(size: int) -> bytes:
    """A PNG whose IHDR claims `size`x`size` — `_read_png_dimensions` only
    reads the header, so the (single, mostly-empty) scanline body below
    doesn't need to be real pixel data for icon-validation tests."""
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    idat = zlib.compress(b"\x00" + bytes(4 * size), 9)  # one scanline
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


# --- 9. Icon validation --------------------------------------------------


def test_icon_rejects_non_png(tmp_path: Path) -> None:
    bad = tmp_path / "icon.png"
    bad.write_bytes(b"not a png at all")
    with pytest.raises(PwaConfigError, match="not a valid PNG"):
        pwa.build_runtime(pwa.PwaOptions(icon=bad), app_name="app", domain=None)


def test_icon_rejects_non_square(tmp_path: Path) -> None:
    bad = tmp_path / "icon.png"
    data = _make_png(512)
    # Corrupt just the height half of IHDR to make it non-square (600x512).
    data = data[:16] + struct.pack(">I", 600) + data[20:]
    bad.write_bytes(data)
    with pytest.raises(PwaConfigError, match=r"must be square, got 600x512"):
        pwa.build_runtime(pwa.PwaOptions(icon=bad), app_name="app", domain=None)


def test_icon_rejects_undersized(tmp_path: Path) -> None:
    small = tmp_path / "icon.png"
    small.write_bytes(_make_png(256))
    with pytest.raises(PwaConfigError, match=r"must be >=512px, got 256x256"):
        pwa.build_runtime(pwa.PwaOptions(icon=small), app_name="app", domain=None)


def test_icon_accepts_valid_square_512_and_reuses_bytes_for_both_sizes(tmp_path: Path) -> None:
    good = tmp_path / "icon.png"
    data = _make_png(512)
    good.write_bytes(data)
    runtime = pwa.build_runtime(pwa.PwaOptions(icon=good), app_name="app", domain=None)
    assert runtime.icon_192 == data
    assert runtime.icon_512 == data


def test_bundled_default_icons_are_distinct_correctly_sized_pngs() -> None:
    runtime = pwa.build_runtime(pwa.PwaOptions(), app_name="app", domain=None)
    for data, expected_size in ((runtime.icon_192, 192), (runtime.icon_512, 512)):
        width, height = struct.unpack(">II", data[16:24])
        assert (width, height) == (expected_size, expected_size)


# --- Hex color validation --------------------------------------------------


@pytest.mark.parametrize("value", ["#fff", "#FFFFFF", "#a1b2c3"])
def test_hex_color_accepts_valid_forms(value: str) -> None:
    pwa.build_runtime(pwa.PwaOptions(theme=value), app_name="app", domain=None)  # no raise


@pytest.mark.parametrize("value", ["fff", "#ffff", "#gggggg", "red", ""])
def test_hex_color_rejects_bad_values(value: str) -> None:
    with pytest.raises(PwaConfigError, match="--pwa-theme"):
        pwa.build_runtime(pwa.PwaOptions(theme=value), app_name="app", domain=None)


# --- 8, 10. Manifest validity + relative URLs ------------------------------


def test_generated_manifest_has_required_installability_fields() -> None:
    runtime = pwa.build_runtime(pwa.PwaOptions(name="My App"), app_name="app", domain=None)
    manifest = json.loads(runtime.manifest_bytes)
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "./"
    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert manifest["name"].startswith("My App (")  # ephemeral session marker


def test_generated_manifest_has_no_absolute_hostname_anywhere() -> None:
    runtime = pwa.build_runtime(pwa.PwaOptions(name="My App"), app_name="app", domain="example.com")
    manifest = json.loads(runtime.manifest_bytes)
    assert manifest["start_url"] == "./"
    assert manifest["scope"] == "./"
    for icon in manifest["icons"]:
        assert icon["src"].startswith("./")
        assert "://" not in icon["src"]


def test_durable_manifest_gets_stable_id_ephemeral_does_not() -> None:
    durable_runtime = pwa.build_runtime(
        pwa.PwaOptions(name="App"), app_name="app", domain="example.com"
    )
    durable = json.loads(durable_runtime.manifest_bytes)
    ephemeral = json.loads(
        pwa.build_runtime(pwa.PwaOptions(name="App"), app_name="app", domain=None).manifest_bytes
    )
    assert durable["id"] == "example.com"
    assert durable["name"] == "App"  # durable: unsuffixed
    assert "id" not in ephemeral
    assert ephemeral["name"] != "App"  # ephemeral: session-marker suffixed
    assert ephemeral["short_name"] == ephemeral["short_name"][:12]


def test_pwa_manifest_served_verbatim(tmp_path: Path) -> None:
    custom = tmp_path / "manifest.json"
    custom.write_text('{"name": "Custom", "totally_custom_field": true}')
    runtime = pwa.build_runtime(
        pwa.PwaOptions(manifest=custom, name="ignored"), app_name="app", domain=None
    )
    assert runtime.manifest_bytes == custom.read_bytes()


def test_pwa_manifest_invalid_json_raises(tmp_path: Path) -> None:
    custom = tmp_path / "manifest.json"
    custom.write_text("not json")
    with pytest.raises(PwaConfigError, match="not valid JSON"):
        pwa.build_runtime(pwa.PwaOptions(manifest=custom), app_name="app", domain=None)


# --- 1, 5. Injection correctness -------------------------------------------


_HTML = b"<!doctype html><html><head><title>x</title></head><body>hi</body></html>"


def test_inject_head_tags_adds_exactly_one_manifest_link_and_preserves_rest() -> None:
    runtime = pwa.build_runtime(pwa.PwaOptions(), app_name="app", domain=None)
    injected = pwa.inject_head_tags(_HTML, runtime)
    assert injected.count(b'rel="manifest"') == 1
    # Byte-identical to the original except for one inserted block right
    # after the opening <head> tag — nothing before it moved, and
    # everything from </head> (the original's <title> plus the untouched
    # <body>) survives unchanged.
    before, after = _HTML.split(b"<head>", 1)
    assert injected.startswith(before + b"<head>")
    assert injected.endswith(after)


def test_inject_head_tags_no_head_tag_returns_body_unchanged() -> None:
    runtime = pwa.build_runtime(pwa.PwaOptions(), app_name="app", domain=None)
    body = b"<html><body>no head here</body></html>"
    assert pwa.inject_head_tags(body, runtime) == body
    assert pwa.has_head_tag(body) is False


def test_inject_head_tags_skips_existing_manifest_unless_forced() -> None:
    html_with_manifest = (
        b'<html><head><link rel="manifest" href="/other.json"></head><body></body></html>'
    )
    default_runtime = pwa.build_runtime(pwa.PwaOptions(), app_name="app", domain=None)
    not_forced = pwa.inject_head_tags(html_with_manifest, default_runtime)
    assert not_forced.count(b'rel="manifest"') == 1  # only the app's own, sidepage's not added
    assert b"theme-color" in not_forced  # meta/SW still injected regardless

    forced_runtime = pwa.build_runtime(pwa.PwaOptions(force=True), app_name="app", domain=None)
    forced = pwa.inject_head_tags(html_with_manifest, forced_runtime)
    assert forced.count(b'rel="manifest"') == 2


def test_inject_head_tags_omits_sw_script_when_no_sw() -> None:
    runtime = pwa.build_runtime(pwa.PwaOptions(no_sw=True), app_name="app", domain=None)
    assert runtime.sw_js is None
    injected = pwa.inject_head_tags(_HTML, runtime)
    assert b"serviceWorker" not in injected


def test_inject_head_tags_crossorigin_always_present() -> None:
    runtime = pwa.build_runtime(pwa.PwaOptions(), app_name="app", domain=None)
    injected = pwa.inject_head_tags(_HTML, runtime)
    assert b'crossorigin="use-credentials"' in injected


# --- CLI-layer validation ---------------------------------------------------


def test_cli_pwa_flag_without_pwa_errors(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("import os\n")
    result = runner.invoke(cli_app, ["serve", str(target), "--pwa-name", "Foo"])
    assert result.exit_code == 1, result.output
    assert "--pwa-* flags require --pwa" in _flat(result.output)


def test_cli_pwa_manifest_plus_other_field_warns_not_errors(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("import os\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"name": "x"}')
    # --timeout 0.1 lets a real serve() call self-terminate quickly instead
    # of blocking the test forever waiting for Ctrl+C — same technique
    # test_proxy.py's BYO-domain test uses for an in-process serve() call.
    result = runner.invoke(
        cli_app,
        [
            "serve",
            str(target),
            "--pwa",
            "--pwa-manifest",
            str(manifest),
            "--pwa-theme",
            "#123456",
            "--timeout",
            "0.1",
        ],
    )
    assert "ignored" in _flat(result.output)
    assert "requires --pwa" not in _flat(result.output)


def test_cli_bad_hex_color_reported_as_error(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("import os\n")
    result = runner.invoke(cli_app, ["serve", str(target), "--pwa", "--pwa-theme", "notacolor"])
    assert result.exit_code == 1, result.output
    assert "--pwa-theme" in _flat(result.output)


# --- --qr --------------------------------------------------------------


def test_print_qr_does_not_raise(capsys: pytest.CaptureFixture[str]) -> None:
    from sidepage.core import qr

    # capsys-captured stdout isn't a real tty, so this exercises the
    # graceful-warning path rather than actual QR rendering — the point
    # of this test is that a non-tty stdout can't crash `serve`.
    qr.print_qr("https://example.com/some/app")
    err = capsys.readouterr().err
    assert "isn't a terminal" in err


# --- Real-server integration: PwaInjectionMiddleware + synthetic routes ---


class _UpstreamHandler(http.server.BaseHTTPRequestHandler):
    """Stands in for the wrapped app — records inbound `Accept-Encoding`
    (test 4) and serves a handful of fixed responses the tests below
    check through the proxy."""

    accept_encoding_seen: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - stdlib override
        self.accept_encoding_seen.append(self.headers.get("Accept-Encoding", ""))

        if self.path == "/":
            body = _HTML
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/gzip-page":
            raw = _HTML
            if "gzip" in self.headers.get("Accept-Encoding", ""):
                body = gzip.compress(raw)
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        elif self.path == "/data.json":
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/bundle.js":
            # Stands in for a framework's real static JS/CSS bundle —
            # what the blanket-Accept-Encoding-stripping regression (see
            # test_pwa_does_not_defeat_compression_for_static_assets)
            # actually broke: forcing this uncompressed on every request
            # is exactly what turned into several-minutes-to-load over a
            # real tunnel on a phone.
            raw = b"console.log('x');" * 500
            if "gzip" in self.headers.get("Accept-Encoding", ""):
                body = gzip.compress(raw)
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript")
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        elif self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for i in range(3):
                self.wfile.write(f"data: {i}\n\n".encode())
                self.wfile.flush()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args: object) -> None:  # quiet test output
        pass


@pytest.fixture
def upstream() -> tuple[int, threading.Event]:
    _UpstreamHandler.accept_encoding_seen = []
    port = allocate_port()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _UpstreamHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port, threading.Event()
    server.shutdown()
    thread.join(timeout=5)


def _wait_ready(url: str, *, timeout: float = 10, headers: dict[str, str] | None = None) -> None:
    """Poll `url` until the upstream is actually up — a 502 means the
    proxy's own holding/error page, not the wrapped app; anything else
    (200, 401, ...) means the request reached real routing logic."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url, timeout=2, headers=headers)
            if resp.status_code != 502:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    raise TimeoutError(f"proxy at {url} never became ready")


def test_pwa_routes_and_injection_over_real_proxy(upstream: tuple[int, threading.Event]) -> None:
    upstream_port, _ = upstream
    listen_port = allocate_port()
    runtime = pwa.build_runtime(pwa.PwaOptions(), app_name="it-pwa", domain=None)
    handle = reverse_proxy.start_proxy(
        "it-pwa",
        listen_port=listen_port,
        upstream_port=upstream_port,
        auth="open",
        token=None,
        control_token=None,
        pwa=runtime,
    )
    base = f"http://127.0.0.1:{listen_port}"
    try:
        _wait_ready(base + "/")

        # test 1: injection correctness — manifest link present, exactly once
        page = httpx.get(base + "/", timeout=5)
        assert page.status_code == 200
        assert page.text.count('rel="manifest"') == 1
        assert "content-length" not in {k.lower() for k in page.headers}

        # synthetic routes serve sidepage's own content
        manifest = httpx.get(base + "/manifest.webmanifest", timeout=5)
        assert manifest.headers["content-type"].startswith("application/manifest+json")
        assert json.loads(manifest.text)["display"] == "standalone"

        sw = httpx.get(base + "/sw.js", timeout=5)
        assert sw.status_code == 200
        assert sw.headers["service-worker-allowed"] == "/"

        icon = httpx.get(base + "/icon-192.png", timeout=5)
        assert icon.headers["content-type"] == "image/png"

        offline = httpx.get(base + "/_sidepage/offline.html", timeout=5)
        assert offline.status_code == 200
        assert "session has ended" in offline.text

        # test 2: non-HTML passes through byte-identical
        data = httpx.get(base + "/data.json", timeout=5)
        assert data.json() == {"ok": True}
        assert data.headers["content-type"] == "application/json"

        # test 3: SSE not buffered/rewritten
        stream = httpx.get(base + "/stream", timeout=5)
        assert stream.headers["content-type"] == "text/event-stream"
        assert "data: 0" in stream.text and "serviceWorker" not in stream.text
    finally:
        reverse_proxy.stop_proxy(handle)


def test_pwa_injection_handles_gzip_compressed_html(
    upstream: tuple[int, threading.Event],
) -> None:
    """Spec §10 test 4 — a gzip-compressed upstream HTML response still
    gets correctly injected, not corrupted. Also proves the upstream
    genuinely received a real `gzip` Accept-Encoding (unlike an earlier
    version of this feature, which forced every request to `identity` —
    see the next test for the regression that caused)."""
    upstream_port, _ = upstream
    listen_port = allocate_port()
    runtime = pwa.build_runtime(pwa.PwaOptions(), app_name="it-pwa-gzip", domain=None)
    handle = reverse_proxy.start_proxy(
        "it-pwa-gzip",
        listen_port=listen_port,
        upstream_port=upstream_port,
        auth="open",
        token=None,
        control_token=None,
        pwa=runtime,
    )
    base = f"http://127.0.0.1:{listen_port}"
    try:
        _wait_ready(base + "/")
        resp = httpx.get(base + "/gzip-page", timeout=5, headers={"Accept-Encoding": "gzip"})
        assert resp.status_code == 200
        assert resp.text.count('rel="manifest"') == 1  # correctly injected, not corrupted
        assert "gzip" in _UpstreamHandler.accept_encoding_seen[-1]
    finally:
        reverse_proxy.stop_proxy(handle)


def test_pwa_does_not_defeat_compression_for_static_assets(
    upstream: tuple[int, threading.Event],
) -> None:
    """The regression this guards against: an earlier implementation
    stripped `Accept-Encoding` on *every* proxied request when `--pwa`
    was on, not just the HTML document it actually rewrites — which also
    forced a framework's real static JS/CSS bundles uncompressed. Free on
    loopback, but reproduced live as several minutes to load a real app
    over an actual BYO-domain tunnel on a phone. A non-HTML asset must
    still be served compressed, byte-for-byte, exactly as if `--pwa`
    weren't on at all.
    """
    upstream_port, _ = upstream
    listen_port = allocate_port()
    runtime = pwa.build_runtime(pwa.PwaOptions(), app_name="it-pwa-asset", domain=None)
    handle = reverse_proxy.start_proxy(
        "it-pwa-asset",
        listen_port=listen_port,
        upstream_port=upstream_port,
        auth="open",
        token=None,
        control_token=None,
        pwa=runtime,
    )
    base = f"http://127.0.0.1:{listen_port}"
    try:
        _wait_ready(base + "/")
        resp = httpx.get(base + "/bundle.js", timeout=5, headers={"Accept-Encoding": "gzip"})
        assert resp.status_code == 200
        assert resp.headers.get("content-encoding") == "gzip"
        assert "gzip" in _UpstreamHandler.accept_encoding_seen[-1]
    finally:
        reverse_proxy.stop_proxy(handle)


def test_pwa_route_precedence_over_static_site(tmp_path: Path) -> None:
    static_root = tmp_path / "site"
    static_root.mkdir()
    (static_root / "index.html").write_text(_HTML.decode())
    (static_root / "manifest.webmanifest").write_text('{"this": "is the apps own file"}')

    listen_port = allocate_port()
    runtime = pwa.build_runtime(pwa.PwaOptions(), app_name="it-pwa-static", domain=None)
    handle = reverse_proxy.start_proxy(
        "it-pwa-static",
        listen_port=listen_port,
        static_root=static_root,
        auth="open",
        token=None,
        control_token=None,
        pwa=runtime,
    )
    base = f"http://127.0.0.1:{listen_port}"
    try:
        resp = httpx.get(base + "/manifest.webmanifest", timeout=5)
        manifest = json.loads(resp.text)
        assert "icons" in manifest  # sidepage's generated manifest, not the site's own file
    finally:
        reverse_proxy.stop_proxy(handle)


def test_pwa_auth_token_gate_and_crossorigin(upstream: tuple[int, threading.Event]) -> None:
    upstream_port, _ = upstream
    listen_port = allocate_port()
    runtime = pwa.build_runtime(pwa.PwaOptions(), app_name="it-pwa-auth", domain=None)
    handle = reverse_proxy.start_proxy(
        "it-pwa-auth",
        listen_port=listen_port,
        upstream_port=upstream_port,
        auth="token",
        token="secret123",
        control_token=None,
        pwa=runtime,
    )
    base = f"http://127.0.0.1:{listen_port}"
    auth_headers = {"Authorization": "Bearer secret123"}
    try:
        # Waited for with the real token, not unauthenticated — an
        # unauthenticated request 401s immediately regardless of upstream
        # readiness (AuthGateMiddleware never reaches proxy_http at all),
        # so it can't be used as a readiness signal here.
        _wait_ready(base + "/", timeout=10, headers=auth_headers)

        no_cred = httpx.get(base + "/manifest.webmanifest", timeout=5)
        assert no_cred.status_code == 401

        with_cred = httpx.get(
            base + "/manifest.webmanifest", headers=auth_headers, timeout=5
        )
        assert with_cred.status_code == 200

        page = httpx.get(base + "/", headers=auth_headers, timeout=5)
        assert 'crossorigin="use-credentials"' in page.text
    finally:
        reverse_proxy.stop_proxy(handle)
