# PWA install and QR codes

[← back to README](../../README.md)

**`--pwa`** makes any served app installable to a phone home screen —
sidepage's reverse proxy synthesizes a web app manifest, a service
worker, and the `<link>`/`<meta>` tags an app needs, injected into the
HTML response on the fly. The wrapped app is never touched or modified on
disk; this makes it *installable*, not mobile-friendly — no viewport
meta, no CSS, no layout changes.

```bash
# Quickest path to an icon on a phone
sidepage serve app.py --anon --pwa --qr

# Durable install — stable across restarts, gets a real id in the manifest
sidepage serve app.py --domain example.com \
  --pwa --pwa-name "Sales Dashboard" --pwa-icon ./icon.png --pwa-theme "#0b3d2e"

# App already ships its own manifest — sidepage defers to it by default
sidepage serve ./dist --domain example.com --pwa --pwa-manifest ./public/manifest.json
```

- `--pwa-name` / `--pwa-short-name` — defaults to the resolved app name
  (truncated to 12 chars for the short form). With `--anon`, `name` gets a
  short session-marker suffix so repeat installs don't collide on the home
  screen; `short_name` doesn't.
- `--pwa-theme` / `--pwa-bg` — hex colors (`#rgb` or `#rrggbb`) for the
  status bar / splash background. Default `#111111` / `#ffffff`.
- `--pwa-icon <path>` — a square PNG, ≥512px; validated up front with a
  clear error naming the actual dimensions found if it isn't. Omit it for
  a bundled default icon.
- `--pwa-display standalone|fullscreen|minimal-ui` — manifest display mode.
- `--pwa-manifest <path>` — serve this file verbatim instead of generating
  one; every other `--pwa-*` field is ignored for the manifest itself
  (still honored for the injected `<meta>` tag and service worker).
- `--pwa-force` — inject sidepage's manifest link even if the app already
  has its own `rel="manifest"` (default: defer to the app's, but still
  inject theme-color/service-worker).
- `--pwa-no-sw` — manifest only, no service worker.

**Ephemeral (`--anon`) vs. durable (`--domain`) installs matter here**:
an `--anon` icon breaks the moment that session's tunnel URL stops
existing — sidepage says so in the startup output — while a `--domain`
install is stable across restarts and gets a real `id` in its manifest so
reinstalling replaces the old icon instead of adding a second one.

**`--qr`** prints a terminal QR code for whatever URL `serve` ends up
with (the tunnel URL if one was opened, otherwise the local one) —
independent of `--pwa`, useful any time there's a URL worth scanning onto
a phone.
