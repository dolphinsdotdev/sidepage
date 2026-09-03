# Bring your own domain

[← back to README](../../README.md)

Route apps through your own Cloudflare domain instead of
`*.trycloudflare.com`. One-time setup:

1. Create a Cloudflare API token (dashboard → My Profile → API Tokens)
   scoped to:
   - Account → Cloudflare Tunnel → Edit
   - Zone → DNS → Edit
   - Zone → Zone → Read
2. Store it in the vault, then provision the domain:
   ```bash
   sidepage secrets set cf-api-token
   sidepage account domain set example.com --api-token-name cf-api-token
   ```
   This creates one Cloudflare Tunnel for the whole domain and stores its
   run-token in the vault automatically — the CLI prints the vault name it
   landed under (`cf-tunnel-token::example.com`), since it was never typed
   by you.
3. Serve apps through it:
   ```bash
   sidepage serve app.py --domain example.com
   ```

Every app served under the same domain shares that one tunnel — no new
Cloudflare resources or tokens per app. The shared `cloudflared` process
starts with the first app on a domain and stops with the last.

## Hostnames, and `--no-suffix`

By default an app is routed at `<app-name>-<id>.example.com`, where
`<id>` is a random 4-char suffix assigned once per app name and kept in
`name_bindings.json` — so the URL is stable across restarts, and two
apps that happen to pick the same name can't collide.

On a domain you own, that guarantee may be worth less than a clean name.
`--no-suffix` drops the id:

```bash
sidepage serve ./docs-site --domain example.com --name docs --no-suffix
# https://docs.example.com
```

What you're trading away: not much, because the name is checked before
it's claimed. Serving fails loud rather than quietly repointing a
hostname that's already spoken for:

```
an app with this name already exists: docs.example.com is taken — an existing
CNAME record points it at cname.vercel-dns.com, which isn't this domain's
sidepage tunnel. Serving here would silently repoint it, so sidepage won't.
To continue: pick a different --name, drop --no-suffix for the dedupe-suffixed
hostname instead, or delete the CNAME record for docs.example.com in the
Cloudflare dashboard if it's stale.
```

The zone's own DNS is the authority — there's no directory service to
ask. A record on the name that isn't a CNAME to this domain's sidepage
tunnel means someone else owns it: another tunnel, an A record, a page
parked on a different host. A CNAME to *your* tunnel is your own app
restarting, which is fine and stays idempotent. The check runs both as a
pre-flight (before `serve` allocates a port or launches anything) and
again at claim time, and it applies to suffixed hostnames too — a stale
`<app-name>-<id>` record is refused on exactly the same test.

**What it can't see**: a second machine set up with the *same* domain
config (same tunnel token, copied by hand). Its records point at the same
tunnel, so they're indistinguishable from this machine's own, and
`sidepage.core.registry` is per-machine. Two machines racing to claim one
name at the same instant aren't serialized either — `_domain_lock` is
per-machine. Configuring the domain normally on a second machine
(`account domain set`) provisions its own tunnel, so that case *is*
caught.

Otherwise `--no-suffix` changes nothing: auth, timeouts, PWA, and
teardown behave identically, and the two forms coexist on one domain —
some apps suffixed, some not.

An unsuffixed app never consumes an id, so dropping `--no-suffix` later
gives it the same `<app-name>-<id>` it would have had all along.

`--no-suffix` requires `--domain` and is rejected otherwise: a local
serve has no hostname to shorten, and `--anon`'s
`*.trycloudflare.com` name is Cloudflare's to assign. It's stored by
`sidepage app register` / `--autoregister` (like `--domain` itself),
since it decides the URL a saved config replays at.
