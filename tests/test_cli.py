from __future__ import annotations

from pathlib import Path

from modal_cockpit.cli import main

from .conftest import read_toml


def base_args(tmp_path: Path) -> list[str]:
    return ["--home", str(tmp_path / "store"), "--config-path", str(tmp_path / ".modal.toml")]


def test_cli_add_list_current_switch_never_prints_secrets(tmp_path: Path, capsys):
    args = base_args(tmp_path)
    assert main(args + ["add", "alpha", "--token-id", "ak-alpha", "--token-secret", "as-alpha", "--activate"]) == 0
    assert main(args + ["add", "beta", "--token-id", "ak-beta", "--token-secret", "as-beta"]) == 0
    assert main(args + ["list"]) == 0
    assert main(args + ["switch", "beta"]) == 0
    assert main(args + ["current"]) == 0

    output = capsys.readouterr().out
    assert "as-alpha" not in output
    assert "as-beta" not in output
    assert "ak-alpha" not in output
    assert "ak-beta" not in output
    assert "active profile: beta" in output
    data = read_toml(tmp_path / ".modal.toml")
    assert data["beta"]["active"] is True


def test_cli_backup_and_restore_requires_yes(tmp_path: Path, capsys):
    args = base_args(tmp_path)
    assert main(args + ["add", "alpha", "--token-id", "ak-alpha", "--token-secret", "as-alpha", "--activate"]) == 0
    assert main(args + ["backup", "--reason", "cli test"]) == 0
    backup_line = [line for line in capsys.readouterr().out.splitlines() if line.startswith("backup: ")][-1]
    backup_id = backup_line.removeprefix("backup: ")

    assert main(args + ["restore", backup_id]) == 2
    assert main(args + ["restore", backup_id, "--yes"]) == 0


def test_cli_env_emits_only_profile_metadata(tmp_path: Path, capsys):
    args = base_args(tmp_path)
    assert main(args + ["add", "alpha", "--token-id", "ak-alpha", "--token-secret", "as-alpha", "--activate"]) == 0
    capsys.readouterr()
    assert main(args + ["env", "alpha"]) == 0
    output = capsys.readouterr().out
    assert "MODAL_PROFILE=alpha" in output
    assert "MODAL_CONFIG_PATH=" in output
    assert "ak-alpha" not in output
    assert "as-alpha" not in output


def test_cli_version(capsys):
    try:
        main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    output = capsys.readouterr().out
    assert output.startswith("modal-cockpit ")
