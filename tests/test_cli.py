from __future__ import annotations

import io
import sys
from pathlib import Path

from modal_cockpit.cli import confirm_or_fail, main
from modal_cockpit.core import ModalCockpit

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
    assert "unset MODAL_ENVIRONMENT" in output
    assert "unset MODAL_SERVER_URL" in output
    assert "ak-alpha" not in output
    assert "as-alpha" not in output


def test_cli_version(capsys):
    try:
        main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    output = capsys.readouterr().out
    assert output.startswith("modal-cockpit ")


def test_cli_transfer_requires_confirmation(tmp_path: Path):
    args = base_args(tmp_path)
    assert main(args + ["transfer", "alpha", "beta", "data"]) == 2


def test_cli_transfer_passes_options(tmp_path: Path, monkeypatch, capsys):
    captured = {}

    def fake_transfer(self, source, destination, volume, **kwargs):
        captured.update({"source": source, "destination": destination, "volume": volume, **kwargs})
        return {
            "source_volume": volume,
            "destination_volume": kwargs.get("destination_volume") or volume,
            "created_volume": True,
            "files": 2,
            "bytes": 12,
        }

    monkeypatch.setattr(ModalCockpit, "transfer_volume", fake_transfer)
    args = base_args(tmp_path)
    rc = main(
        args
        + [
            "transfer",
            "alpha",
            "beta",
            "data",
            "--dest-volume",
            "data-copy",
            "--force",
            "--yes",
            "--source-env",
            "main",
            "--dest-env",
            "backup",
        ]
    )

    assert rc == 0
    assert captured["source"] == "alpha"
    assert captured["destination"] == "beta"
    assert captured["volume"] == "data"
    assert captured["destination_volume"] == "data-copy"
    assert captured["force"] is True
    assert captured["source_environment"] == "main"
    assert captured["destination_environment"] == "backup"
    output = capsys.readouterr().out
    assert "transferred: data -> data-copy" in output
    assert "source volumes were not modified or deleted" in output


def test_confirmation_prompt_goes_to_stderr(monkeypatch, capsys):
    class TtyInput(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", TtyInput("yes\n"))

    confirm_or_fail("test action", False, "subject")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "test action" in captured.err


def test_exec_keep_env_after_alias_is_parsed(monkeypatch, tmp_path: Path):
    captured = {}

    def fake_exec(self, alias, command, *, keep_credential_env=False):
        captured.update(alias=alias, command=command, keep_credential_env=keep_credential_env)
        return 0

    monkeypatch.setattr(ModalCockpit, "exec", fake_exec)
    rc = main(base_args(tmp_path) + ["exec", "alpha", "--keep-env", "--", "modal", "app", "list"])

    assert rc == 0
    assert captured["keep_credential_env"] is True
    assert captured["command"] == ["modal", "app", "list"]
