from __future__ import annotations

import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from modal_cockpit.core import ModalCockpit
from modal_cockpit.errors import ConfigError, ExternalCommandError

from .conftest import read_toml


def test_add_switch_and_encrypted_at_rest(cockpit, tmp_path: Path):
    cockpit.add_token_account("alpha", profile=None, token_id="ak-alpha", token_secret="as-alpha", activate=True)
    cockpit.add_token_account("beta", profile=None, token_id="ak-beta", token_secret="as-beta")

    data = read_toml(tmp_path / ".modal.toml")
    assert data["alpha"]["active"] is True
    assert "active" not in data["beta"]

    result = cockpit.switch("beta")
    assert result.changed is True
    assert result.backup_id is not None

    data = read_toml(tmp_path / ".modal.toml")
    assert data["beta"]["active"] is True
    assert "active" not in data["alpha"]
    assert data["alpha"]["token_id"] == "ak-alpha"
    assert data["alpha"]["token_secret"] == "as-alpha"

    mode = stat.S_IMODE((tmp_path / ".modal.toml").stat().st_mode)
    assert mode == 0o600

    encrypted_account = cockpit.store.account_path("beta").read_text(encoding="utf-8")
    assert "as-beta" not in encrypted_account
    assert '"ciphertext"' in encrypted_account

    backup_files = list((tmp_path / "store" / "backups").glob("*.mcbackup"))
    assert backup_files
    for path in backup_files:
        content = path.read_text(encoding="utf-8")
        assert '"ciphertext"' in content
        assert "as-alpha" not in content
        assert "as-beta" not in content


def test_capture_and_switch_preserve_comments_and_settings(cockpit, tmp_path: Path):
    original = textwrap.dedent(
        '''\
        # top-level comment
        [alpha]
        token_id = "ak-alpha" # inline comment
        token_secret = "as-alpha"
        loglevel = "DEBUG"
        active = true

        # beta comment
        [beta]
        token_id = "ak-beta"
        token_secret = "as-beta"
        logs_timeout = 30
        '''
    )
    config_path = tmp_path / ".modal.toml"
    config_path.write_text(original, encoding="utf-8")

    cockpit.capture("alpha", profile="alpha")
    cockpit.capture("beta", profile="beta")
    cockpit.switch("beta")

    switched = config_path.read_text(encoding="utf-8")
    assert "# top-level comment" in switched
    assert "# inline comment" in switched
    assert "# beta comment" in switched
    data = read_toml(config_path)
    assert data["beta"]["active"] is True
    assert data["beta"]["logs_timeout"] == 30
    assert data["alpha"]["loglevel"] == "DEBUG"
    assert "active" not in data["alpha"]


def test_switch_restores_missing_profile_from_encrypted_snapshot(cockpit, tmp_path: Path):
    cockpit.add_token_account("alpha", profile=None, token_id="ak-alpha", token_secret="as-alpha", activate=True)
    cockpit.add_token_account("beta", profile=None, token_id="ak-beta", token_secret="as-beta")

    config_path = tmp_path / ".modal.toml"
    config_path.write_text(
        textwrap.dedent(
            '''\
            [alpha]
            token_id = "ak-alpha"
            token_secret = "as-alpha"
            active = true
            '''
        ),
        encoding="utf-8",
    )

    result = cockpit.switch("beta")
    assert result.restored_missing_profile is True
    data = read_toml(config_path)
    assert data["beta"]["token_secret"] == "as-beta"
    assert data["beta"]["active"] is True


def test_backup_restore_returns_exact_config_text(cockpit, tmp_path: Path):
    config_path = tmp_path / ".modal.toml"
    original = textwrap.dedent(
        '''\
        # carefully preserved backup
        [alpha]
        token_id = "ak-alpha"
        token_secret = "as-alpha"
        active = true
        '''
    )
    config_path.write_text(original, encoding="utf-8")
    backup_id = cockpit.backup_now(reason="test backup")
    assert backup_id

    modified = original + "\n[beta]\ntoken_id = \"ak-beta\"\n"
    config_path.write_text(modified, encoding="utf-8")
    pre_restore = cockpit.restore_backup(backup_id)

    assert pre_restore is not None
    assert config_path.read_text(encoding="utf-8") == original


def test_repeated_backups_have_unique_ids(cockpit, tmp_path: Path):
    config_path = tmp_path / ".modal.toml"
    config_path.write_text('[alpha]\ntoken_id="a"\ntoken_secret="b"\nactive=true\n', encoding="utf-8")
    first = cockpit.backup_now(reason="same")
    second = cockpit.backup_now(reason="same")
    assert first != second


def test_invalid_toml_is_never_overwritten(cockpit, tmp_path: Path):
    config_path = tmp_path / ".modal.toml"
    broken = "[alpha\ntoken_id = "
    config_path.write_text(broken, encoding="utf-8")

    with pytest.raises(ConfigError):
        cockpit.add_token_account("alpha", profile=None, token_id="ak", token_secret="as", activate=True)
    assert config_path.read_text(encoding="utf-8") == broken

    with pytest.raises(ConfigError):
        cockpit.backup_now()
    assert config_path.read_text(encoding="utf-8") == broken


