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
