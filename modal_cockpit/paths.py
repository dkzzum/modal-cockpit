from __future__ import annotations

import os
from pathlib import Path

STORE_HOME_ENV = "MODAL_COCKPIT_HOME"
MODAL_CONFIG_ENV = "MODAL_CONFIG_PATH"
DEFAULT_STORE_DIRNAME = ".modal-cockpit"
DEFAULT_MODAL_CONFIG = ".modal.toml"


def default_store_home() -> Path:
    override = os.environ.get(STORE_HOME_ENV)
    if override and override.strip():
        return Path(override).expanduser()
    return Path.home() / DEFAULT_STORE_DIRNAME


def default_modal_config_path() -> Path:
    override = os.environ.get(MODAL_CONFIG_ENV)
    if override and override.strip():
        return Path(override).expanduser()
    return Path.home() / DEFAULT_MODAL_CONFIG


def coerce_path(value: str | None, fallback: Path) -> Path:
    if value and value.strip():
        return Path(value).expanduser().absolute()
    return fallback.absolute()