@pytest.mark.parametrize("profile", ["work.prod", "team alpha", "кириллица"])
def test_profile_names_requiring_toml_quotes(cockpit, tmp_path: Path, profile: str):
    cockpit.add_token_account(profile, profile=None, token_id="ak-x", token_secret="as-x", activate=True)
    data = read_toml(tmp_path / ".modal.toml")
    assert profile in data
    assert data[profile]["active"] is True
    assert cockpit.store.load_account(profile)["modal_profile"] == profile


def test_adding_token_replaces_oauth_credentials(cockpit, tmp_path: Path):
    config_path = tmp_path / ".modal.toml"
    config_path.write_text(
        textwrap.dedent(
            '''\
            [alpha]
            oauth_refresh_token = "refresh"
            oauth_client_id = "client"
            oauth_jwt_key = "jwt"
            server_url = "https://example.invalid"
            active = true
            '''
        ),
        encoding="utf-8",
    )

    cockpit.add_token_account("alpha", profile=None, token_id="ak-alpha", token_secret="as-alpha")
    data = read_toml(config_path)
    assert data["alpha"]["token_id"] == "ak-alpha"
    assert data["alpha"]["server_url"] == "https://example.invalid"
    assert "oauth_refresh_token" not in data["alpha"]
    assert "oauth_client_id" not in data["alpha"]
    assert "oauth_jwt_key" not in data["alpha"]
    assert data["alpha"]["active"] is True


def test_capture_explicit_profiles_and_switch_repairs_multiple_active(cockpit, tmp_path: Path):
    config_path = tmp_path / ".modal.toml"
    config_path.write_text(
        textwrap.dedent(
            '''\
            [alpha]
            token_id = "ak-alpha"
            token_secret = "as-alpha"
            active = true

            [beta]
            token_id = "ak-beta"
            token_secret = "as-beta"
            active = true
            '''
        ),
        encoding="utf-8",
    )

    cockpit.capture("alpha", profile="alpha")
    cockpit.capture("beta", profile="beta")
    cockpit.switch("beta")

    data = read_toml(config_path)
    assert data["beta"]["active"] is True
    assert "active" not in data["alpha"]


def test_exec_uses_profile_without_global_switch(cockpit, tmp_path: Path, monkeypatch):
    cockpit.add_token_account("alpha", profile=None, token_id="ak-alpha", token_secret="as-alpha", activate=True)
    cockpit.add_token_account("beta", profile=None, token_id="ak-beta", token_secret="as-beta")
    monkeypatch.setenv("MODAL_TOKEN_ID", "override")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "override")

    output = tmp_path / "exec-env.txt"
    code = (
        "import os,sys,pathlib;"
        "pathlib.Path(sys.argv[1]).write_text("
        "os.environ.get('MODAL_PROFILE','')+'|'+os.environ.get('MODAL_CONFIG_PATH','')+'|'+os.environ.get('MODAL_TOKEN_ID',''),"
        "encoding='utf-8')"
    )
    rc = cockpit.exec("beta", [sys.executable, "-c", code, str(output)])

    assert rc == 0
    assert output.read_text(encoding="utf-8") == f"beta|{tmp_path / '.modal.toml'}|"
    data = read_toml(tmp_path / ".modal.toml")
    assert data["alpha"]["active"] is True
    assert "active" not in data["beta"]


