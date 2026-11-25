from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .core import CREDENTIAL_ENV_VARS, ModalCockpit
from .errors import CockpitError
from .paths import coerce_path, default_modal_config_path, default_store_home


def build_cockpit(args: argparse.Namespace) -> ModalCockpit:
    store_home = coerce_path(getattr(args, "home", None), default_store_home())
    config_path = coerce_path(getattr(args, "config_path", None), default_modal_config_path())
    return ModalCockpit(store_home=store_home, config_path=config_path)


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def read_secret(value: str | None, prompt: str) -> str:
    if value == "-":
        return sys.stdin.readline().rstrip("\n")
    if value is not None:
        return value
    return getpass.getpass(prompt)


def confirm_or_fail(action: str, yes: bool, subject: str) -> None:
    if yes:
        return
    if not sys.stdin.isatty():
        raise CockpitError(f"{action} требует подтверждения; повторите с --yes")
    answer = input(f"{action}: {subject}. Введите 'yes' для подтверждения: ")
    if answer != "yes":
        raise CockpitError("операция отменена")


def cmd_init(args: argparse.Namespace) -> int:
    cockpit = build_cockpit(args)
    cockpit.store.ensure_layout()
    print(f"store: {cockpit.store.root}")
    print(f"modal config: {cockpit.config_path}")
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    cockpit = build_cockpit(args)
    meta = cockpit.capture(args.alias, profile=args.profile, force=args.force)
    print(f"captured: {args.alias or meta['modal_profile']} (profile: {meta['modal_profile']}, auth: {meta['auth_type']})")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    cockpit = build_cockpit(args)
    token_id = read_secret(args.token_id, "Token ID: ")
    token_secret = read_secret(args.token_secret, "Token secret: ")
    meta = cockpit.add_token_account(
        args.alias,
        profile=args.profile,
        token_id=token_id,
        token_secret=token_secret,
        activate=args.activate,
    )
    state = "active" if meta.get("active") else "inactive"
    print(f"added: {args.alias} (profile: {meta['modal_profile']}, {state})")
    if meta.get("backup_id"):
        print(f"backup: {meta['backup_id']}")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    cockpit = build_cockpit(args)
    meta = cockpit.login_with_modal_cli(
        args.alias,
        profile=args.profile,
        activate=args.activate,
        expires_in=args.expires_in,
    )
    print(f"login captured: {args.alias} (profile: {meta['modal_profile']})")
    if args.activate:
        print(f"active profile: {meta['modal_profile']}")
    return 0


