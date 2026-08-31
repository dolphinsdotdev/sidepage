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