def test_login_uses_isolated_temp_config_and_merges_result(cockpit, tmp_path: Path, monkeypatch):
    config_path = tmp_path / ".modal.toml"
    config_path.write_text(
        textwrap.dedent(
            '''\
            [alpha]
            token_id = "ak-alpha"
            token_secret = "as-alpha"
            server_url = "https://modal.example.invalid"
            active = true
            '''
        ),
        encoding="utf-8",
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "fake-modal-marker.txt"
    fake_modal = bin_dir / "modal"
    fake_modal.write_text(
        textwrap.dedent(
            '''\
            #!/usr/bin/env python3
            import os, sys
            config = os.environ["MODAL_CONFIG_PATH"]
            with open(os.environ["FAKE_MODAL_MARKER"], "w", encoding="utf-8") as marker_file:
                marker_file.write(config)
            with open(config, "a", encoding="utf-8") as config_file:
                config_file.write('token_id = "ak-login"\\n')
                config_file.write('token_secret = "as-login"\\n')
            sys.exit(0)
            '''
        ),
        encoding="utf-8",
    )
    fake_modal.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_MODAL_MARKER", str(marker))

    cockpit.login_with_modal_cli("beta", activate=True)

    used_temp_config = Path(marker.read_text(encoding="utf-8"))
    assert used_temp_config != config_path
    assert not used_temp_config.exists()

    data = read_toml(config_path)
    assert data["beta"]["token_id"] == "ak-login"
    assert data["beta"]["token_secret"] == "as-login"
    assert data["beta"]["server_url"] == "https://modal.example.invalid"
    assert data["beta"]["active"] is True
    assert "active" not in data["alpha"]
    assert cockpit.store.load_account("beta")["section"]["token_id"] == "ak-login"


def seed_two_profiles(config_path: Path, beta_token: str, beta_secret: str) -> None:
    config_path.write_text(
        textwrap.dedent(
            f'''\
            [alpha]
            token_id = "ak-alpha"
            token_secret = "as-alpha"
            active = true

            [beta]
            token_id = "{beta_token}"
            token_secret = "{beta_secret}"
            '''
        ),
        encoding="utf-8",
    )


def test_switch_without_force_preserves_newer_live_profile(cockpit, tmp_path: Path):
    cockpit.add_token_account("alpha", profile=None, token_id="ak-alpha", token_secret="as-alpha", activate=True)
    cockpit.add_token_account("beta", profile=None, token_id="ak-beta", token_secret="as-beta")
    seed_two_profiles(tmp_path / ".modal.toml", "ak-live", "as-live")

    cockpit.switch("beta")

    data = read_toml(tmp_path / ".modal.toml")
    assert data["beta"]["token_id"] == "ak-live"
    assert cockpit.store.load_account("beta")["section"]["token_id"] == "ak-live"


def test_switch_force_snapshot_restores_saved_credentials(cockpit, tmp_path: Path):
    cockpit.add_token_account("alpha", profile=None, token_id="ak-alpha", token_secret="as-alpha", activate=True)
    cockpit.add_token_account("beta", profile=None, token_id="ak-beta", token_secret="as-beta")
    seed_two_profiles(tmp_path / ".modal.toml", "ak-broken", "as-broken")

    cockpit.switch("beta", force_snapshot=True)

    data = read_toml(tmp_path / ".modal.toml")
    assert data["beta"]["token_id"] == "ak-beta"
    assert data["beta"]["token_secret"] == "as-beta"
    assert data["beta"]["active"] is True


def test_config_symlink_is_preserved(tmp_path: Path):
    real_config = tmp_path / "dotfiles" / "modal.toml"
    real_config.parent.mkdir()
    linked_config = tmp_path / ".modal.toml"
    linked_config.symlink_to(real_config)
    cockpit = ModalCockpit(store_home=tmp_path / "store", config_path=linked_config)

    cockpit.add_token_account("alpha", profile=None, token_id="ak-alpha", token_secret="as-alpha", activate=True)

    assert linked_config.is_symlink()
    assert read_toml(real_config)["alpha"]["active"] is True
    assert cockpit.config_path == real_config


def test_exec_missing_command_is_expected_error(cockpit):
    cockpit.add_token_account("alpha", profile=None, token_id="ak-alpha", token_secret="as-alpha", activate=True)
    with pytest.raises(ExternalCommandError):
        cockpit.exec("alpha", ["definitely-not-a-real-modal-cockpit-test-command"])


def test_current_status_distinguishes_file_and_env_profile(cockpit, monkeypatch):
    cockpit.add_token_account("alpha", profile=None, token_id="ak-alpha", token_secret="as-alpha", activate=True)
    cockpit.add_token_account("beta", profile=None, token_id="ak-beta", token_secret="as-beta")
    monkeypatch.setenv("MODAL_PROFILE", "beta")

    status = cockpit.current_status()

    assert status["file_active_profile"] == "alpha"
    assert status["active_profile"] == "beta"
    assert status["env_profile_override"] == "beta"


def test_concurrent_cli_adds_are_serialized(tmp_path: Path):
    store = tmp_path / "store"
    config = tmp_path / ".modal.toml"
    commands = [
        [
            sys.executable,
            "-m",
            "modal_cockpit",
            "--home",
            str(store),
            "--config-path",
            str(config),
            "add",
            f"acc{index}",
            "--token-id",
            f"ak-{index}",
            "--token-secret",
            f"as-{index}",
        ]
        for index in range(4)
    ]
    processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for command in commands]
    failures = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=20)
        if process.returncode != 0:
            failures.append(stderr.decode("utf-8", errors="replace") or stdout.decode("utf-8", errors="replace"))

    assert failures == []
    data = read_toml(config)
    assert sorted(data) == ["acc0", "acc1", "acc2", "acc3"]
    index_data = (store / "accounts-index.json").read_text(encoding="utf-8")
    for index in range(4):
        assert f"acc{index}" in index_data


def test_capture_honors_modal_profile_override(cockpit, monkeypatch):
    cockpit.add_token_account("alpha", profile=None, token_id="ak-alpha", token_secret="as-alpha", activate=True)
    cockpit.config_path.write_text(
        cockpit.config_path.read_text(encoding="utf-8").replace("[alpha]", "[alpha]").replace(
            "active = true", "active = true", 1
        )
        + '\n[gamma]\ntoken_id = "ak-gamma"\ntoken_secret = "as-gamma"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MODAL_PROFILE", "gamma")

    meta = cockpit.capture("captured")

    assert meta["modal_profile"] == "gamma"
    assert cockpit.store.load_account("captured")["section"]["token_id"] == "ak-gamma"
