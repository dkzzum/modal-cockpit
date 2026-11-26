from __future__ import annotations

import copy
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.items import AbstractTable

from .errors import ConfigError


@dataclass
class LoadedModalConfig:
    path: Path
    exists: bool
    text: str
    data: dict[str, dict[str, Any]]
    doc: tomlkit.TOMLDocument


def _validate_top_level(data: dict[str, Any], path: Path) -> dict[str, dict[str, Any]]:
    bad = [key for key, value in data.items() if not isinstance(value, dict)]
    if bad:
        raise ConfigError(
            f"{path}: Modal config должен состоять только из TOML-секций профилей; "
            f"верхнеуровневые значения без секции: {', '.join(bad)}"
        )
    return copy.deepcopy(data)  # type: ignore[return-value]


def load_modal_config(path: Path) -> LoadedModalConfig:
    if not path.exists():
        return LoadedModalConfig(
            path=path,
            exists=False,
            text="",
            data={},
            doc=tomlkit.document(),
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"не удалось прочитать Modal config {path}: {exc}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path}: файл не является UTF-8 TOML") from exc
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: повреждён TOML, запись отменена: {exc}") from exc
    data = _validate_top_level(parsed, path)
    try:
        doc = tomlkit.parse(text)
    except Exception as exc:
        raise ConfigError(f"{path}: tomlkit не смог разобрать файл, запись отменена: {exc}") from exc
    return LoadedModalConfig(path=path, exists=True, text=text, data=data, doc=doc)


def active_profiles(data: dict[str, dict[str, Any]]) -> list[str]:
    return [name for name, section in data.items() if section.get("active") is True]


def detect_active_profile(data: dict[str, dict[str, Any]]) -> str | None:
    actives = active_profiles(data)
    if len(actives) > 1:
        raise ConfigError(
            "в Modal config активно больше одного профиля: "
            + ", ".join(actives)
            + "; сначала исправьте файл или выполните switch"
        )
    if actives:
        return actives[0]
    if "default" in data:
        return "default"
    return None


def ensure_no_multiple_active(data: dict[str, dict[str, Any]]) -> None:
    actives = active_profiles(data)
    if len(actives) > 1:
        raise ConfigError(
            "в Modal config активно больше одного профиля: "
            + ", ".join(actives)
            + "; запись в таком состоянии небезопасна"
        )


def section_without_active(section: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(section)
    result.pop("active", None)
    return result


def _put_value(table: AbstractTable, key: str, value: Any) -> None:
    table[key] = copy.deepcopy(value)


def upsert_profile_section(
    doc: tomlkit.TOMLDocument,
    profile: str,
    section: dict[str, Any],
    *,
    replace: bool = False,
) -> None:
    """Insert or merge a profile section, never copying its active flag."""

    clean = section_without_active(section)
    if profile not in doc:
        table = tomlkit.table()
        for key, value in clean.items():
            _put_value(table, key, value)
        doc[profile] = table
        return

    existing = doc[profile]
    if not isinstance(existing, AbstractTable):
        raise ConfigError(f"профиль {profile!r} в TOML не является таблицей")
    if replace:
        for key in list(existing.keys()):
            del existing[key]
    for key, value in clean.items():
        _put_value(existing, key, value)


def set_only_active_profile(doc: tomlkit.TOMLDocument, profile: str) -> None:
    if profile not in doc or not isinstance(doc[profile], AbstractTable):
        raise ConfigError(f"профиль {profile!r} отсутствует в Modal config")
    for item in doc.values():
        if isinstance(item, AbstractTable) and "active" in item:
            del item["active"]
    doc[profile]["active"] = True  # type: ignore[index]


def remove_profile_section(doc: tomlkit.TOMLDocument, profile: str) -> bool:
    if profile not in doc:
        return False
    del doc[profile]
    return True


def render_toml(doc: tomlkit.TOMLDocument) -> str:
    return tomlkit.dumps(doc)


def validate_rendered_config(text: str, *, expected_active: str | None = None, allow_multiple_active: bool = False) -> dict[str, dict[str, Any]]:
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"внутренняя ошибка: сгенерирован повреждённый TOML: {exc}") from exc
    data = _validate_top_level(parsed, Path("<memory>"))
    actives = active_profiles(data)
    if len(actives) > 1 and not allow_multiple_active:
        raise ConfigError("внутренняя ошибка: сгенерировано несколько активных профилей")
    if expected_active is not None:
        if expected_active not in data:
            raise ConfigError(f"внутренняя ошибка: профиль {expected_active!r} потерян при генерации TOML")
        if actives != [expected_active]:
            raise ConfigError(f"внутренняя ошибка: профиль {expected_active!r} не стал единственным активным")
    return data
