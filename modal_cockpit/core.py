from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit

from .atomic import ensure_private_dir, exclusive_lock, write_text_atomic
from .errors import CockpitError, ConfigError, ExternalCommandError, StoreError
from .store import Store, validate_name
from .toml_config import (
    LoadedModalConfig,
    active_profiles,
    detect_active_profile,
    ensure_no_multiple_active,
    load_modal_config,
    remove_profile_section,
    render_toml,
    section_without_active,
    set_only_active_profile,
    upsert_profile_section,
    validate_rendered_config,
)

CREDENTIAL_ENV_VARS = (
    "MODAL_TOKEN_ID",
    "MODAL_TOKEN_SECRET",
    "MODAL_OAUTH_REFRESH_TOKEN",
    "MODAL_OAUTH_CLIENT_ID",
    "MODAL_OAUTH_CLIENT_SECRET",
    "MODAL_OAUTH_JWT_KEY",
)
TRANSPORT_ENV_VARS = (
    "MODAL_ENVIRONMENT",
    "MODAL_SERVER_URL",
    "MODAL_STREAMS_URL",
    "MODAL_OVERRIDE_HEADERS",
    "MODAL_DISABLE_API_PROXY",
    "MODAL_IS_REMOTE",
)
TERMINAL_ENV_VARS = (
    "FORCE_COLOR",
    "FORCE_TERMINAL",
    "TTY_COMPATIBLE",
)
OAUTH_CONFIG_KEYS = (
    "oauth_refresh_token",
    "oauth_client_id",
    "oauth_client_secret",
    "oauth_jwt_key",
)
TOKEN_CONFIG_KEYS = ("token_id", "token_secret")
AUTH_CONFIG_KEYS = TOKEN_CONFIG_KEYS + OAUTH_CONFIG_KEYS


@dataclass
class SwitchResult:
    alias: str
    modal_profile: str
    backup_id: str | None
    changed: bool
    restored_missing_profile: bool
    synced_accounts: list[str]


