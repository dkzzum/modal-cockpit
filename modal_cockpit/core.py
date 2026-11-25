from __future__ import annotations

import copy
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit

from .atomic import ensure_private_dir, write_text_atomic
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
        for alias, meta in self.store.load_index()["accounts"].items():
            if alias == exclude_alias:
                continue
            profile = meta.get("modal_profile")
            if isinstance(profile, str) and profile in data:
                self.store.save_account(alias, profile, section_without_active(data[profile]))
                synced.append(alias)
        return synced

    def sync(self) -> list[str]:
        """Refresh encrypted snapshots for managed profiles currently present in Modal config."""

        with self.store.lock():
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
        has_token_auth = any(section.get(key) for key in TOKEN_CONFIG_KEYS)
        has_oauth_auth = any(section.get(key) for key in OAUTH_CONFIG_KEYS)
        if has_token_auth and not all(section.get(key) for key in TOKEN_CONFIG_KEYS):
            raise StoreError("token_id и token_secret должны быть заданы вместе")
        if has_token_auth and has_oauth_auth:
            raise StoreError("Modal не допускает одновременную token и OAuth авторизацию в одном профиле")
        with self.store.lock():
            existing_meta = self.store.load_index()["accounts"].get(alias)
            if existing_meta and existing_meta.get("modal_profile") != profile:
                raise StoreError(
                    f"аккаунт {alias!r} уже привязан к профилю {existing_meta.get('modal_profile')!r}"
                )
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
            restored_missing = modal_profile not in cfg.data
            if restored_missing or force_snapshot:
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
            if modal_profile in cfg.data:
                self.store.save_account(alias, modal_profile, section_without_active(cfg.data[modal_profile]))
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
            validate_rendered_config(text)
            self._write_config_transaction(cfg, text)
            return pre_restore_backup

    def remove_account(self, alias: str, *, delete_profile: bool = False) -> dict[str, Any]:
        alias = validate_name(alias, label="имя аккаунта")
        with self.store.lock():
            account = self.store.load_account(alias)
            modal_profile = account["modal_profile"]
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
        cfg = self._load_config()
        active_error: str | None = None
        try:
            file_active = detect_active_profile(cfg.data)
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
                    "present_in_config": profile in cfg.data if isinstance(profile, str) else False,
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
        return completed.returncode

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
        return checks
