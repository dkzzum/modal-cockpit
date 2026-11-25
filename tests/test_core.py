from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from modal_cockpit.core import ModalCockpit
from modal_cockpit.errors import CockpitError, ConfigError, ExternalCommandError

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


def install_fake_volume_modal(tmp_path: Path, monkeypatch, state: dict) -> Path:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    state_path = tmp_path / "fake-modal-state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    fake_modal = bin_dir / "modal"
    fake_modal.write_text(
        textwrap.dedent(
            '''\
            #!/usr/bin/env python3
            import json
            import os
            import sys
            from pathlib import Path, PurePosixPath

            state_file = Path(os.environ["FAKE_MODAL_STATE"])
            state = json.loads(state_file.read_text(encoding="utf-8"))
            profile = os.environ["MODAL_PROFILE"]
            account = state.setdefault("profiles", {}).setdefault(profile, {"volumes": {}})
            volumes = account.setdefault("volumes", {})

            def clean_options(items):
                result = []
                force = False
                skip = False
                for index, item in enumerate(items):
                    if skip:
                        skip = False
                        continue
                    if item == "-e":
                        skip = True
                        continue
                    if item in ("--force", "-f"):
                        force = True
                        continue
                    if item == "--json":
                        continue
                    result.append(item)
                return result, force

            args, force = clean_options(sys.argv[1:])
            log_path = os.environ.get("FAKE_MODAL_LOG")
            if log_path:
                with open(log_path, "a", encoding="utf-8") as log_file:
                    log_file.write(json.dumps({
                        "argv": sys.argv[1:],
                        "profile": profile,
                        "modal_environment_present": bool(os.environ.get("MODAL_ENVIRONMENT")),
                        "token_id_present": bool(os.environ.get("MODAL_TOKEN_ID")),
                        "token_secret_present": bool(os.environ.get("MODAL_TOKEN_SECRET")),
                    }) + "\\n")
            if args[:2] == ["volume", "list"]:
                rows = [
                    {"name": name, "created_at": "2026-01-01", "created_by": "fake"}
                    for name in sorted(volumes)
                ]
                print(json.dumps(rows))
                sys.exit(0)
            if args[:2] == ["volume", "create"]:
                volumes.setdefault(args[2], {})
                state_file.write_text(json.dumps(state), encoding="utf-8")
                sys.exit(0)
            if args[:2] == ["volume", "ls"]:
                volume = args[2]
                if volume not in volumes:
                    print("missing volume", file=sys.stderr)
                    sys.exit(2)
                entries = {}
                for relative, content in volumes[volume].items():
                    first, *rest = relative.split("/")
                    if rest:
                        entries[first] = {"filename": first, "type": "dir", "created_modified": "x", "size": 0}
                    else:
                        entries[first] = {
                            "filename": first,
                            "type": "file",
                            "created_modified": "x",
                            "size": len(content),
                        }
                print(json.dumps(list(entries.values())))
                sys.exit(0)
            if args[:2] == ["volume", "get"]:
                volume, remote_path, destination = args[2:5]
                if volume not in volumes:
                    print("missing source volume", file=sys.stderr)
                    sys.exit(2)
                if remote_path != "/":
                    print("fake modal supports only root transfer", file=sys.stderr)
                    sys.exit(2)
                root = Path(destination)
                root.mkdir(parents=True, exist_ok=True)
                for relative, content in volumes[volume].items():
                    target = root / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
                sys.exit(0)
            if args[:2] == ["volume", "put"]:
                volume, local_path, remote_path = args[2:5]
                if volume not in volumes:
                    print("missing destination volume", file=sys.stderr)
                    sys.exit(2)
                root = Path(local_path)
                updates = {}
                for source in root.rglob("*"):
                    if not source.is_file():
                        continue
                    relative = source.relative_to(root).as_posix()
                    if remote_path in ("/", "/."):
                        target = relative
                    else:
                        target = (PurePosixPath(remote_path) / relative).as_posix().lstrip("/")
                    updates[target] = source.read_text(encoding="utf-8")
                if not force:
                    collisions = sorted(set(updates) & set(volumes[volume]))
                    if collisions:
                        print("destination files exist", file=sys.stderr)
                        sys.exit(3)
                volumes[volume].update(updates)
                state_file.write_text(json.dumps(state), encoding="utf-8")
                sys.exit(0)
            print("unsupported fake modal command: " + " ".join(sys.argv[1:]), file=sys.stderr)
            sys.exit(2)
            '''
        ),
        encoding="utf-8",
    )
    fake_modal.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_MODAL_STATE", str(state_path))
    monkeypatch.setenv("FAKE_MODAL_LOG", str(tmp_path / "fake-modal-log.jsonl"))
    return state_path


def seed_transfer_accounts(cockpit):
    cockpit.add_token_account("alpha", profile=None, token_id="ak-alpha", token_secret="as-alpha", activate=True)
    cockpit.add_token_account("beta", profile=None, token_id="ak-beta", token_secret="as-beta")


def test_transfer_volume_creates_destination_and_copies_files(cockpit, tmp_path: Path, monkeypatch):
    seed_transfer_accounts(cockpit)
    state_path = install_fake_volume_modal(
        tmp_path,
        monkeypatch,
        {"profiles": {"alpha": {"volumes": {"data": {"a.txt": "hello", "dir/b.txt": "world"}}}, "beta": {"volumes": {}}}},
    )

    result = cockpit.transfer_volume("alpha", "beta", "data")

    assert result["created_volume"] is True
    assert result["files"] == 2
    assert result["bytes"] == 10
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["profiles"]["beta"]["volumes"]["data"] == {"a.txt": "hello", "dir/b.txt": "world"}
    assert not list((cockpit.store.root / "tmp").glob("volume-transfer-*"))


