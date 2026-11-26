from __future__ import annotations

import base64
import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .atomic import _fsync_dir
from .errors import CryptoError

KEY_ID = "local-file-v1"
ALGORITHM = "AES-256-GCM"
VERSION = 1


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(value: str, field: str) -> bytes:
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except Exception as exc:
        raise CryptoError(f"повреждённое поле {field} в зашифрованном файле") from exc


class LocalCipher:
    """AES-256-GCM envelope encryption with a local randomly generated key.

    The key file is protected by user-only permissions; encryption primarily prevents secret
    leakage when backups or account files are copied outside the user's home.
    """

    def __init__(self, key_path: Path):
        self.key_path = key_path
        self._key: bytes | None = None

    def _create_key_file_once(self, encoded_key: str) -> None:
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.key_path.with_name(f".{self.key_path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(encoded_key + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(tmp, self.key_path)
            except FileExistsError:
                pass
            except OSError as exc:
                raise CryptoError(f"файловая система не позволяет создать ключ {self.key_path}: {exc}") from exc
            else:
                _fsync_dir(self.key_path.parent)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _read_key_file(self) -> bytes:
        try:
            raw = self.key_path.read_text(encoding="utf-8").strip()
            key = base64.b64decode(raw.encode("ascii"), validate=True)
        except Exception as exc:
            raise CryptoError(f"не удалось прочитать ключ шифрования {self.key_path}") from exc
        if len(key) != 32:
            raise CryptoError(f"ключ шифрования {self.key_path} имеет неверную длину")
        return key

    def _load_or_create_key(self) -> bytes:
        if self.key_path.exists():
            key = self._read_key_file()
            if self._key is not None and self._key != key:
                raise CryptoError(f"ключ шифрования {self.key_path} изменился во время работы процесса")
        else:
            encoded = _b64e(self._key or secrets.token_bytes(32))
            self._create_key_file_once(encoded)
            key = self._read_key_file()
        if os.name == "posix":
            os.chmod(self.key_path, 0o600)
        self._key = key
        return key

    def validate_or_create_key(self) -> None:
        self._load_or_create_key()

    @staticmethod
    def _aad(kind: str) -> bytes:
        return f"modal-cockpit:v{VERSION}:{kind}".encode()

    def encrypt(self, kind: str, plaintext: bytes) -> str:
        key = self._load_or_create_key()
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, self._aad(kind))
        envelope: dict[str, Any] = {
            "version": VERSION,
            "kind": kind,
            "algorithm": ALGORITHM,
            "key_id": KEY_ID,
            "nonce": _b64e(nonce),
            "ciphertext": _b64e(ciphertext),
            "encrypted_at": utc_now(),
        }
        return json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def decrypt(self, envelope_text: str, *, expected_kind: str | None = None) -> bytes:
        try:
            envelope = json.loads(envelope_text)
        except json.JSONDecodeError as exc:
            raise CryptoError("зашифрованный файл не является корректным JSON envelope") from exc
        if not isinstance(envelope, dict):
            raise CryptoError("зашифрованный файл имеет неверный формат")
        if envelope.get("version") != VERSION or envelope.get("algorithm") != ALGORITHM:
            raise CryptoError("неподдерживаемая версия или алгоритм шифрования")
        if envelope.get("key_id") != KEY_ID:
            raise CryptoError("неподдерживаемый источник ключа шифрования")
        kind = envelope.get("kind")
        if not isinstance(kind, str) or not kind:
            raise CryptoError("в зашифрованном файле отсутствует kind")
        if expected_kind is not None and kind != expected_kind:
            raise CryptoError(f"ожидался зашифрованный объект {expected_kind}, получен {kind}")
        nonce = _b64d(str(envelope.get("nonce", "")), "nonce")
        ciphertext = _b64d(str(envelope.get("ciphertext", "")), "ciphertext")
        if len(nonce) != 12:
            raise CryptoError("nonce в зашифрованном файле имеет неверную длину")
        key = self._load_or_create_key()
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, self._aad(kind))
        except InvalidTag as exc:
            raise CryptoError("не удалось расшифровать файл: ключ не подходит или файл повреждён") from exc