class ModalCockpit:
    def __init__(self, store_home: Path, config_path: Path):
        self.store = Store(store_home)
        self.config_path = config_path.resolve()

    def _load_config(self) -> LoadedModalConfig:
        return load_modal_config(self.config_path)

    def _backup_if_exists(self, cfg: LoadedModalConfig, reason: str) -> str | None:
        if not cfg.exists:
            return None
        return self.store.create_backup(self.config_path, cfg.text, reason=reason)

    def _write_config(self, text: str) -> None:
        write_text_atomic(self.config_path, text, mode=0o600)

    def _write_config_transaction(self, original: LoadedModalConfig, text: str) -> None:
        current = self._load_config()
        if current.exists != original.exists or current.text != original.text:
            raise ConfigError(
                f"{self.config_path} изменился во время операции; запись отменена, encrypted backup сохранён"
            )
        self._write_config(text)

    def _sync_managed_from_data_locked(
        self,
        data: dict[str, dict[str, Any]],
        *,
        exclude_alias: str | None = None,
    ) -> list[str]:
        synced: list[str] = []
        index = self.store.load_index()
        if index.get("restore_protection"):
            return []
        for alias, meta in index["accounts"].items():
            if alias == exclude_alias:
                continue
            profile = meta.get("modal_profile")
            if isinstance(profile, str) and profile in data:
                live = section_without_active(data[profile])
                try:
                    self._validate_auth_section(live)
                except StoreError:
                    continue
                self.store.save_account(alias, profile, live)
                synced.append(alias)
        return synced

    def sync(self) -> list[str]:
        """Refresh encrypted snapshots for managed profiles currently present in Modal config."""

        with self.store.lock():
            index = self.store.load_index()
            index.pop("restore_protection", None)
            self.store.save_index(index)
            cfg = self._load_config()
            return self._sync_managed_from_data_locked(cfg.data)

    def capture(self, alias: str | None = None, *, profile: str | None = None, force: bool = False) -> dict[str, Any]:
        with self.store.lock():
            cfg = self._load_config()
            if profile is None:
                profile = os.environ.get("MODAL_PROFILE") or detect_active_profile(cfg.data)
                if profile is None:
                    raise ConfigError("в Modal config нет активного профиля; укажите --profile явно")
            profile = validate_name(profile, label="имя Modal profile")
            if profile not in cfg.data:
                raise ConfigError(f"профиль {profile!r} отсутствует в {self.config_path}")
            alias = validate_name(alias or profile, label="имя аккаунта")
            existing = self.store.load_index()["accounts"].get(alias)
            if existing and existing.get("modal_profile") != profile and not force:
                raise StoreError(
                    f"аккаунт {alias!r} уже привязан к профилю {existing.get('modal_profile')!r}; "
                    "используйте --force для перепривязки"
                )
            return self.store.save_account(alias, profile, section_without_active(cfg.data[profile]))

    @staticmethod
    def _validate_auth_section(section: dict[str, Any]) -> None:
        token_present = any(key in section for key in TOKEN_CONFIG_KEYS)
        oauth_present = any(key in section for key in OAUTH_CONFIG_KEYS)
        if token_present and oauth_present:
            raise StoreError("Modal не допускает одновременную token и OAuth авторизацию в одном профиле")
        if token_present and not all(isinstance(section.get(key), str) and section.get(key) for key in TOKEN_CONFIG_KEYS):
            raise StoreError("token_id и token_secret должны быть непустыми и заданы вместе")
        if oauth_present:
            required = ("oauth_refresh_token", "oauth_client_id")
            if not all(isinstance(section.get(key), str) and section.get(key) for key in required):
                raise StoreError("OAuth profile должен содержать oauth_refresh_token и oauth_client_id")
            if not (section.get("oauth_client_secret") or section.get("oauth_jwt_key")):
                raise StoreError("OAuth profile должен содержать oauth_client_secret или oauth_jwt_key")

    def _merge_authenticated_section(
        self,
        existing_section: dict[str, Any],
        incoming_section: dict[str, Any],
    ) -> dict[str, Any]:
        merged = section_without_active(existing_section)
        if any(incoming_section.get(key) for key in AUTH_CONFIG_KEYS):
            for key in AUTH_CONFIG_KEYS:
                merged.pop(key, None)
        merged.update(section_without_active(incoming_section))
        return merged

    def import_profile_section(
        self,
        alias: str,
        *,
        profile: str | None,
        section: dict[str, Any],
        activate: bool = False,
        reason: str,
    ) -> dict[str, Any]:
        alias = validate_name(alias, label="имя аккаунта")
        profile = validate_name(profile or alias, label="имя Modal profile")
        if not isinstance(section, dict) or not section:
            raise StoreError("импортируемая секция профиля пуста или повреждена")
        self._validate_auth_section(section)
        with self.store.lock():
            existing_meta = self.store.load_index()["accounts"].get(alias)
            if existing_meta and existing_meta.get("modal_profile") != profile:
                raise StoreError(
                    f"аккаунт {alias!r} уже привязан к профилю {existing_meta.get('modal_profile')!r}"
                )
            self.store._validate_unique_profile(alias, profile)
            self.store.cipher.validate_or_create_key()
            if self.store.account_path(alias).exists():
                self.store.load_account(alias)
            cfg = self._load_config()
            ensure_no_multiple_active(cfg.data)
            backup_id = self._backup_if_exists(cfg, reason)

            existing_section = copy.deepcopy(cfg.data.get(profile, {}))
            was_active = detect_active_profile(cfg.data) == profile
            merged = self._merge_authenticated_section(existing_section, section)

            upsert_profile_section(cfg.doc, profile, merged, replace=True)
            make_active = activate or was_active or not cfg.data
            if make_active:
                set_only_active_profile(cfg.doc, profile)
            new_text = render_toml(cfg.doc)
            new_data = validate_rendered_config(new_text, expected_active=profile if make_active else None)
            self._write_config_transaction(cfg, new_text)

            saved = self.store.save_account(alias, profile, section_without_active(new_data[profile]), mark_switched=make_active)
            if make_active:
                self.store.set_current_account(alias)
            saved["backup_id"] = backup_id
            saved["active"] = make_active
            return saved

    def add_token_account(
        self,
        alias: str,
        *,
        profile: str | None,
        token_id: str,
        token_secret: str,
        activate: bool = False,
    ) -> dict[str, Any]:
        if not token_id or not token_secret:
            raise StoreError("token_id и token_secret не должны быть пустыми")
        return self.import_profile_section(
            alias,
            profile=profile,
            section={"token_id": token_id, "token_secret": token_secret},
            activate=activate,
            reason=f"before-add-{alias}",
        )

    def switch(self, alias: str, *, force_snapshot: bool = False) -> SwitchResult:
        alias = validate_name(alias, label="имя аккаунта")
        with self.store.lock():
            account = self.store.load_account(alias)
            modal_profile = account["modal_profile"]
            cfg = self._load_config()
            backup_id = self._backup_if_exists(cfg, f"before-switch-{alias}")

            synced = self._sync_managed_from_data_locked(cfg.data, exclude_alias=alias if force_snapshot else None)
            account = self.store.load_account(alias)
            protected = self.store.load_index().get("restore_protection")
            restored_missing = modal_profile not in cfg.data
            if restored_missing or force_snapshot or protected:
                upsert_profile_section(cfg.doc, modal_profile, account["section"], replace=True)

            set_only_active_profile(cfg.doc, modal_profile)
            new_text = render_toml(cfg.doc)
            new_data = validate_rendered_config(new_text, expected_active=modal_profile)
            changed = new_text != cfg.text
            if changed:
                self._write_config_transaction(cfg, new_text)

            self.store.save_account(
                alias,
                modal_profile,
                section_without_active(new_data[modal_profile]),
                mark_switched=True,
            )
            self.store.set_current_account(alias)
            return SwitchResult(
                alias=alias,
                modal_profile=modal_profile,
                backup_id=backup_id,
                changed=changed,
                restored_missing_profile=restored_missing,
                synced_accounts=synced,
            )

    def ensure_profile_present(self, alias: str) -> bool:
        """Ensure the managed profile exists in Modal config without changing active profile."""

        alias = validate_name(alias, label="имя аккаунта")
        with self.store.lock():
            account = self.store.load_account(alias)
            modal_profile = account["modal_profile"]
            cfg = self._load_config()
            ensure_no_multiple_active(cfg.data)
            protected = self.store.load_index().get("restore_protection")
            if modal_profile in cfg.data and not protected:
                self.store.save_account(alias, modal_profile, section_without_active(cfg.data[modal_profile]))
                return False
            if modal_profile in cfg.data and section_without_active(cfg.data[modal_profile]) == account["section"]:
                return False
            self._backup_if_exists(cfg, f"before-restore-profile-{alias}")
            upsert_profile_section(cfg.doc, modal_profile, account["section"], replace=True)
            new_text = render_toml(cfg.doc)
            validate_rendered_config(new_text)
            self._write_config_transaction(cfg, new_text)
            return True

    def backup_now(self, *, reason: str = "manual") -> str:
        with self.store.lock():
            cfg = self._load_config()
            if not cfg.exists:
                raise ConfigError(f"Modal config {self.config_path} не существует")
            backup_id = self._backup_if_exists(cfg, reason)
            assert backup_id is not None
            return backup_id

    def restore_backup(self, backup_id: str) -> str | None:
        payload = self.store.read_backup(backup_id)
        with self.store.lock():
            cfg = self._load_config()
            pre_restore_backup = self._backup_if_exists(cfg, "before-restore")
            text = payload["text"]
            if "source_path" in payload and Path(payload["source_path"]).resolve() != self.config_path:
                raise ConfigError(f"backup создан для другого Modal config: {payload['source_path']}")
            validate_rendered_config(text, allow_multiple_active=True)
            self._write_config_transaction(cfg, text)
            index = self.store.load_index()
            index["restore_protection"] = True
            self.store.save_index(index)
            return pre_restore_backup

    def remove_account(self, alias: str, *, delete_profile: bool = False) -> dict[str, Any]:
        alias = validate_name(alias, label="имя аккаунта")
        with self.store.lock():
            metadata = self.store.load_index()["accounts"].get(alias)
            if metadata is None:
                raise StoreError(f"аккаунт {alias!r} не найден в modal-cockpit")
            modal_profile = metadata.get("modal_profile")
            if not isinstance(modal_profile, str):
                raise StoreError(f"metadata аккаунта {alias!r} повреждена")
            backup_id: str | None = None
            removed_profile = False
            if delete_profile:
                cfg = self._load_config()
                ensure_no_multiple_active(cfg.data)
                active = detect_active_profile(cfg.data)
                if active == modal_profile:
                    raise ConfigError(
                        f"нельзя удалить активный Modal profile {modal_profile!r}; "
                        "сначала переключитесь на другой аккаунт"
                    )
                backup_id = self._backup_if_exists(cfg, f"before-remove-profile-{alias}")
                removed_profile = remove_profile_section(cfg.doc, modal_profile)
                new_text = render_toml(cfg.doc)
                validate_rendered_config(new_text)
                self._write_config_transaction(cfg, new_text)
            self.store.delete_account(alias)
            return {"alias": alias, "modal_profile": modal_profile, "removed_profile": removed_profile, "backup_id": backup_id}

    def list_accounts(self) -> dict[str, Any]:
        active_error: str | None = None
        try:
            cfg = self._load_config()
            config_data = cfg.data
        except ConfigError as exc:
            config_data = {}
            file_active = None
            active_error = str(exc)
        else:
            try:
                file_active = detect_active_profile(config_data)
            except ConfigError as exc:
                file_active = None
                active_error = str(exc)
        env_override = os.environ.get("MODAL_PROFILE") or None
        active = env_override or file_active
        accounts = []
        index = self.store.load_index()
        for alias, meta in sorted(index["accounts"].items()):
            profile = meta.get("modal_profile")
            accounts.append(
                {
                    "alias": alias,
                    "modal_profile": profile,
                    "active": profile == active,
                    "present_in_config": profile in config_data if isinstance(profile, str) else False,
                    "auth_type": meta.get("auth_type"),
                    "token_id_sha256": meta.get("token_id_sha256"),
                    "created_at": meta.get("created_at"),
                    "updated_at": meta.get("updated_at"),
                    "last_switched_at": meta.get("last_switched_at"),
                }
            )
        return {
            "config_path": str(self.config_path),
            "store_home": str(self.store.root),
            "active_profile": active,
            "file_active_profile": file_active,
            "env_profile_override": env_override,
            "active_error": active_error,
            "current_account": index.get("current_account"),
            "accounts": accounts,
        }

    def current_status(self) -> dict[str, Any]:
        cfg = self._load_config()
        file_active = detect_active_profile(cfg.data)
        env_override = os.environ.get("MODAL_PROFILE") or None
        active = env_override or file_active
        alias = self.store.find_alias_by_profile(active) if active else None
        return {
            "config_path": str(self.config_path),
            "active_profile": active,
            "file_active_profile": file_active,
            "env_profile_override": env_override,
            "account_alias": alias,
            "profiles": sorted(cfg.data),
            "env_credentials_override": any(os.environ.get(name) for name in CREDENTIAL_ENV_VARS),
        }

    def build_exec_environment(self, alias: str, *, keep_credential_env: bool = False) -> dict[str, str]:
        account = self.store.load_account(alias)
        env = os.environ.copy()
        env["MODAL_PROFILE"] = account["modal_profile"]
        env["MODAL_CONFIG_PATH"] = str(self.config_path)
        for name in TRANSPORT_ENV_VARS + TERMINAL_ENV_VARS:
            env.pop(name, None)
        if not keep_credential_env:
            for name in CREDENTIAL_ENV_VARS:
                env.pop(name, None)
        return env

    def exec(self, alias: str, command: Sequence[str], *, keep_credential_env: bool = False) -> int:
        if not command:
            raise CockpitError("exec требует команду после имени аккаунта")
        self.ensure_profile_present(alias)
        env = self.build_exec_environment(alias, keep_credential_env=keep_credential_env)
        try:
            completed = subprocess.run(list(command), env=env, check=False)
        except OSError as exc:
            raise ExternalCommandError(f"не удалось запустить команду {command[0]!r}: {exc}") from exc
        if completed.returncode < 0:
            return 128 + abs(completed.returncode)
        return completed.returncode

    @staticmethod
    def _redact_modal_output(text: str) -> str:
        return re.sub(r"\b(?:ak|as)-[A-Za-z0-9_-]+\b", "[redacted]", text)

    @staticmethod
    def _environment_args(environment: str | None) -> list[str]:
        return ["-e", environment] if environment else []

    @staticmethod
    def _decode_modal_ls_path(value: str) -> str:
        try:
            return value.encode("utf-8").decode("unicode_escape")
        except UnicodeError as exc:
            raise ExternalCommandError("modal volume ls вернул некорректно escaped filename") from exc

    @staticmethod
    def _validate_remote_volume_path(path: str) -> None:
        if not path or path.startswith("/") or "\x00" in path:
            raise CockpitError(f"небезопасный путь в Modal volume: {path!r}")
        if any(part in ("", ".", "..") for part in path.split("/")):
            raise CockpitError(f"небезопасный или неканонический путь в Modal volume: {path!r}")

    @staticmethod
    def _filesystem_collision_flags(root: Path) -> tuple[bool, bool]:
        case_probe = root / f".mc-case-{secrets.token_hex(4)}"
        unicode_probe = root / f".mc-unicode-e\u0301-{secrets.token_hex(4)}"
        try:
            case_probe.touch()
            unicode_probe.touch()
            case_insensitive = (root / case_probe.name.upper()).exists()
            unicode_normalizing = (root / unicodedata.normalize("NFC", unicode_probe.name)).exists()
            return case_insensitive, unicode_normalizing
        finally:
            case_probe.unlink(missing_ok=True)
            unicode_probe.unlink(missing_ok=True)

    def inventory_volume(
        self,
        alias: str,
        volume: str,
        *,
        environment: str | None = None,
        collision_root: Path,
    ) -> dict[str, tuple[str, int]]:
        entries: dict[str, tuple[str, int]] = {}
        collision_seen: dict[str, str] = {}
        case_insensitive, unicode_normalizing = self._filesystem_collision_flags(collision_root)

        def collision_key(path: str) -> str:
            key = unicodedata.normalize("NFC", path) if unicode_normalizing else path
            return key.casefold() if case_insensitive else key

        queue = ["/"]
        visited_dirs: set[str] = set()
        while queue:
            rows = self.list_volume_entries(alias, volume, path=queue.pop(0), environment=environment)
            for row in rows:
                path = self._decode_modal_ls_path(row["filename"])
                self._validate_remote_volume_path(path)
                entry_type = row["type"]
                if entry_type not in ("file", "dir"):
                    raise CockpitError(
                        f"volume {volume!r} содержит unsupported entry {path!r} типа {entry_type!r}; "
                        "переносятся только regular files и directories"
                    )
                key = collision_key(path)
                if key in collision_seen:
                    raise CockpitError(
                        f"volume {volume!r} содержит имена {collision_seen[key]!r} и {path!r}, "
                        "которые конфликтуют на локальной файловой системе"
                    )
                collision_seen[key] = path
                size = row.get("size")
                entries[path] = (entry_type, size if isinstance(size, int) and size >= 0 else 0)
                if entry_type == "dir" and path not in visited_dirs:
                    visited_dirs.add(path)
                    queue.append(path)
        return entries

    @staticmethod
    def _staging_file_sizes(staging: Path) -> dict[str, int]:
        result: dict[str, int] = {}
        for root, _dirs, files in os.walk(staging):
            for name in files:
                path = Path(root) / name
                if path.is_file() and not path.is_symlink():
                    result[path.relative_to(staging).as_posix()] = path.stat().st_size
        return result

    @staticmethod
    def _validate_downloaded_inventory(
        inventory: dict[str, tuple[str, int]],
        staging: Path,
        *,
        volume: str,
    ) -> None:
        remote_files = {path: size for path, (entry_type, size) in inventory.items() if entry_type == "file"}
        local_files = ModalCockpit._staging_file_sizes(staging)
        missing = sorted(set(remote_files) - set(local_files))
        unexpected = sorted(set(local_files) - set(remote_files))
        if missing or unexpected:
            details = []
            if missing:
                details.append("не скачаны: " + ", ".join(missing[:5]))
            if unexpected:
                details.append("лишние локальные файлы: " + ", ".join(unexpected[:5]))
            raise CockpitError(f"download volume {volume!r} incomplete или несогласован: " + "; ".join(details))
        mismatched = sorted(path for path, size in remote_files.items() if local_files[path] != size)
        if mismatched:
            raise CockpitError(
                f"download volume {volume!r} incomplete: размеры не совпадают у " + ", ".join(mismatched[:5])
            )

    @staticmethod
    def validate_destination_volume_name(volume: str) -> str:
        volume = validate_name(volume, label="имя destination Modal volume")
        if len(volume) > 64 or re.fullmatch(r"[A-Za-z0-9_.-]+", volume) is None or re.fullmatch(r"ap-[A-Za-z0-9]{22}", volume):
            raise CockpitError(
                f"некорректное имя destination Modal volume {volume!r}: используйте до 64 символов "
                "латиницы/цифр и '-_.'; имена формата App ID запрещены"
            )
        return volume

    def _run_modal(
        self,
        alias: str,
        args: Sequence[str],
        *,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        self.ensure_profile_present(alias)
        modal = shutil.which("modal")
        if modal is None:
            raise ExternalCommandError("Modal CLI не найден в PATH; установите modal перед transfer")
        env = self.build_exec_environment(alias)
        command = [modal, *args]
        try:
            if capture_output:
                completed = subprocess.run(
                    command,
                    env=env,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
            else:
                completed = subprocess.run(command, env=env, check=False, stdout=2)
        except OSError as exc:
            raise ExternalCommandError(f"не удалось запустить Modal CLI {modal!r}: {exc}") from exc
        if completed.returncode != 0:
            operation = " ".join(args[:2]) or "command"
            detail = ""
            if capture_output:
                combined = self._redact_modal_output((completed.stderr or "") + "\n" + (completed.stdout or "")).strip()
                if combined:
                    detail = ": " + combined[-1200:]
            else:
                detail = "; вывод команды показан выше"
            raise ExternalCommandError(
                f"modal {operation} завершился с кодом {completed.returncode} для аккаунта {alias!r}{detail}"
            )
        return completed

    def list_volume_names(self, alias: str, *, environment: str | None = None) -> list[str]:
        completed = self._run_modal(
            alias,
            ["volume", "list", "--json", *self._environment_args(environment)],
            capture_output=True,
        )
        if not (completed.stdout or "").strip():
            raise ExternalCommandError("modal volume list вернул пустой stdout")
        try:
            rows = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ExternalCommandError("modal volume list вернул некорректный JSON") from exc
        if not isinstance(rows, list):
            raise ExternalCommandError("modal volume list вернул неожиданный JSON-формат")
        names: list[str] = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                raise ExternalCommandError("modal volume list вернул неожиданную строку JSON")
            names.append(row["name"])
        return sorted(names)

    def list_volume_entries(
        self,
        alias: str,
        volume: str,
        *,
        path: str = "/",
        environment: str | None = None,
    ) -> list[dict[str, Any]]:
        volume = validate_name(volume, label="имя Modal volume")
        completed = self._run_modal(
            alias,
            ["volume", "ls", "--json", *self._environment_args(environment), volume, path],
            capture_output=True,
        )
        if not (completed.stdout or "").strip():
            raise ExternalCommandError("modal volume ls вернул пустой stdout")
        try:
            rows = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ExternalCommandError("modal volume ls вернул некорректный JSON") from exc
        if not isinstance(rows, list):
            raise ExternalCommandError("modal volume ls вернул неожиданный JSON-формат")
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("filename"), str) or not isinstance(row.get("type"), str):
                raise ExternalCommandError("modal volume ls вернул неожиданную строку JSON")
        return rows

    def preflight_volume_transfer(
        self,
        source_alias: str,
        destination_alias: str,
        volume: str,
        *,
        destination_volume: str,
        force: bool,
        source_environment: str | None,
        destination_environment: str | None,
    ) -> list[str]:
        source_names = self.list_volume_names(source_alias, environment=source_environment)
        if volume not in source_names:
            raise CockpitError(f"volume {volume!r} не найден в аккаунте {source_alias!r}")
        destination_names = self.list_volume_names(destination_alias, environment=destination_environment)
        if destination_volume in destination_names and not force:
            entries = self.list_volume_entries(
                destination_alias,
                destination_volume,
                environment=destination_environment,
            )
            if entries:
                raise CockpitError(
                    f"целевой volume {destination_volume!r} уже содержит данные; "
                    "используйте --force для merge/overwrite или выберите --dest-volume"
                )
        return destination_names

    @staticmethod
    def _inspect_staging_tree(staging: Path) -> tuple[list[Path], int, int]:
        files: list[Path] = []
        directories = 0
        special_entries = 0
        for root, dirnames, filenames in os.walk(staging, followlinks=False):
            root_path = Path(root)
            for dirname in list(dirnames):
                mode = (root_path / dirname).lstat().st_mode
                if stat.S_ISDIR(mode):
                    directories += 1
                else:
                    special_entries += 1
                    dirnames.remove(dirname)
            for filename in filenames:
                mode = (root_path / filename).lstat().st_mode
                if stat.S_ISREG(mode):
                    files.append(root_path / filename)
                else:
                    special_entries += 1
        if special_entries:
            raise CockpitError(
                f"staging содержит {special_entries} symlink/special entries; transfer остановлен до upload"
            )
        byte_count = sum(path.stat().st_size for path in files)
        return files, byte_count, directories

    def create_volume(self, alias: str, volume: str, *, environment: str | None = None) -> None:
        volume = validate_name(volume, label="имя Modal volume")
        self._run_modal(alias, ["volume", "create", *self._environment_args(environment), volume], capture_output=True)

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _file_manifest(self, root: Path, files: Sequence[Path]) -> dict[str, tuple[int, str]]:
        return {
            path.relative_to(root).as_posix(): (path.stat().st_size, self._sha256_file(path))
            for path in files
        }

    def transfer_volume(
        self,
        source_alias: str,
        destination_alias: str,
        volume: str,
        *,
        destination_volume: str | None = None,
        force: bool = False,
        staging_parent: Path | None = None,
        source_environment: str | None = None,
        destination_environment: str | None = None,
        verify: bool = False,
    ) -> dict[str, Any]:
        transfer_lock = self.store.root / "modal-cockpit-transfer.lock"
        with exclusive_lock(transfer_lock):
            return self._transfer_volume_unlocked(
                source_alias,
                destination_alias,
                volume,
                destination_volume=destination_volume,
                force=force,
                staging_parent=staging_parent,
                source_environment=source_environment,
                destination_environment=destination_environment,
                verify=verify,
            )

    def _transfer_volume_unlocked(
        self,
        source_alias: str,
        destination_alias: str,
        volume: str,
        *,
        destination_volume: str | None = None,
        force: bool = False,
        staging_parent: Path | None = None,
        source_environment: str | None = None,
        destination_environment: str | None = None,
        verify: bool = False,
    ) -> dict[str, Any]:
        """Copy a whole Modal volume through local temporary staging.

        This is a copy-first operation: the source volume is never deleted or mutated.
        Only regular files are guaranteed to transfer. Empty directories, symlinks,
        special file types and timestamps are not preserved by the Modal CLI path.
        """

        source_alias = validate_name(source_alias, label="исходный аккаунт")
        destination_alias = validate_name(destination_alias, label="целевой аккаунт")
        volume = validate_name(volume, label="исходный volume")
        destination_volume = volume if destination_volume is None else validate_name(destination_volume, label="целевой volume")
        self.validate_destination_volume_name(destination_volume)
        if source_alias == destination_alias:
            raise CockpitError("transfer предназначен для копирования между разными аккаунтами")
        if volume.startswith("-") or destination_volume.startswith("-"):
            raise CockpitError("имена volumes не должны начинаться с '-'")

        destination_names = self.preflight_volume_transfer(
            source_alias,
            destination_alias,
            volume,
            destination_volume=destination_volume,
            force=force,
            source_environment=source_environment,
            destination_environment=destination_environment,
        )
        created_volume = False

        if staging_parent is None:
            parent = self.store.root / "tmp"
            ensure_private_dir(parent)
        else:
            parent = staging_parent.expanduser()
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise CockpitError(f"не удалось подготовить staging directory {parent}: {exc}") from exc
            if not parent.is_dir():
                raise CockpitError(f"staging parent не является директорией: {parent}")

        with tempfile.TemporaryDirectory(prefix="volume-transfer-", dir=parent) as temp_dir:
            staging = Path(temp_dir)
            inventory = self.inventory_volume(
                source_alias,
                volume,
                environment=source_environment,
                collision_root=staging,
            )
            self._run_modal(
                source_alias,
                [
                    "volume",
                    "get",
                    *self._environment_args(source_environment),
                    volume,
                    "/",
                    str(staging),
                ],
            )
            self._validate_downloaded_inventory(inventory, staging, volume=volume)
            files, byte_count, directories = self._inspect_staging_tree(staging)
            source_manifest = self._file_manifest(staging, files)
            if destination_volume not in destination_names:
                self.create_volume(destination_alias, destination_volume, environment=destination_environment)
                created_volume = True
            if files:
                self._run_modal(
                    destination_alias,
                    [
                        "volume",
                        "put",
                        *(["--force"] if force else []),
                        *self._environment_args(destination_environment),
                        destination_volume,
                        str(staging),
                        "/.",
                    ],
                )

        verified = False
        if verify and files:
            with tempfile.TemporaryDirectory(prefix="volume-verify-", dir=parent) as verify_dir:
                verify_staging = Path(verify_dir)
                self.inventory_volume(
                    destination_alias,
                    destination_volume,
                    environment=destination_environment,
                    collision_root=verify_staging,
                )
                self._run_modal(
                    destination_alias,
                    [
                        "volume",
                        "get",
                        *self._environment_args(destination_environment),
                        destination_volume,
                        "/",
                        str(verify_staging),
                    ],
                )
                verify_files, _, _ = self._inspect_staging_tree(verify_staging)
                verify_manifest = self._file_manifest(verify_staging, verify_files)
            mismatches = [
                relative
                for relative, expected in source_manifest.items()
                if verify_manifest.get(relative) != expected
            ]
            if mismatches:
                raise CockpitError(
                    f"verification failed для {len(mismatches)} файлов; destination не откатывался автоматически"
                )
            verified = True
        elif verify:
            verified = True

        return {
            "source_account": source_alias,
            "destination_account": destination_alias,
            "source_volume": volume,
            "destination_volume": destination_volume,
            "created_volume": created_volume,
            "files": len(files),
            "directories": directories,
            "bytes": byte_count,
            "verified": verified,
        }

    def transfer_volumes(
        self,
        source_alias: str,
        destination_alias: str,
        *,
        volumes: Sequence[str] | None = None,
        force: bool = False,
        staging_parent: Path | None = None,
        source_environment: str | None = None,
        destination_environment: str | None = None,
        verify: bool = False,
    ) -> list[dict[str, Any]]:
        transfer_lock = self.store.root / "modal-cockpit-transfer.lock"
        with exclusive_lock(transfer_lock):
            return self._transfer_volumes_locked(
                source_alias,
                destination_alias,
                volumes=volumes,
                force=force,
                staging_parent=staging_parent,
                source_environment=source_environment,
                destination_environment=destination_environment,
                verify=verify,
            )

    def _transfer_volumes_locked(
        self,
        source_alias: str,
        destination_alias: str,
        *,
        volumes: Sequence[str] | None = None,
        force: bool = False,
        staging_parent: Path | None = None,
        source_environment: str | None = None,
        destination_environment: str | None = None,
        verify: bool = False,
    ) -> list[dict[str, Any]]:
        if volumes is None:
            names = self.list_volume_names(source_alias, environment=source_environment)
        else:
            names = []
            for volume in volumes:
                volume = validate_name(volume, label="имя Modal volume")
                if volume not in names:
                    names.append(volume)
        for volume in names:
            self.preflight_volume_transfer(
                source_alias,
                destination_alias,
                volume,
                destination_volume=volume,
                force=force,
                source_environment=source_environment,
                destination_environment=destination_environment,
            )
        results = []
        for volume in names:
            try:
                results.append(
                    self._transfer_volume_unlocked(
                        source_alias,
                        destination_alias,
                        volume,
                        force=force,
                        staging_parent=staging_parent,
                        source_environment=source_environment,
                        destination_environment=destination_environment,
                        verify=verify,
                    )
                )
            except CockpitError as exc:
                completed = ", ".join(item["source_volume"] for item in results) or "(none)"
                raise CockpitError(
                    f"перенос volume {volume!r} завершился ошибкой после {len(results)}/{len(names)} "
                    f"успешных volumes [{completed}]: {exc}"
                ) from exc
        return results

    def login_with_modal_cli(
        self,
        alias: str,
        *,
        profile: str | None = None,
        activate: bool = False,
        expires_in: str | None = None,
    ) -> dict[str, Any]:
        alias = validate_name(alias, label="имя аккаунта")
        profile = validate_name(profile or alias, label="имя Modal profile")
        existing = self.store.load_index()["accounts"].get(alias)
        if existing and existing.get("modal_profile") != profile:
            raise StoreError(f"аккаунт {alias!r} уже привязан к профилю {existing.get('modal_profile')!r}")
        other = self.store.find_alias_by_profile(profile)
        if other and other != alias:
            raise StoreError(f"Modal profile {profile!r} уже привязан к аккаунту {other!r}")
        modal = shutil.which("modal")
        if modal is None:
            raise ExternalCommandError("Modal CLI не найден в PATH; установите modal или используйте add")

        self.store.ensure_layout()
        tmp_parent = self.store.root / "tmp"
        ensure_private_dir(tmp_parent)
        tmp_dir = Path(tempfile.mkdtemp(prefix="login-", dir=tmp_parent))
        try:
            cfg = self._load_config()
            template_source: dict[str, Any] = {}
            if profile in cfg.data:
                template_source = cfg.data[profile]
            else:
                try:
                    active = detect_active_profile(cfg.data)
                except ConfigError:
                    active = None
                if active and active in cfg.data:
                    template_source = cfg.data[active]
            template = {
                key: copy.deepcopy(value)
                for key, value in section_without_active(template_source).items()
                if key not in AUTH_CONFIG_KEYS
            }
            doc = tomlkit.document()
            upsert_profile_section(doc, profile, template, replace=True)
            temp_config = tmp_dir / "modal.toml"
            write_text_atomic(temp_config, render_toml(doc), mode=0o600)

            env = os.environ.copy()
            for name in (*CREDENTIAL_ENV_VARS, *TRANSPORT_ENV_VARS):
                env.pop(name, None)
            env["MODAL_PROFILE"] = profile
            env["MODAL_CONFIG_PATH"] = str(temp_config)
            command = [modal, "token", "new", "--no-activate"]
            if expires_in:
                command.extend(["--expires-in", expires_in])
            try:
                completed = subprocess.run(command, env=env, check=False)
            except OSError as exc:
                raise ExternalCommandError(f"не удалось запустить Modal CLI {modal!r}: {exc}") from exc
            if completed.returncode != 0:
                raise ExternalCommandError(f"modal token new завершился с кодом {completed.returncode}; аккаунт не captured")

            login_cfg = load_modal_config(temp_config)
            if profile not in login_cfg.data:
                raise ExternalCommandError("modal token new завершился успешно, но не записал ожидаемый профиль")
            login_section = login_cfg.data[profile]
            if not any(login_section.get(key) for key in AUTH_CONFIG_KEYS):
                raise ExternalCommandError("modal token new завершился успешно, но не записал credentials в профиль")
            return self.import_profile_section(
                alias,
                profile=profile,
                section=login_cfg.data[profile],
                activate=activate,
                reason=f"before-login-{alias}",
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def doctor(self, *, fix_permissions: bool = False) -> list[dict[str, str]]:
        checks: list[dict[str, str]] = []

        def add(level: str, name: str, message: str) -> None:
            checks.append({"level": level, "name": name, "message": message})

        if shutil.which("modal"):
            add("ok", "modal-cli", f"Modal CLI найден: {shutil.which('modal')}")
        else:
            add("warn", "modal-cli", "Modal CLI не найден в PATH; ядро переключения всё равно работает")

        try:
            cfg = self._load_config()
            if cfg.exists:
                add("ok", "config-readable", f"Modal config читается: {self.config_path}")
            else:
                add("warn", "config-missing", f"Modal config отсутствует: {self.config_path}")
            actives = active_profiles(cfg.data)
            if len(actives) == 1:
                add("ok", "active-profile", f"активный профиль: {actives[0]}")
            elif len(actives) > 1:
                add("error", "active-profile", "активно больше одного профиля: " + ", ".join(actives))
            elif len(cfg.data) > 1:
                add("error", "active-profile", "профилей несколько, но ни один не активен")
            else:
                add("warn", "active-profile", "активный профиль явно не задан")
        except Exception as exc:  # noqa: BLE001
            add("error", "config-readable", str(exc))
            cfg = None  # type: ignore[assignment]

        if self.config_path.exists() and os.name == "posix":
            mode = stat.S_IMODE(self.config_path.stat().st_mode)
            if mode & 0o077:
                if fix_permissions:
                    os.chmod(self.config_path, 0o600)
                    add("ok", "config-permissions", "права Modal config исправлены на 0600")
                else:
                    add("warn", "config-permissions", f"права Modal config слишком широкие: {oct(mode)}; рекомендуется 0600")
            else:
                add("ok", "config-permissions", "права Modal config приватные")

        try:
            self.store.ensure_layout()
            index = self.store.load_index()
            add("ok", "store-index", f"аккаунтов в реестре: {len(index['accounts'])}")
            for alias in sorted(index["accounts"]):
                try:
                    account = self.store.load_account(alias)
                    profile = account["modal_profile"]
                    if cfg is not None and cfg.exists and profile not in cfg.data:
                        add("warn", f"account:{alias}", "профиль отсутствует в Modal config, но будет восстановлен из снапшота при switch")
                    else:
                        add("ok", f"account:{alias}", "зашифрованный снапшот читается")
                except Exception as exc:  # noqa: BLE001
                    add("error", f"account:{alias}", str(exc))
        except Exception as exc:  # noqa: BLE001
            add("error", "store-index", str(exc))

        tmp_dir = self.store.root / "tmp"
        if tmp_dir.exists():
            try:
                stale = sorted(
                    path.name
                    for pattern in ("volume-transfer-*", "volume-verify-*", "login-*")
                    for path in tmp_dir.glob(pattern)
                )
            except OSError as exc:
                add("warn", "temporary-files", f"не удалось проверить temporary directories: {exc}")
            else:
                if stale:
                    add(
                        "warn",
                        "temporary-files",
                        f"найдены остатки plaintext temporary directories: {len(stale)}; "
                        "проверьте и удалите их вручную",
                    )
                else:
                    add("ok", "temporary-files", "остатков plaintext temporary directories нет")

        if os.environ.get("MODAL_PROFILE"):
            add(
                "warn",
                "env-profile",
                f"MODAL_PROFILE={os.environ['MODAL_PROFILE']!r} переопределяет активный профиль из файла в текущем shell",
            )
        else:
            add("ok", "env-profile", "MODAL_PROFILE не переопределяет активный профиль")

        env_overrides = [name for name in CREDENTIAL_ENV_VARS if os.environ.get(name)]
        if env_overrides:
            add(
                "warn",
                "env-overrides",
                "переменные окружения переопределяют профили Modal: " + ", ".join(env_overrides),
            )
        else:
            add("ok", "env-overrides", "credential env overrides не установлены")

        transport_env = [name for name in TRANSPORT_ENV_VARS if os.environ.get(name)]
        if transport_env:
            add("warn", "env-transport", "установлены transport env overrides: " + ", ".join(transport_env))
        else:
            add("ok", "env-transport", "transport env overrides не установлены")
        return checks
