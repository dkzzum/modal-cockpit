# Security policy

`modal-cockpit` handles authentication tokens. Please do not open public issues
that contain token values, encrypted account payloads, encryption keys, or full
`~/.modal.toml` files.

## Reporting a vulnerability

After this repository is published on GitHub, report vulnerabilities through
GitHub Security Advisories for the repository. Do not include real credentials
in the report. Redact all `ak-*` and `as-*` values before sharing logs.

If private reporting is not available yet, open a minimal public issue saying
that you found a security issue, without details or secrets, and ask the
maintainer to provide a private contact channel.

## Threat model

The encrypted store protects against accidental disclosure when backups or
account files are copied elsewhere. It does not protect against malicious
software running as the same OS user: the local encryption key is stored under
`~/.modal-cockpit`, and Modal itself requires plaintext credentials in
`~/.modal.toml` while they are in use.

The tool is designed to avoid printing secrets, use restrictive permissions,
serialize writes, create encrypted backups, and write configuration files
atomically.

Volume transfer writes copied data to a temporary plaintext local staging
directory. The directory is removed after a normal exit, but plaintext remnants
can remain after SIGKILL, power loss or forced termination. Choose `--staging-dir`
carefully and remove stale `volume-transfer-*`/`volume-verify-*` directories if
a transfer is interrupted.
