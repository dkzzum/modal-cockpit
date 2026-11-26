# Agent instructions: use Modal through modal-cockpit

These instructions are for coding agents working on behalf of a user who manages
multiple Modal accounts with `modal-cockpit`. Copy this file into the root of any
repository where an agent should use Modal, or merge the relevant sections into
that repository's existing `AGENTS.md`.

## Core rule

For every account-bound Modal CLI operation, prefer `modal-cockpit exec` over
running bare `modal`:

```bash
modal-cockpit exec ACCOUNT -- modal app list
```

`modal-cockpit exec` pins the correct Modal profile and config path for that one
subprocess. This avoids accidental use of the wrong account when several agents,
terminals or jobs are active at the same time.

Do not run account-bound commands as bare `modal ...` unless the user explicitly
asks for that. Generic help/version inspection such as `modal --help` may be run
directly because it does not depend on the selected account.

## Discovering accounts

Before choosing an account, inspect the managed account list:

```bash
modal-cockpit list --json
modal-cockpit current --json
modal-cockpit doctor --strict
```

Use the exact account alias from `modal-cockpit list`; do not guess aliases. If
no suitable managed account exists, stop and ask the user which account to use or
ask them to configure one. Do not create, capture or login an account on your own
initiative.

If `modal-cockpit` is not on `PATH`, do not assume that bare `modal` is an
acceptable fallback. Ask the user how it is installed, or use the installation
method they approve. In a source checkout it may be available as
`.venv/bin/modal-cockpit` or as `python -m modal_cockpit` with the checkout on
`PYTHONPATH`.

## Command pattern

Examples:

```bash
modal-cockpit exec ACCOUNT -- modal app list
modal-cockpit exec ACCOUNT -- modal volume list
modal-cockpit exec ACCOUNT -- modal deploy path/to/app.py
modal-cockpit exec ACCOUNT -- modal run path/to/job.py
```

For shell pipelines or redirection, use an explicit shell inside `exec` and be
careful with quoting:

```bash
modal-cockpit exec ACCOUNT -- sh -c 'modal app list | head'
```

Prefer several short `exec` commands over changing global account state. This is
especially important when multiple agents or user terminals may be working in
parallel.

## Do not manage credentials manually

Never print, copy, commit or paste Modal tokens. Treat strings matching `ak-*`
and `as-*` as secrets and redact them from logs and bug reports.

Do not manually edit `~/.modal.toml` or files under `~/.modal-cockpit`.

Do not set credential overrides yourself:

- `MODAL_TOKEN_ID`
- `MODAL_TOKEN_SECRET`
- Modal OAuth token environment variables

Do not pass `--keep-env` to `modal-cockpit exec` unless the user explicitly asks
for it. The default behavior intentionally strips credential overrides plus
transport overrides (`MODAL_ENVIRONMENT`, `MODAL_SERVER_URL`, `MODAL_STREAMS_URL`,
`MODAL_OVERRIDE_HEADERS`, `MODAL_DISABLE_API_PROXY`, `MODAL_IS_REMOTE`) and
terminal-forcing variables that can corrupt machine-readable output. Use explicit
Modal command options or profile settings instead of relying on inherited values.
Do not use a login shell such as `sh -lc` inside `exec`; shell startup files can
reintroduce the overrides that modal-cockpit just removed.

Do not set `MODAL_PROFILE` or `MODAL_CONFIG_PATH` manually for normal Modal
operations; `modal-cockpit exec` sets the correct values for the subprocess.

## Global switching

Avoid global switching. Use per-command isolation instead:

```bash
modal-cockpit exec ACCOUNT -- modal app list
```

Use `modal-cockpit switch ACCOUNT` only when the user explicitly asks to change
the persistent active account. If a task genuinely requires a temporary global
switch, first record the current account, perform the minimum necessary work,
restore the original account in a finally-style cleanup step, and verify with
`modal-cockpit current --json`.

Never use global switching as a convenience substitute for `exec` in automated
agent workflows.

## Volume transfer safety

`modal-cockpit transfer` copies Modal Volume data between accounts through local
temporary staging. It is potentially expensive, slow and destructive to the
destination when overwrite is enabled. Treat it as a production data migration,
not as a routine test command.

Before running a transfer, all of the following must be explicit in the current
user request or confirmation:

1. source account alias;
2. destination account alias;
3. volume name, or explicit approval for `--all-volumes`;
4. destination volume name if it differs;
5. whether overwrite/merge with `--force` is allowed;
6. confirmation that network transfer, local disk use and possible Modal costs
   are acceptable.

Default to verification and a non-colliding destination name when possible:

```bash
modal-cockpit transfer SOURCE DESTINATION VOLUME \
  --dest-volume rescued-VOLUME \
  --verify \
  --yes
```

Use `--all-volumes` only when the user explicitly asks to copy every volume:

```bash
modal-cockpit transfer SOURCE DESTINATION --all-volumes --verify --yes
```

Do not use `--force` unless the user explicitly authorizes overwriting or
merging destination files in the current conversation. If a transfer fails
because the destination is non-empty, do not retry with `--force` on your own;
report the collision and ask what to do.

A transfer is copy-first, not mirror and not deletion:

- source volumes are never modified or deleted by the command;
- destination-only files remain after a `--force` merge;
- an interrupted upload can leave a partial destination volume;
- `--verify` compares copied files by re-downloading the destination, but it
  does not independently prove the original source download and does not roll
  back failures;
- modal-cockpit inventories the volume recursively before download and refuses
  symlinks, FIFOs, sockets, unsafe path components and local filename collisions;
  only regular files and directories are accepted;
- empty directories, timestamps and executable bits are not guaranteed to be
  preserved;
- inventory is not a snapshot: writers must be stopped because entries can still
  change between inventory, download, upload and verification.

Never delete the source volume or otherwise "clean up" the source account after
a transfer. Even after successful verification, deletion is a separate
destructive operation requiring explicit user approval.

Large volumes temporarily consume local disk space. If the user expects a large
transfer, ask where temporary staging should live or use a user-approved path:

```bash
modal-cockpit transfer SOURCE DESTINATION VOLUME \
  --staging-dir /path/to/large/disk \
  --verify \
  --yes
```

## Account setup and authentication

If authentication or account setup is required, prefer asking the user to run
the interactive setup command themselves. Do not ask the user to paste token
values into chat. Suitable user-side commands are:

```bash
modal-cockpit login ACCOUNT --activate
modal-cockpit add ACCOUNT
modal-cockpit capture ACCOUNT
```

After setup, verify state without printing secrets:

```bash
modal-cockpit list
modal-cockpit doctor --strict
```

## Health checks and troubleshooting

When Modal behavior looks wrong, check the account selection before retrying:

```bash
modal-cockpit current --json
modal-cockpit list --json
modal-cockpit doctor --strict
```

If a command fails, do not retry repeatedly in a loop. Inspect the error, verify
that the correct account was used, and report the failing command with secrets
redacted. Do not weaken safety flags, bypass confirmations or change permissions
unless the user asks for that specific remediation.

## Output hygiene

`modal-cockpit list`, `current` and `doctor` are designed not to print tokens.
External Modal commands may still include sensitive values in errors or object
metadata. Review output before forwarding it, redact credentials and never paste
full config files into chat, commits, tests or issues.
