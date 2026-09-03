"""`--pwa` — makes any sidepage-served app installable to a phone home
screen, without touching the wrapped app itself. All work happens at the
reverse-proxy layer (`sidepage.core.reverse_proxy`): this module is the
pure, request-independent half — building the manifest/service-worker/
offline-page bytes once at `serve` startup and rewriting one HTML response
body — with no knowledge of Starlette, sockets, or the proxy's routing.

**Non-goal, load-bearing**: this makes an app *installable*, not
*usable*. No viewport meta, no CSS, no layout rewriting — a
non-mobile-friendly app stays exactly as (un)usable as it was.

**Relative `start_url`/`scope`, always `"./"`.** Sidepage can't know the
public hostname at manifest-generation time — with `--anon` the tunnel
hostname is only assigned *after* `cloudflared` reports it, well after
this module has already built and handed off the manifest bytes.
Relative URLs resolve correctly regardless of origin, so there's nothing
to template in and no ordering dependency to get wrong.

**Ephemeral session marker is generated locally**, not derived from the
real `*.trycloudflare.com` hostname, for the same reason: this module
runs before any tunnel exists. Reuses the 4-char alphabet
`sidepage.core.directory_client._ID_ALPHABET` already uses for its own
short suffixes (`secrets.choice` over lowercase+digits) — same shape, a
separate copy here since directory_client's is a name-binding concern,
not a PWA one.

**Manifest `id`, durable installs only.** Set to the literal `--domain`
value, exactly as specified — not the resolved `<app-name>-<id>.<domain>`
hostname multiple apps on one base domain would each get. Two apps on the
same `--domain` therefore share one manifest `id`; that's what was asked
for, recorded here rather than silently "fixed" into something else.

**`--pwa-manifest` short-circuits *manifest content* only** — name/
short_name/theme/bg/icon/display all stop mattering for the JSON that
gets served at `/manifest.webmanifest`. It does **not** short-circuit
`--pwa-force`/`--pwa-no-sw` (those control the injection *process*, §6/§7
of the spec, not manifest *content*, §4) or `--pwa-theme`'s use in the
injected `<meta name="theme-color">` tag (a separate injection step, not
part of the manifest object). This is this module's own reading of "serve
verbatim; ignore all other --pwa-* fields" — recorded here since the spec
text alone doesn't disambiguate "ignore for manifest-building" from
"ignore entirely."

**§8 path-scoping constraint** ("if sidepage ever multiplexes multiple
apps under one hostname by path prefix, `--pwa` must refuse") has no
runtime check here: sidepage has no such multiplexing today — every app
gets its own subdomain/tunnel — so there is no code path that could trip
it. Forward-looking per the spec, not implemented as dead validation
against a feature that doesn't exist yet.

Icon handling never adds an image-processing dependency (spec §5): custom
`--pwa-icon` dimensions are read straight out of the PNG's IHDR chunk
(bytes 16-24 — width/height, big-endian), and the same file's bytes are
served at both `/icon-192.png` and `/icon-512.png` (browsers scale down
fine). The bundled defaults are two real, correctly-sized PNGs shipped as
package data under `sidepage.assets`, generated once by a throwaway
stdlib-only PNG writer that isn't itself part of this package.
"""

from __future__ import annotations

import importlib.resources
import json
import re
import secrets
import string
import struct
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from sidepage.core.exceptions import PwaConfigError
from sidepage.output import info

_ID_ALPHABET = string.ascii_lowercase + string.digits  # matches directory_client's own


def _session_marker() -> str:
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(4))


class PwaDisplay(StrEnum):
    STANDALONE = "standalone"
    FULLSCREEN = "fullscreen"
    MINIMAL_UI = "minimal-ui"


@dataclass(frozen=True)
class PwaOptions:
    """Everything `--pwa-*` collects at the CLI layer — one field per
    flag, `None`/spec-default values exactly matching the spec's own
    per-flag defaults (`sidepage.commands.serve` supplies those; this
    dataclass just carries them through to `build_runtime`)."""

    name: str | None = None
    short_name: str | None = None
    theme: str = "#111111"
    bg: str = "#ffffff"
    icon: Path | None = None
    display: PwaDisplay = PwaDisplay.STANDALONE
    manifest: Path | None = None
    force: bool = False
    no_sw: bool = False


