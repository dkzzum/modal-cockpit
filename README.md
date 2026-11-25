# modal-cockpit

`modal-cockpit` is a local command-line tool for safely managing and switching multiple [Modal](https://modal.com/) CLI accounts.

It uses Modal's native multi-profile configuration and adds one-command switching, encrypted account snapshots, encrypted whole-config backups, atomic writes, cross-process locking, profile restoration, and isolated execution under different accounts.

Русская версия документации: [README.ru.md](README.ru.md).

> **Scope:** `modal-cockpit` manages Modal CLI/API credentials in `~/.modal.toml`. It does **not** manage browser cookies or dashboard sessions for modal.com.

## Why not just `modal profile activate`?

Modal already supports multiple profiles in one TOML file. `modal-cockpit` keeps that native model instead of replacing it, and adds the missing operational safety layer:

- encrypted snapshot for every managed profile;
- encrypted backup of the whole `~/.modal.toml` before each mutation;
- restoration of a missing profile section from its encrypted snapshot;
- synchronization of rotated or changed credentials from the live config before switching;
- atomic writes through temporary-file + rename rather than direct truncation;
- `0600` permissions for secret-bearing files and `0700` for store directories;
- detection of external file changes during a write operation;
- `exec`/`env` helpers for parallel work under different accounts without globally switching;
- `doctor` diagnostics for broken TOML, multiple active profiles, unsafe permissions, and environment overrides.

By default, the live `~/.modal.toml` is considered newer than the encrypted snapshot. This prevents a token rotation from being silently rolled back during switching. If a live profile section is damaged and you intentionally want to restore the saved snapshot, use `switch --force-snapshot`.

## Installation

Requirements:

- Python 3.11 or newer;
- Modal CLI for browser login and `switch --verify` (the core switcher itself can write the config without Modal CLI).

From a source checkout:

```bash
git clone https://github.com/dkzzum/modal-cockpit.git
cd modal-cockpit
python3 -m venv .venv
.venv/bin/pip install -e .
```

Then run:

```bash
.venv/bin/modal-cockpit --help
```

Alternatively, install the checkout with `pipx`:

```bash
pipx install .
modal-cockpit --help
```

## Quick start

### 1. Capture an existing Modal profile

If you have already authenticated with `modal token new` or `modal token set`:

```bash
modal-cockpit capture personal
```

If no alias is supplied, the active Modal profile name is used:

```bash
modal-cockpit capture
```

Capture a specific non-active profile:

```bash
modal-cockpit capture work --profile my-workspace
```

### 2. Add another account through Modal browser login

```bash
modal-cockpit login work --activate
```

This runs `modal token new --no-activate` inside a temporary isolated config copy, then atomically merges the resulting profile into the real `~/.modal.toml`. The real config is not directly exposed to the non-atomic write performed during interactive login.

A token TTL can be forwarded to Modal:

```bash
modal-cockpit login work --expires-in 7d --activate
```

### 3. Add token credentials manually

Hidden interactive input:

```bash
modal-cockpit add work
```

Or through stdin without putting secret literals into shell arguments:

```bash
printf '%s\n%s\n' "$MODAL_TOKEN_ID" "$MODAL_TOKEN_SECRET" \
  | modal-cockpit add work --token-id - --token-secret -
```

Passing secrets directly as `--token-id/--token-secret` arguments is supported but discouraged because they may appear in shell history or process listings.

### 4. Switch in one action

```bash
modal-cockpit switch work
# or:
modal-cockpit use work
```

Internally, switching performs the following steps:

1. acquires the cross-process store lock;
2. synchronizes encrypted snapshots from live managed profiles;
3. creates an encrypted backup of the current Modal config;
4. restores the target profile section if it is missing;
5. makes the target profile the only active profile;
6. verifies that the file was not externally modified during the operation;
7. atomically replaces the config and updates account metadata.

Check the result:

```bash
modal-cockpit current
modal-cockpit list
```

Optionally verify the active token through Modal:

```bash
modal-cockpit switch work --verify
```

### 5. Run commands in parallel without switching globally

`exec` sets `MODAL_PROFILE` only for one child process:

```bash
modal-cockpit exec personal -- modal app list
modal-cockpit exec work -- modal volume ls
```

This allows commands for different workspaces/accounts to run at the same time without changing the globally active profile.

For the current shell:

```bash
eval "$(modal-cockpit env work)"
```

Return to the file-backed active profile:

```bash
unset MODAL_PROFILE
```

`MODAL_PROFILE` takes precedence over `active = true` in the TOML file. `modal-cockpit current` and `doctor` explicitly show this override.

## Backups and recovery

Create a manual encrypted backup:

```bash
modal-cockpit backup --reason before-rotation
```

List backups:

```bash
modal-cockpit backups
```

Restore the whole Modal config:

```bash
modal-cockpit restore <backup-id> --yes
```

A fresh backup of the current state is created before restore.

Backups contain the Modal config, but restore intentionally does not roll back the encrypted account snapshots in `accounts/`. This avoids losing newer credentials that may have been captured after the backup was created.

If a managed profile section was deleted from the config, a normal `switch` restores it from the encrypted snapshot. If the section exists but is damaged or stale and you intentionally want the snapshot to win:

```bash
modal-cockpit switch work --force-snapshot
```

This is destructive for the current live section, so an encrypted backup is always created first.

## Diagnostics

```bash
modal-cockpit doctor
```

Strict mode returns a non-zero exit code on warnings or errors:

```bash
modal-cockpit doctor --strict
```

Fix permissions of the Modal config to `0600`:

```bash
modal-cockpit doctor --fix-permissions
```

`doctor` checks:

- whether `modal` is available in `PATH`;
- whether the TOML config exists and parses;
- whether exactly one profile is active;
- config file permissions;
- readability of encrypted account snapshots;
- `MODAL_PROFILE` and credential environment overrides.

## Command reference

| Command | Purpose |
|---|---|
| `init` | create store directories and show paths |
| `capture [alias]` | capture an existing profile from `~/.modal.toml` |
| `login <alias>` | browser-login through `modal token new`, then capture/merge |
| `add <alias>` | add token credentials without plaintext account storage |
| `switch/use <alias>` | switch the active Modal profile in one action |
| `sync` | refresh encrypted snapshots from live managed profiles |
| `list` | list accounts without printing secrets |
| `current` | show the active profile, including `MODAL_PROFILE` override |
| `exec <alias> -- cmd ...` | run one command as an account without global switching |
| `env <alias>` | print shell commands for `eval` |
| `backup` / `backups` / `restore` | manage encrypted whole-config backups |
| `remove` | remove an account snapshot; optionally remove an inactive profile |
| `doctor` | validate configuration, store, permissions, and env overrides |

Global options are placed before the command:

```bash
modal-cockpit --home ~/.modal-cockpit --config-path ~/.modal.toml list
```

Environment overrides:

- `MODAL_COCKPIT_HOME` — modal-cockpit store directory;
- `MODAL_CONFIG_PATH` — Modal config path, same as in Modal itself.

## Storage and security model

Default store layout:

```text
~/.modal-cockpit/
├── accounts/              # encrypted profile snapshots
├── backups/               # encrypted whole-config backups
├── accounts-index.json    # metadata only, no token values
├── encryption.key         # local AES key, mode 0600
└── modal-cockpit.lock
```

Encryption format: AES-256-GCM envelope encryption. Account snapshots contain the complete TOML profile section, including token or OAuth fields and non-secret profile settings such as `server_url`, `loglevel`, or `logs_timeout`.

Important limitations:

- Modal requires plaintext credentials in `~/.modal.toml`, so the active working config cannot be fully encrypted at rest;
- `encryption.key` is stored locally next to the encrypted data. This protects against accidental copying or backup leakage, but not against malicious software running as the same OS user;
- the CLI never intentionally prints token values in `list`, `current`, or `switch` output;
- shell history and process listings can expose secrets if they are passed directly as command-line arguments;
- the lock serializes `modal-cockpit` operations, but cannot prevent Modal CLI or another process from writing the file. The additional external-change check exists for that reason.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check modal_cockpit tests
```

The test suite does not use real `~/.modal.toml` files or real tokens. Browser-login integration is tested with a fake `modal` executable.

## License

This project is distributed under the [PolyForm Noncommercial License 1.0.0](LICENSE).

That means the source is available and may be used, changed, and distributed for noncommercial purposes. Because it restricts commercial use, it is a source-available noncommercial license rather than an OSI-approved open-source license.
