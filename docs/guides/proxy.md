# Proxying an already-running service

[← back to README](../../README.md)

`sidepage proxy` wraps a service you already have running — `npm run
dev`, a container, anything already listening on a port — with the same
reverse proxy, auth, and tunnel stack `serve` uses, minus one thing:
sidepage never launches, owns, or manages the process's lifecycle.

```bash
sidepage proxy --port <n> [--name <app-name>] [--domain <domain> | --anon]
               [--auth open|token] [--token <value>]
               [--timeout <seconds>] [--idle-timeout <seconds>]
```

- `--port` is the only required flag — always dialed on `127.0.0.1`, with
  an automatic fallback to `[::1]` (IPv6 loopback) if that doesn't
  answer, since `proxy` can't control how the wrapped service was bound
  the way `serve` can for its own launchers.
- `--name` defaults to `proxy-<port>` for plain local use; it's required
  (and rejected loud if missing) once `--domain`/`--anon` is set, since it
  becomes part of the public hostname there.
- `--type`, `--env`, `--guardrail`, `--peer` aren't accepted at all — each
  gives a specific, actionable error instead of being silently ignored,
  since they're all about a subprocess `proxy` doesn't own.

**The one behavior that's genuinely different from `serve`:** Ctrl+C /
`sidepage stop <name>` tear down the proxy, the tunnel, and the registry
entry only. The service you pointed `--port` at was never sidepage's to
stop, and it doesn't.

**Read `sidepage proxy --help` before pointing this at anything public** —
it documents, loudly, three things worth knowing up front:
- Every proxied request reaches the wrapped app from `127.0.0.1`
  (sidepage's own address) — any app-level logic that trusts "this came
  from localhost" instead of checking `X-Forwarded-For` (debug endpoints,
  admin panels, and pointedly Flask/Werkzeug's interactive debugger — a
  known RCE if reachable) is silently defeated, `--auth` or not.
- The real `Host`/`X-Forwarded-Host`/`X-Forwarded-Proto`/`X-Forwarded-For`
  are forwarded on HTTP requests (WebSocket connections carry
  `X-Forwarded-Host` only, not a literal `Host` override — some WS
  servers, Jupyter/Tornado confirmed live, reject a forwarded real
  hostname on the handshake) — but that only helps an app that's
  configured to trust them. `--help` has a one-line fix per framework
  (Django, Flask, FastAPI/Starlette, Express, Rails, Vite).
- OAuth/SSO logins are effectively incompatible with `--anon`, since the
  hostname changes every run and providers require an exact,
  pre-registered redirect URI — use `--domain` for anything doing OAuth.

```bash
# Already running: npm run dev -- --host 127.0.0.1 --port 5173
sidepage proxy --port 5173                        # local only
sidepage proxy --port 5173 --domain example.com    # your own domain
sidepage proxy --port 5173 --anon                  # *.trycloudflare.com
```

## Known limitation: Vite HMR over `--anon`

HMR/live-reload for a Vite dev server proxied through `--anon` doesn't
reliably work, even though the initial page load and BYO-domain are both
unaffected — ruled out sidepage's own header forwarding/routing as the
cause (the exact browser handshake, reproduced with `curl`, succeeds
through the real Cloudflare edge + `cloudflared` + sidepage + Vite chain
end to end); the gap is somewhere in how a real browser's `WebSocket`
negotiates against Cloudflare's Quick Tunnel edge specifically, not
isolated further. Investigated, not fixed — see
[`docs/CHECKLIST.md`](../CHECKLIST.md).
