from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from modal_cockpit.core import ModalCockpit


@pytest.fixture
def cockpit(tmp_path: Path) -> ModalCockpit:
    return ModalCockpit(store_home=tmp_path / "store", config_path=tmp_path / ".modal.toml")


def read_toml(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)
