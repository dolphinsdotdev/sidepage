# Saved apps (the local registry)

[← back to README](../../README.md)

Save a `serve` invocation under a short name and re-run it without
retyping flags:

```bash
sidepage app register "abc.py --auth token" abc-app
sidepage serve abc-app
```

Any flag passed at `serve` time overrides the registered one **for that
one run only** — the saved registration itself is never changed:

```bash
sidepage serve abc-app --scope web   # runs with --auth token (registered)
                                      # but --scope web for just this run
```

`sidepage app show abc-app` prints the saved config; add `--with "<flags>"`
to preview the effective merged config before actually running it, e.g.
`sidepage app show abc-app --with "--scope web"`.

A registered app's target is resolved once, at registration time — so
`--type` is stored as a concrete value (`code`, `static`, `notebook`),
never "auto." `sidepage app register` **refuses** a literal `--token
<value>`: auth tokens are per-process and regenerate on every `serve`
call, so storing one would defeat the point of them being ephemeral.
`--env <SECRET_NAME>` is fine to save — it's a reference to a vault entry,
never the secret value itself.

```bash
sidepage app list
sidepage app unregister abc-app
```

## Registering as you serve

`--autoregister` saves the invocation you're already running, so you don't
have to write it out a second time as an `app register` string:

```bash
sidepage serve abc.py --auth token --autoregister
sidepage serve abc                                  # replays it
```

The entry is written **after** the app is actually serving — a config that
fails to start is never saved. Three things can happen when the name is
already taken:

| Situation | What happens |
|---|---|
| Nothing registered under that name | Saved once the app is up |
| Same config already registered | Nothing written; a warning tells you `sidepage serve <app-name>` is all you need next time |
| A *different* config registered | Refused before anything starts — `app show` to compare, `app unregister` to replace, or `--name` to save under a different name |

Unlike `app register`, `--autoregister` doesn't refuse an invocation
carrying a `--token`; it saves everything else and tells you what it
dropped. The same applies to every other per-invocation flag, so you're
never left believing a saved config is more complete than it is:

```
warning --autoregister won't save --timeout, --qr — `sidepage serve dash`
        won't replay them, pass them on the command line again.
```

## What's stored, and what isn't

| Flag | Stored? | Why |
|---|---|---|
| `--type`, `--name`, `--domain`, `--no-suffix`, `--auth`, `--scope`, `--anon`, `--env` | Yes | Describe what the app is and how it's reached |
| `--pwa`, `--pwa-*` | Yes | An installed app's name, icon, and theme are part of its identity — a saved config that dropped them wouldn't reproduce the app it was saved from |
| `--timeout`, `--idle-timeout`, `--peer`, `--qr` | No | Describe how *one run* behaves, not what the app is |
| `--token` | Never | Process-scoped secret, reissued every run |

`--pwa*` merges **as one unit**, not field by field: if a later `serve
<app-name>` passes any `--pwa*` flag, that invocation's whole PWA config
wins; pass none and the saved one applies unchanged.

```bash
sidepage app register "abc.py --pwa --pwa-name Dashboard --pwa-theme '#111111'" dash
sidepage serve dash                              # Dashboard, #111111
sidepage serve dash --pwa --pwa-theme '#999999'  # #999999, and the saved name is NOT inherited
```

That's deliberate: a field-by-field merge would mean `--pwa-theme` alone
silently picking up a saved `--pwa-icon` and `--pwa-manifest`, and would
leave no way to say "PWA on, but none of the saved settings."
`--pwa-icon`/`--pwa-manifest` paths are stored absolute, so a saved app
serves the same icon no matter which directory you run it from.
