# Running someone else's app (`sidepage pull`)

[← back to README](../../README.md)

Fetch a Hugging Face Space, resolve how it would run, and register it —
without executing any of it:

```bash
sidepage pull huggingface.co/spaces/Anvarbekkk/real-time-stock-predictor
```

```
  pulled  real-time-stock-predictor
  source  huggingface.co/spaces/Anvarbekkk/real-time-stock-predictor
  commit  458cee6
  sdk     gradio 5.29.0
  entry   app.py
  deps    requirements.txt (79 packages, not yet installed)
  size    675.6 KB

  nothing has been executed. review the code, then:
    sidepage serve real-time-stock-predictor
```

`hf:<owner>/<name>` works as a shorthand for the full URL.

## Flags

- `--dry-run` prints the same plan — including the total download size and
  the declared dependency file — having fetched nothing at all. This is
  the mode to use before pulling a Space that carries tens of gigabytes of
  model weights.
- `--json` emits the plan as one line, for agents.
- `--as <name>` chooses the registered name; `--force` replaces an
  existing one.
- `--ignore-hardware` overrides the GPU-tier warning (see below).

## What it refuses, and what it only warns about

`pull` decides what it will fetch *before* downloading anything.

**Refused outright:** Docker Spaces and private/gated repos — the first
needs a container runtime sidepage doesn't have, the second needs Hub
credentials it can't supply. These errors are final, not something to
retry differently.

**A warning with an override:** a GPU or ZeroGPU hardware tier. The tier
is the owner's Hugging Face hosting choice, not a property of the code,
and a ZeroGPU app's `@spaces.GPU` decorator is inert off Hugging Face — so
the Space usually does run locally, just on CPU, possibly slowly. A Space
sized for an A100 may exhaust memory. `--ignore-hardware` pulls it anyway
and keeps saying so in the plan.

## What `pull` does and doesn't do

Files are fetched over plain HTTPS at a pinned commit — no `git` or
`git-lfs` needed — and every LFS file is checked against the digest the
Hub declared beforehand.

**Nothing is installed and nothing is executed.** Dependencies resolve on
the first `serve`, which for a heavy app is genuinely slow the first time.
Environment variables the Space requests are reported by name as
`(requested — not granted)` and bound only when you pass `--env`; sidepage
never auto-grants a vault secret because a downloaded manifest asked for
it.

## The confirmation gate

`serve` runs code sidepage downloaded only after you approve the exact
commit:

```
  about to run code sidepage downloaded
  source   huggingface.co/spaces/Anvarbekkk/real-time-stock-predictor
  commit   458cee6
  sdk      gradio 5.29.0
  entry    .../apps/real-time-stock-predictor/app.py

  run it? [y/N]:
```

Approval is recorded against that commit, so a later `pull` that brings
down different code asks again.

With no terminal to prompt at — an agent, a CI job, a piped command —
`serve` **refuses outright** rather than assuming yes.
`--trust-remote-code` is the explicit waiver, and it is meant to be typed
by a person who has read the code, not passed by an agent to get past the
prompt.

This is the same warning Hugging Face puts in front of running a Space
locally, and it applies here on a machine that also holds your secrets
vault.

## Removing a pulled app

```bash
sidepage app delete real-time-stock-predictor   # removes the source too
```

`delete` removes the downloaded source tree; `unregister` only forgets the
saved config. An app registered against a path you already had has no
downloaded tree, and `delete` will never remove your own files.