def verify_after_switch(cockpit: ModalCockpit, alias: str) -> int:
    modal = shutil.which("modal")
    if not modal:
        print("verify: Modal CLI не найден, проверка пропущена", file=sys.stderr)
        return 1
    env = cockpit.build_exec_environment(alias)
    try:
        completed = subprocess.run(
            [modal, "token", "info"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print("verify: modal token info не ответил за 20 секунд", file=sys.stderr)
        return 1
    if completed.returncode == 0:
        print("verify: modal token info OK")
        return 0
    print(f"verify: modal token info завершился с кодом {completed.returncode}", file=sys.stderr)
    return completed.returncode or 1


def cmd_switch(args: argparse.Namespace) -> int:
    cockpit = build_cockpit(args)
    result = cockpit.switch(args.alias, force_snapshot=args.force_snapshot)
    if result.changed:
        print(f"active profile: {result.modal_profile} (account: {result.alias})")
    else:
        print(f"already active: {result.modal_profile} (account: {result.alias})")
    if result.restored_missing_profile:
        print("profile was missing in Modal config and restored from encrypted snapshot")
    if result.backup_id:
        print(f"backup: {result.backup_id}")
    if os.environ.get("MODAL_PROFILE"):
        print(
            f"warning: MODAL_PROFILE={os.environ['MODAL_PROFILE']!r} overrides the switched file profile in this shell",
            file=sys.stderr,
        )
    if args.verify:
        return verify_after_switch(cockpit, args.alias)
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    cockpit = build_cockpit(args)
    synced = cockpit.sync()
    print("synced accounts: " + (", ".join(synced) if synced else "(none)"))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    cockpit = build_cockpit(args)
    data = cockpit.list_accounts()
    if args.json:
        print_json(data)
        return 0
    if data["active_error"]:
        print(f"active-error: {data['active_error']}")
    print(f"config: {data['config_path']}")
    print(f"store:  {data['store_home']}")
    if not data["accounts"]:
        print("accounts: (none)")
        return 0
    print("accounts:")
    for item in data["accounts"]:
        marker = "*" if item["active"] else " "
        missing = " missing-in-config" if not item["present_in_config"] else ""
        fp = item.get("token_id_sha256") or ""
        fp_text = f" token-sha256:{fp[:12]}…" if fp else ""
        print(
            f" {marker} {item['alias']}  profile={item['modal_profile']}  "
            f"auth={item['auth_type']}{fp_text}{missing}"
        )
    return 0


def cmd_current(args: argparse.Namespace) -> int:
    cockpit = build_cockpit(args)
    data = cockpit.current_status()
    if args.json:
        print_json(data)
        return 0
    if data["active_profile"]:
        account = f" (account: {data['account_alias']})" if data["account_alias"] else ""
        print(f"active profile: {data['active_profile']}{account}")
    else:
        print("active profile: (none)")
    if data["env_profile_override"]:
        print(
            f"note: MODAL_PROFILE={data['env_profile_override']!r} overrides file profile {data['file_active_profile']!r}",
            file=sys.stderr,
        )
    if data["env_credentials_override"]:
        print("warning: MODAL_* credential environment variables override the config", file=sys.stderr)
    return 0


def cmd_exec(args: argparse.Namespace) -> int:
    cockpit = build_cockpit(args)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise CockpitError("usage: modal-cockpit exec ACCOUNT -- COMMAND [ARGS...]")
    return cockpit.exec(args.alias, command, keep_credential_env=args.keep_env)


def shell_quote(value: str) -> str:
    return shlex.quote(value)


def cmd_env(args: argparse.Namespace) -> int:
    cockpit = build_cockpit(args)
    cockpit.ensure_profile_present(args.alias)
    env = cockpit.build_exec_environment(args.alias, keep_credential_env=args.keep_env)
    print(f"export MODAL_PROFILE={shell_quote(env['MODAL_PROFILE'])}")
    print(f"export MODAL_CONFIG_PATH={shell_quote(env['MODAL_CONFIG_PATH'])}")
    if not args.keep_env:
        for name in CREDENTIAL_ENV_VARS:
            print(f"unset {name}")
    return 0


def format_byte_count(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} B"
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"


def cmd_transfer(args: argparse.Namespace) -> int:
    cockpit = build_cockpit(args)
    if args.all_volumes and args.volume:
        raise CockpitError("укажите либо конкретный volume, либо --all-volumes")
    if not args.all_volumes and not args.volume:
        raise CockpitError("укажите volume или --all-volumes")
    if args.all_volumes and args.destination_volume:
        raise CockpitError("--dest-volume можно использовать только при переносе одного volume")

    target = "all volumes" if args.all_volumes else f"volume {args.volume}"
    action = "copy Modal volume data into another account"
    if args.force:
        action += " and overwrite existing destination files"
    confirm_or_fail(
        action,
        args.yes,
        f"{args.source_account} -> {args.destination_account}, {target}",
    )
    staging_parent = Path(args.staging_dir).expanduser() if args.staging_dir else None
    if args.volume:
        results = [
            cockpit.transfer_volume(
                args.source_account,
                args.destination_account,
                args.volume,
                destination_volume=args.destination_volume,
                force=args.force,
                staging_parent=staging_parent,
                source_environment=args.source_env,
                destination_environment=args.dest_env,
                verify=args.verify,
            )
        ]
    else:
        results = cockpit.transfer_volumes(
            args.source_account,
            args.destination_account,
            force=args.force,
            staging_parent=staging_parent,
            source_environment=args.source_env,
            destination_environment=args.dest_env,
            verify=args.verify,
        )
    total_files = sum(item["files"] for item in results)
    total_bytes = sum(item["bytes"] for item in results)
    if args.json:
        print_json(
            {
                "source_account": args.source_account,
                "destination_account": args.destination_account,
                "volumes": results,
                "total_volumes": len(results),
                "total_files": total_files,
                "total_bytes": total_bytes,
            }
        )
        return 0
    if not results:
        print("no volumes found in source account")
        return 0
    for item in results:
        created = ", created destination volume" if item["created_volume"] else ""
        verified = ", verified" if item.get("verified") else ""
        print(
            f"transferred: {item['source_volume']} -> {item['destination_volume']} "
            f"({item['files']} files, {format_byte_count(item['bytes'])}{created}{verified})"
        )
    print(f"transfer complete: {len(results)} volumes, {total_files} files, {format_byte_count(total_bytes)}")
    print("source volumes were not modified or deleted")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    cockpit = build_cockpit(args)
    backup_id = cockpit.backup_now(reason=args.reason)
    print(f"backup: {backup_id}")
    return 0


def cmd_backups(args: argparse.Namespace) -> int:
    cockpit = build_cockpit(args)
    backups = cockpit.store.list_backups()
    if args.json:
        print_json(backups)
        return 0
    if not backups:
        print("backups: (none)")
        return 0
    print("backups:")
    for item in backups:
        if "error" in item:
            print(f"  {item['id']}  ERROR: {item['error']}")
        else:
            print(f"  {item['id']}  {item.get('created_at')}  {item.get('reason')}")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    cockpit = build_cockpit(args)
    confirm_or_fail("restore encrypted backup over Modal config", args.yes, args.backup_id)
    pre = cockpit.restore_backup(args.backup_id)
    print(f"restored: {args.backup_id}")
    if pre:
        print(f"pre-restore backup: {pre}")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    cockpit = build_cockpit(args)
    action = "remove account snapshot" + (" and Modal profile" if args.delete_profile else "")
    confirm_or_fail(action, args.yes, args.alias)
    result = cockpit.remove_account(args.alias, delete_profile=args.delete_profile)
    print(f"removed account: {result['alias']} (profile: {result['modal_profile']})")
    if result["removed_profile"]:
        print("profile removed from Modal config")
    if result.get("backup_id"):
        print(f"backup: {result['backup_id']}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    cockpit = build_cockpit(args)
    checks = cockpit.doctor(fix_permissions=args.fix_permissions)
    worst = 0
    for item in checks:
        level = item["level"].upper()
        print(f"{level:5} {item['name']}: {item['message']}")
        if item["level"] == "error":
            worst = 2
        elif item["level"] == "warn" and worst == 0:
            worst = 1
    return worst if args.strict else 0


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="modal-cockpit",
        description="Cockpit-style multi-account switcher for Modal CLI",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--home", help="override modal-cockpit store directory (default: ~/.modal-cockpit)")
    parser.add_argument("--config-path", help="override Modal config path (default: MODAL_CONFIG_PATH or ~/.modal.toml)")
    sub = parser.add_subparsers(dest="command_name", required=True)

    p = sub.add_parser("init", help="create store directories and show paths")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("capture", help="capture an existing Modal profile into the encrypted account store")
    p.add_argument("alias", nargs="?", help="local account alias (default: profile name)")
    p.add_argument("--profile", help="Modal profile to capture (default: active profile)")
    p.add_argument("--force", action="store_true", help="rebind an existing alias to another Modal profile")
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("add", help="add token credentials without printing or storing them in plaintext")
    p.add_argument("alias")
    p.add_argument("--profile", help="Modal profile name (default: alias)")
    p.add_argument("--token-id", help="token id; use '-' for stdin, omit for hidden prompt")
    p.add_argument("--token-secret", help="token secret; use '-' for stdin, omit for hidden prompt")
    p.add_argument("--activate", action="store_true", help="activate the profile after writing")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("login", help="run modal token new for a named profile, then capture it")
    p.add_argument("alias")
    p.add_argument("--profile", help="Modal profile name (default: alias)")
    p.add_argument("--activate", action="store_true", help="switch to the account after successful login")
    p.add_argument("--expires-in", help="token lifetime passed to modal token new, e.g. 12h/7d/90d")
    p.set_defaults(func=cmd_login)

    for name in ("switch", "use"):
        p = sub.add_parser(name, help="switch the active Modal profile in one action")
        p.add_argument("alias")
        p.add_argument("--verify", action="store_true", help="run modal token info after switching")
        p.add_argument(
            "--force-snapshot",
            action="store_true",
            help="overwrite the live profile section from the encrypted snapshot before activating it",
        )
        p.set_defaults(func=cmd_switch)

    p = sub.add_parser("sync", help="refresh encrypted snapshots from live managed profiles")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("list", help="list managed accounts without secrets")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("current", help="show the active Modal profile")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_current)

    p = sub.add_parser("exec", help="run a command as an account without globally switching")
    p.add_argument("alias")
    p.add_argument("--keep-env", action="store_true", help="keep MODAL_TOKEN_ID/MODAL_TOKEN_SECRET/OAuth env overrides")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_exec)

    p = sub.add_parser("env", help="print shell commands that select an account in the current shell")
    p.add_argument("alias")
    p.add_argument("--keep-env", action="store_true", help="do not emit unset commands for credential overrides")
    p.set_defaults(func=cmd_env)

    p = sub.add_parser("transfer", help="copy Modal volumes between managed accounts through local staging")
    p.add_argument("source_account", help="managed source account alias")
    p.add_argument("destination_account", help="managed destination account alias")
    p.add_argument("volume", nargs="?", help="volume to copy; omit when using --all-volumes")
    p.add_argument("--all-volumes", action="store_true", help="copy every volume visible in the source account")
    p.add_argument("--dest-volume", "--destination-volume", dest="destination_volume", help="destination volume name")
    p.add_argument("--force", action="store_true", help="overwrite existing destination files")
    p.add_argument("--yes", action="store_true", help="confirm remote writes without an interactive prompt")
    p.add_argument("--json", action="store_true", help="print machine-readable transfer summary")
    p.add_argument("--verify", action="store_true", help="re-download destination and compare SHA-256 for copied files")
    p.add_argument("--staging-dir", help="directory used as the parent for temporary local transfer data")
    p.add_argument("--source-env", help="Modal environment in the source account")
    p.add_argument("--dest-env", "--destination-env", dest="dest_env", help="Modal environment in the destination account")
    p.set_defaults(func=cmd_transfer)

    p = sub.add_parser("backup", help="create an encrypted backup of the whole Modal config")
    p.add_argument("--reason", default="manual", help="backup reason label")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("backups", help="list encrypted config backups")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_backups)

    p = sub.add_parser("restore", help="restore an encrypted whole-config backup")
    p.add_argument("backup_id")
    p.add_argument("--yes", action="store_true", help="confirm overwrite")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("remove", help="remove a managed account snapshot")
    p.add_argument("alias")
    p.add_argument("--delete-profile", action="store_true", help="also remove the inactive profile from Modal config")
    p.add_argument("--yes", action="store_true", help="confirm deletion")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("doctor", help="validate config/store state and permissions")
    p.add_argument("--fix-permissions", action="store_true", help="chmod Modal config to 0600 if needed")
    p.add_argument("--strict", action="store_true", help="return non-zero on warnings/errors")
    p.set_defaults(func=cmd_doctor)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except CockpitError as exc:
        print(f"modal-cockpit: error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("modal-cockpit: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
