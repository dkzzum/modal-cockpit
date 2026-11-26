from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

from .atomic import ensure_private_dir, exclusive_lock, write_text_atomic
from .crypto import LocalCipher, utc_now
from .errors import CryptoError, StoreError

INDEX_VERSION = 1
ACCOUNT_KIND = "modal-profile"
BACKUP_KIND = "modal-config-backup"


TOML_TYPE_KEY = "__modal_cockpit_toml_type__"
TOML_VALUE_KEY = "value"


def encode_json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return {TOML_TYPE_KEY: "datetime", TOML_VALUE_KEY: value.isoformat()}
    if isinstance(value, date):
        return {TOML_TYPE_KEY: "date", TOML_VALUE_KEY: value.isoformat()}
    if isinstance(value, time):
        return {TOML_TYPE_KEY: "time", TOML_VALUE_KEY: value.isoformat()}
    if isinstance(value, dict):
        return {str(key): encode_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode_json_value(item) for item in value]
    return value


def decode_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {TOML_TYPE_KEY, TOML_VALUE_KEY}:
            type_name = value[TOML_TYPE_KEY]
            encoded = value[TOML_VALUE_KEY]
            try:
                if type_name == "datetime" and isinstance(encoded, str):
                    return datetime.fromisoformat(encoded)
                if type_name == "date" and isinstance(encoded, str):
                    return date.fromisoformat(encoded)
                if type_name == "time" and isinstance(encoded, str):
                    return time.fromisoformat(encoded)
            except ValueError:
                pass
        return {key: decode_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode_json_value(item) for item in value]
    return value


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(encode_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def validate_name(value: str, *, label: str) -> str:
    value = value.strip()
    if not value:
        raise StoreError(f"{label} не может быть пустым")
    if len(value) > 256:
        raise StoreError(f"{label} слишком длинный (максимум 256 символов)")
    if any(ord(ch) < 32 or ch == "\x7f" for ch in value):
        raise StoreError(f"{label} не может содержать управляющие символы")
    return value


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in normalized)
    slug = re.sub(r"[-_]{2,}", "-", slug).strip("-. _")
    return slug[:48] or "account"