@dataclass
class PwaRuntime:
    """Everything the reverse proxy needs to serve PWA routes and rewrite
    HTML — built once at `serve` startup by `build_runtime`, held in
    memory, referenced (not copied) by every request. Deliberately
    **not** frozen, unlike `PwaOptions`: `offline_html` is rebuilt once
    the served URL is actually known (`finalize_offline_page` — happens
    *after* the tunnel opens, later than everything else here), and
    `warned_existing_manifest` is a print-once latch `inject_head_tags`
    sets so a page a user's app also hosts a manifest for doesn't spam
    one warning line per request.
    """

    manifest_bytes: bytes
    sw_js: str | None  # None means --pwa-no-sw
    icon_192: bytes
    icon_512: bytes
    offline_html: str
    theme_color: str
    force: bool
    warned_existing_manifest: bool = field(default=False)


_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _validate_hex_color(value: str, flag: str) -> None:
    if not _HEX_RE.match(value):
        raise PwaConfigError(f"{flag} must be a #rgb or #rrggbb hex color, got {value!r}")


_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _read_png_dimensions(data: bytes, *, source: str) -> tuple[int, int]:
    """Parse width/height straight out of the PNG IHDR chunk (bytes
    16-24, two big-endian uint32s) — no dependency, ~10 lines, per spec
    §5. Raises `PwaConfigError` naming the actual problem: not a PNG at
    all, or too short to even contain an IHDR chunk."""
    if not data.startswith(_PNG_MAGIC) or len(data) < 24:
        raise PwaConfigError(f"--pwa-icon {source} is not a valid PNG file")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _load_icons(icon_path: Path | None) -> tuple[bytes, bytes]:
    """Returns `(icon_192_bytes, icon_512_bytes)`. Bundled default: two
    real, distinctly-sized PNGs shipped as package data. Custom
    `--pwa-icon`: validated square PNG >=512px, same bytes served at both
    sizes (spec §5 — resizing needs a dependency this doesn't add)."""
    if icon_path is None:
        assets = importlib.resources.files("sidepage.assets")
        return (
            assets.joinpath("icon-192.png").read_bytes(),
            assets.joinpath("icon-512.png").read_bytes(),
        )

    try:
        data = icon_path.read_bytes()
    except OSError as exc:
        raise PwaConfigError(f"--pwa-icon {icon_path}: could not read file ({exc})") from exc

    width, height = _read_png_dimensions(data, source=str(icon_path))
    if width != height:
        raise PwaConfigError(f"--pwa-icon must be square, got {width}x{height} ({icon_path})")
    if width < 512:
        raise PwaConfigError(f"--pwa-icon must be >=512px, got {width}x{height} ({icon_path})")
    return data, data