def test_transfer_all_volumes_copies_every_source_volume(cockpit, tmp_path: Path, monkeypatch):
    seed_transfer_accounts(cockpit)
    state_path = install_fake_volume_modal(
        tmp_path,
        monkeypatch,
        {
            "profiles": {
                "alpha": {"volumes": {"one": {"1.txt": "1"}, "two": {"nested/2.txt": "22"}}},
                "beta": {"volumes": {}},
            }
        },
    )

    results = cockpit.transfer_volumes("alpha", "beta", force=True)

    assert [item["source_volume"] for item in results] == ["one", "two"]
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["profiles"]["beta"]["volumes"]["one"] == {"1.txt": "1"}
    assert state["profiles"]["beta"]["volumes"]["two"] == {"nested/2.txt": "22"}


def test_transfer_volume_collision_requires_force(cockpit, tmp_path: Path, monkeypatch):
    seed_transfer_accounts(cockpit)
    install_fake_volume_modal(
        tmp_path,
        monkeypatch,
        {
            "profiles": {
                "alpha": {"volumes": {"data": {"a.txt": "new"}}},
                "beta": {"volumes": {"data": {"a.txt": "old"}}},
            }
        },
    )

    with pytest.raises(CockpitError, match="уже содержит данные"):
        cockpit.transfer_volume("alpha", "beta", "data")

    result = cockpit.transfer_volume("alpha", "beta", "data", force=True)
    assert result["created_volume"] is False


def test_transfer_volume_rejects_same_source_and_destination(cockpit):
    seed_transfer_accounts(cockpit)
    with pytest.raises(CockpitError):
        cockpit.transfer_volume("alpha", "alpha", "data")


def read_fake_modal_log(tmp_path: Path) -> list[dict]:
    log_path = tmp_path / "fake-modal-log.jsonl"
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


def test_transfer_preflight_prevents_download_to_non_empty_destination(cockpit, tmp_path: Path, monkeypatch):
    seed_transfer_accounts(cockpit)
    install_fake_volume_modal(
        tmp_path,
        monkeypatch,
        {
            "profiles": {
                "alpha": {"volumes": {"data": {"a.txt": "new"}}},
                "beta": {"volumes": {"data": {"a.txt": "old"}}},
            }
        },
    )

    with pytest.raises(CockpitError):
        cockpit.transfer_volume("alpha", "beta", "data")

    commands = [entry["argv"][:2] for entry in read_fake_modal_log(tmp_path)]
    assert ["volume", "get"] not in commands
    assert ["volume", "put"] not in commands


def test_transfer_force_is_upload_only_and_environment_is_scrubbed(cockpit, tmp_path: Path, monkeypatch):
    seed_transfer_accounts(cockpit)
    install_fake_volume_modal(
        tmp_path,
        monkeypatch,
        {"profiles": {"alpha": {"volumes": {"data": {"a.txt": "new"}}}, "beta": {"volumes": {}}}},
    )
    monkeypatch.setenv("MODAL_ENVIRONMENT", "wrong-env")
    monkeypatch.setenv("MODAL_TOKEN_ID", "ak-env")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "as-env")

    cockpit.transfer_volume("alpha", "beta", "data", force=True)

    entries = read_fake_modal_log(tmp_path)
    get_entries = [entry for entry in entries if entry["argv"][:2] == ["volume", "get"]]
    put_entries = [entry for entry in entries if entry["argv"][:2] == ["volume", "put"]]
    assert get_entries and "--force" not in get_entries[0]["argv"]
    assert put_entries and "--force" in put_entries[0]["argv"]
    assert put_entries[0]["argv"][-1] == "/."
    assert all(not entry["modal_environment_present"] for entry in entries)
    assert all(not entry["token_id_present"] for entry in entries)
    assert all(not entry["token_secret_present"] for entry in entries)


def test_staging_tree_rejects_symlink_without_following_it(tmp_path: Path):
    outside = tmp_path / "outside-secret"
    outside.write_text("secret", encoding="utf-8")
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "link").symlink_to(outside)

    with pytest.raises(CockpitError, match="symlink/special"):
        ModalCockpit._inspect_staging_tree(staging)

    assert outside.read_text(encoding="utf-8") == "secret"


def test_transfer_verify_redownloads_destination_and_compares_hashes(cockpit, tmp_path: Path, monkeypatch):
    seed_transfer_accounts(cockpit)
    install_fake_volume_modal(
        tmp_path,
        monkeypatch,
        {"profiles": {"alpha": {"volumes": {"data": {"a.txt": "hello", "dir/b.txt": "world"}}}, "beta": {"volumes": {}}}},
    )

    result = cockpit.transfer_volume("alpha", "beta", "data", verify=True)

    assert result["verified"] is True
    entries = read_fake_modal_log(tmp_path)
    get_entries = [entry for entry in entries if entry["argv"][:2] == ["volume", "get"]]
    assert len(get_entries) == 2
    assert [entry["profile"] for entry in get_entries] == ["alpha", "beta"]