def fingerprint(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def auth_type(section: dict[str, Any]) -> str:
    if section.get("token_id") or section.get("token_secret"):
        return "token"
    if section.get("oauth_refresh_token") or section.get("oauth_client_id") or section.get("oauth_client_secret") or section.get("oauth_jwt_key"):
        return "oauth"
    return "settings-only"


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.accounts_dir = root / "accounts"
        self.backups_dir = root / "backups"
        self.index_path = root / "accounts-index.json"
        self.key_path = root / "encryption.key"
        self.lock_path = root / "modal-cockpit.lock"
        self.cipher = LocalCipher(self.key_path)

    def ensure_layout(self) -> None:
        ensure_private_dir(self.root)
        ensure_private_dir(self.accounts_dir)
        ensure_private_dir(self.backups_dir)

    def lock(self):
        self.ensure_layout()
        return exclusive_lock(self.lock_path)

    def _empty_index(self) -> dict[str, Any]:
        return {"version": INDEX_VERSION, "current_account": None, "accounts": {}}

    def load_index(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return self._empty_index()
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise StoreError(f"не удалось прочитать индекс аккаунтов {self.index_path}: {exc}") from exc
        if not isinstance(data, dict) or data.get("version") != INDEX_VERSION or not isinstance(data.get("accounts"), dict):
            raise StoreError(f"индекс аккаунтов {self.index_path} повреждён или имеет неподдерживаемую версию")
        for alias, meta in data["accounts"].items():
            if not isinstance(alias, str) or not isinstance(meta, dict) or not isinstance(meta.get("modal_profile"), str) or not isinstance(meta.get("file"), str):
                raise StoreError(f"индекс аккаунтов {self.index_path} содержит повреждённую metadata")
        data.setdefault("current_account", None)
        return data

    def save_index(self, index: dict[str, Any]) -> None:
        self.ensure_layout()
        write_text_atomic(
            self.index_path,
            json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            mode=0o600,
        )

    def account_file_name(self, alias: str) -> str:
        digest = hashlib.sha256(alias.encode("utf-8")).hexdigest()[:24]
        return f"{slugify(alias)}-{digest}.json.enc"

    def account_path(self, alias: str) -> Path:
        return self.accounts_dir / self.account_file_name(alias)

    def list_metadata(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self.load_index()["accounts"])

    def find_alias_by_profile(self, modal_profile: str) -> str | None:
        for alias, meta in self.load_index()["accounts"].items():
            if meta.get("modal_profile") == modal_profile:
                return alias
        return None

    def _validate_unique_profile(self, alias: str, modal_profile: str) -> None:
        existing = self.find_alias_by_profile(modal_profile)
        if existing is not None and existing != alias:
            raise StoreError(
                f"Modal profile {modal_profile!r} уже привязан к аккаунту {existing!r}; "
                "дублирующие alias не поддерживаются"
            )

    def save_account(
        self,
        alias: str,
        modal_profile: str,
        section: dict[str, Any],
        *,
        mark_switched: bool = False,
    ) -> dict[str, Any]:
        alias = validate_name(alias, label="имя аккаунта")
        modal_profile = validate_name(modal_profile, label="имя Modal profile")
        if not isinstance(section, dict):
            raise StoreError("секция профиля должна быть объектом")
        self.ensure_layout()
        self._validate_unique_profile(alias, modal_profile)

        index = self.load_index()
        now = utc_now()
        previous_payload: dict[str, Any] = {}
        previous_meta = index["accounts"].get(alias, {})
        account_path = self.account_path(alias)
        if account_path.exists():
            previous_payload = self.load_account(alias)

        created_at = previous_payload.get("created_at") or previous_meta.get("created_at") or now
        last_switched_at = previous_payload.get("last_switched_at") or previous_meta.get("last_switched_at")
        if mark_switched:
            last_switched_at = now
        payload = {
            "schema_version": 1,
            "alias": alias,
            "modal_profile": modal_profile,
            "section": copy.deepcopy(section),
            "created_at": created_at,
            "updated_at": now,
            "last_switched_at": last_switched_at,
        }
        envelope = self.cipher.encrypt(ACCOUNT_KIND, canonical_json_bytes(payload))
        write_text_atomic(account_path, envelope, mode=0o600)

        metadata = {
            "file": account_path.name,
            "modal_profile": modal_profile,
            "created_at": created_at,
            "updated_at": now,
            "last_switched_at": last_switched_at,
            "token_id_sha256": fingerprint(section.get("token_id")),
            "auth_type": auth_type(section),
        }
        index["accounts"][alias] = metadata
        self.save_index(index)
        return copy.deepcopy(metadata)

    def load_account(self, alias: str) -> dict[str, Any]:
        alias = validate_name(alias, label="имя аккаунта")
        path = self.account_path(alias)
        if not path.exists():
            raise StoreError(f"аккаунт {alias!r} не найден в modal-cockpit")
        try:
            plaintext = self.cipher.decrypt(path.read_text(encoding="utf-8"), expected_kind=ACCOUNT_KIND)
            payload = decode_json_value(json.loads(plaintext.decode("utf-8")))
        except CryptoError:
            raise
        except Exception as exc:
            raise StoreError(f"не удалось разобрать зашифрованный аккаунт {alias!r}") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("alias") != alias
            or not isinstance(payload.get("modal_profile"), str)
            or not payload.get("modal_profile")
            or not isinstance(payload.get("section"), dict)
        ):
            raise StoreError(f"зашифрованный аккаунт {alias!r} повреждён")
        return payload

    def delete_account(self, alias: str) -> None:
        alias = validate_name(alias, label="имя аккаунта")
        index = self.load_index()
        if alias not in index["accounts"]:
            raise StoreError(f"аккаунт {alias!r} не найден в modal-cockpit")
        del index["accounts"][alias]
        if index.get("current_account") == alias:
            index["current_account"] = None
        self.save_index(index)
        self.account_path(alias).unlink(missing_ok=True)

    def set_current_account(self, alias: str | None) -> None:
        index = self.load_index()
        if alias is not None and alias not in index["accounts"]:
            raise StoreError(f"аккаунт {alias!r} не найден в modal-cockpit")
        index["current_account"] = alias
        self.save_index(index)

    def create_backup(self, config_path: Path, text: str, *, reason: str) -> str:
        self.ensure_layout()
        now = utc_now()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        payload = {
            "schema_version": 1,
            "created_at": now,
            "reason": reason,
            "source_path": str(config_path),
            "sha256": digest,
            "encoding": "utf-8",
            "text": text,
        }
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        safe_reason = slugify(reason)[:32]
        backup_id = f"{stamp}-{digest[:12]}-{safe_reason}.mcbackup"
        path = self.backups_dir / backup_id
        envelope = self.cipher.encrypt(BACKUP_KIND, canonical_json_bytes(payload))
        write_text_atomic(path, envelope, mode=0o600)
        return backup_id

    def read_backup(self, backup_id: str) -> dict[str, Any]:
        if backup_id != Path(backup_id).name or not backup_id.endswith(".mcbackup"):
            raise StoreError("некорректный идентификатор backup")
        path = self.backups_dir / backup_id
        if not path.exists():
            raise StoreError(f"backup {backup_id!r} не найден")
        try:
            plaintext = self.cipher.decrypt(path.read_text(encoding="utf-8"), expected_kind=BACKUP_KIND)
            payload = json.loads(plaintext.decode("utf-8"))
        except CryptoError:
            raise
        except Exception as exc:
            raise StoreError(f"backup {backup_id!r} повреждён") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str) or not isinstance(payload.get("sha256"), str):
            raise StoreError(f"backup {backup_id!r} имеет неверный формат")
        actual = hashlib.sha256(payload["text"].encode("utf-8")).hexdigest()
        if actual != payload["sha256"]:
            raise StoreError(f"backup {backup_id!r} не прошёл проверку SHA-256")
        return payload

    def list_backups(self) -> list[dict[str, Any]]:
        self.ensure_layout()
        result: list[dict[str, Any]] = []
        for path in sorted(self.backups_dir.glob("*.mcbackup")):
            try:
                payload = self.read_backup(path.name)
                result.append(
                    {
                        "id": path.name,
                        "created_at": payload.get("created_at"),
                        "reason": payload.get("reason"),
                        "source_path": payload.get("source_path"),
                        "sha256": payload.get("sha256"),
                    }
                )
            except (CryptoError, StoreError, OSError) as exc:
                result.append({"id": path.name, "error": str(exc)})
        return result