def build_manifest_dict(options: PwaOptions, *, app_name: str, domain: str | None) -> dict:
    """The generated-manifest branch of spec §4 (the other branch,
    `--pwa-manifest`, never calls this — see `build_runtime`). `app_name`
    is the already-resolved served app name (`ServeConfig`/`RunningApp`'s
    own `name`, whatever `--name`/target-stem default it resolved to),
    used as `--pwa-name`'s default per spec §2.
    """
    name = options.name or app_name
    short_name = (options.short_name or name)[:12]
    if domain is None:
        # Ephemeral (no --domain, spec §4's "durable installs" branch not
        # taken): suffix so repeated installs are distinguishable on the
        # home screen. short_name stays unsuffixed, per spec.
        name = f"{name} ({_session_marker()})"

    manifest: dict = {
        "name": name,
        "short_name": short_name,
        "start_url": "./",
        "scope": "./",
        "display": options.display.value,
        "theme_color": options.theme,
        "background_color": options.bg,
        "icons": [
            {"src": "./icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "./icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {
                "src": "./icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
        ],
    }
    if domain is not None:
        manifest["id"] = domain
    return manifest


def _render_offline_html(app_name: str, served_url: str | None) -> str:
    """Spec §7's `/_sidepage/offline.html`: static, self-contained, no
    external assets, no retry loop, no auto-refresh. `served_url` is
    `None` until `finalize_offline_page` runs (the real URL isn't known
    at `build_runtime` time — see this module's docstring); the initial
    placeholder is never actually servable before that call happens, since
    it runs well before `serve` finishes starting the proxy.
    """
    import html as html_lib

    name = html_lib.escape(app_name)
    url = html_lib.escape(served_url or "")
    url_html = f"<p style=\"color:#666; word-break:break-all;\">{url}</p>" if served_url else ""
    return (
        "<!doctype html>\n"
        f"<html><head><meta charset=\"utf-8\"><title>{name} — session ended</title></head>\n"
        '<body style="font-family: system-ui; max-width: 420px; margin: 80px auto; '
        'text-align: center;">\n'
        f"<h2>{name}</h2>\n"
        "<p>This sidepage session has ended.</p>\n"
        f"{url_html}\n"
        "</body></html>"
    )


def finalize_offline_page(runtime: PwaRuntime, *, app_name: str, served_url: str) -> None:
    """Rebuilds `runtime.offline_html` with the real served URL, once it's
    actually known (after the tunnel opens, or the local URL if there's
    no tunnel) — called from `sidepage.core.process.serve` right after
    that URL is resolved. Mutates in place: the `/_sidepage/offline.html`
    route reads `runtime.offline_html` fresh on every request, so this
    update is visible immediately with no route re-registration needed.
    """
    runtime.offline_html = _render_offline_html(app_name, served_url)


_EPHEMERAL_SW = """\
const CACHE = 'sidepage-ephemeral-v1';
const OFFLINE_URL = '/_sidepage/offline.html';

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.add(OFFLINE_URL)));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

async function endSession() {
  const keys = await caches.keys();
  await Promise.all(keys.map((key) => caches.delete(key)));
  await self.registration.unregister();
}

// Network-first, minimal caching — a cached shell rendering happily while
// the tunnel is dead would be a zombie app, worse than a broken one. Only
// navigation requests are intercepted at all; everything else goes
// straight to the network untouched.
self.addEventListener('fetch', (event) => {
  if (event.request.mode !== 'navigate') {
    return;
  }
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        if (response.status === 404 || response.status === 503) {
          // Tunnel edge is saying this session is gone: show the offline
          // page and make sure a dead icon never renders a stale cached
          // shell again.
          event.waitUntil(endSession());
          return caches.match(OFFLINE_URL);
        }
        return response;
      })
      .catch(() => caches.match(OFFLINE_URL))
  );
});
"""

_DURABLE_SW = """\
const CACHE = 'sidepage-durable-v1';
const OFFLINE_URL = '/_sidepage/offline.html';

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.add(OFFLINE_URL)));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys.filter((key) => key.startsWith('sidepage-') && key !== CACHE)
            .map((key) => caches.delete(key))
        )
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  if (event.request.mode === 'navigate') {
    event.respondWith(fetch(event.request).catch(() => caches.match(OFFLINE_URL)));
    return;
  }
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin) {
    return;
  }
  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) {
        return cached;
      }
      return fetch(event.request).then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(event.request, copy));
        }
        return response;
      });
    })
  );
});
"""


def _build_service_worker(*, ephemeral: bool) -> str:
    return _EPHEMERAL_SW if ephemeral else _DURABLE_SW


_MANIFEST_LINK_RE = re.compile(rb'rel=["\']manifest["\']', re.IGNORECASE)
_HEAD_TAG_RE = re.compile(rb"<head[^>]*>", re.IGNORECASE)


def has_head_tag(body: bytes) -> bool:
    """Spec §6's own injection gate — callers check this (and status/
    content-type) *before* buffering a response at all, so a non-HTML or
    headless-fragment body is never touched."""
    return _HEAD_TAG_RE.search(body) is not None


def inject_head_tags(body: bytes, runtime: PwaRuntime) -> bytes:
    """Spec §6 step 3: insert the manifest link / meta tags / SW
    registration script immediately after the first `<head...>` tag.
    Callers (`sidepage.core.reverse_proxy`'s injection middleware) are
    expected to have already confirmed `has_head_tag(body)` — this
    returns `body` unchanged if that's not actually true, as a defensive
    fallback rather than raising.

    `crossorigin="use-credentials"` is set unconditionally on the
    manifest `<link>`, even in `--auth open` — harmless there, and
    required whenever auth isn't open (spec §6: an anonymous manifest
    fetch 401s through a token gate with nothing useful in the console
    otherwise).
    """
    match = _HEAD_TAG_RE.search(body)
    if match is None:
        return body

    already_has_manifest = _MANIFEST_LINK_RE.search(body) is not None
    skip_manifest_link = already_has_manifest and not runtime.force
    if skip_manifest_link and not runtime.warned_existing_manifest:
        runtime.warned_existing_manifest = True
        info("pwa: app ships its own manifest, not injecting (--pwa-force to override)")

    lines: list[str] = []
    if not skip_manifest_link:
        lines.append(
            '<link rel="manifest" href="/manifest.webmanifest" crossorigin="use-credentials">'
        )
    lines += [
        f'<meta name="theme-color" content="{runtime.theme_color}">',
        '<meta name="mobile-web-app-capable" content="yes">',
        '<meta name="apple-mobile-web-app-capable" content="yes">',
        '<meta name="apple-mobile-web-app-status-bar-style" content="default">',
        '<link rel="apple-touch-icon" href="/icon-192.png">',
    ]
    if runtime.sw_js is not None:
        lines.append(
            "<script>\n"
            "if ('serviceWorker' in navigator) {\n"
            "  window.addEventListener('load', function () {\n"
            "    navigator.serviceWorker.register('/sw.js', { scope: '/' });\n"
            "  });\n"
            "}\n"
            "</script>"
        )

    injected = ("\n" + "\n".join(lines) + "\n").encode()
    insert_at = match.end()
    return body[:insert_at] + injected + body[insert_at:]


def build_runtime(options: PwaOptions, *, app_name: str, domain: str | None) -> PwaRuntime:
    """The "generated once at startup, held in memory" step (spec §4) —
    called from `sidepage.core.process.serve` right after target/app-name
    resolution, before any port is allocated or subprocess spawned, so a
    bad `--pwa-icon`/`--pwa-manifest`/hex color fails loud immediately
    (same "fail before touching anything" posture `_validate_supported`
    already has for its own checks).

    `domain` is `config.domain` exactly as `serve` resolved it — `None`
    selects the ephemeral manifest-naming/service-worker profile,
    otherwise the durable one. Independent of `--anon`: what matters here
    is whether a stable BYO hostname exists, not which tunnel mode is
    running.
    """
    _validate_hex_color(options.theme, "--pwa-theme")
    _validate_hex_color(options.bg, "--pwa-bg")

    icon_192, icon_512 = _load_icons(options.icon)

    if options.manifest is not None:
        try:
            manifest_bytes = options.manifest.read_bytes()
        except OSError as exc:
            raise PwaConfigError(
                f"--pwa-manifest {options.manifest}: could not read file ({exc})"
            ) from exc
        try:
            json.loads(manifest_bytes)
        except json.JSONDecodeError as exc:
            raise PwaConfigError(
                f"--pwa-manifest {options.manifest}: not valid JSON ({exc})"
            ) from exc
    else:
        manifest_bytes = json.dumps(
            build_manifest_dict(options, app_name=app_name, domain=domain), indent=2
        ).encode()

    sw_js = None if options.no_sw else _build_service_worker(ephemeral=domain is None)

    return PwaRuntime(
        manifest_bytes=manifest_bytes,
        sw_js=sw_js,
        icon_192=icon_192,
        icon_512=icon_512,
        offline_html=_render_offline_html(options.name or app_name, served_url=None),
        theme_color=options.theme,
        force=options.force,
    )
