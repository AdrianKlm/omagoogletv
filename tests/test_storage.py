"""XDG paths, atomic state writes and file permissions."""

import json
import os
import stat

import pytest

from google_tv_remote.storage import Storage


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return Storage()


def mode_of(path):
    return stat.S_IMODE(path.stat().st_mode)


# --- paths ---

def test_uses_xdg_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    s = Storage()
    assert s.credentials_dir == tmp_path / "d" / "io.github.adrianklm.omagoogletv" / "credentials"
    assert s.state_file == tmp_path / "s" / "io.github.adrianklm.omagoogletv" / "state.json"


def test_falls_back_to_default_paths_without_xdg(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    s = Storage()
    assert s.credentials_dir == tmp_path / ".local/share/io.github.adrianklm.omagoogletv/credentials"
    assert s.state_file == tmp_path / ".local/state/io.github.adrianklm.omagoogletv/state.json"


def test_ignores_empty_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", "")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert Storage().data_dir == tmp_path / ".local/share/io.github.adrianklm.omagoogletv"


def test_certificate_and_key_live_in_the_credentials_directory(storage):
    assert storage.certfile.parent == storage.credentials_dir
    assert storage.keyfile.parent == storage.credentials_dir
    assert storage.certfile != storage.keyfile


# --- permissions ---

def test_ensure_dirs_creates_directories_with_mode_0700(storage):
    storage.ensure_dirs()
    for directory in (storage.data_dir, storage.credentials_dir, storage.state_file.parent):
        assert directory.is_dir()
        assert mode_of(directory) == 0o700


def test_ensure_dirs_tightens_permissions_that_were_too_open(storage):
    storage.credentials_dir.mkdir(parents=True)
    storage.credentials_dir.chmod(0o755)
    storage.ensure_dirs()
    assert mode_of(storage.credentials_dir) == 0o700


def test_ensure_dirs_is_idempotent(storage):
    storage.ensure_dirs()
    storage.ensure_dirs()
    assert mode_of(storage.credentials_dir) == 0o700


def test_saved_state_has_mode_0600(storage):
    storage.save_state(host="192.168.1.42", name="Chromecast")
    assert mode_of(storage.state_file) == 0o600


def test_harden_credentials_tightens_file_modes(storage):
    storage.ensure_dirs()
    storage.certfile.write_text("x")
    storage.keyfile.write_text("y")
    storage.certfile.chmod(0o644)
    storage.keyfile.chmod(0o644)
    storage.harden_credentials()
    assert mode_of(storage.certfile) == 0o600
    assert mode_of(storage.keyfile) == 0o600


def test_harden_credentials_survives_missing_files(storage):
    storage.ensure_dirs()
    storage.harden_credentials()


# --- writing and reading ---

def test_write_and_read_round_trip(storage):
    storage.save_state(host="192.168.1.42", name="Chromecast")
    assert storage.load_state() == {"host": "192.168.1.42", "name": "Chromecast", "mac": ""}


def test_missing_file_yields_none(storage):
    assert storage.load_state() is None


def test_corrupt_json_yields_none_instead_of_raising(storage):
    storage.ensure_dirs()
    storage.state_file.write_text("{this is not json")
    assert storage.load_state() is None


def test_state_that_is_not_an_object_yields_none(storage):
    storage.ensure_dirs()
    storage.state_file.write_text('["a list"]')
    assert storage.load_state() is None


def test_state_with_a_non_local_host_is_rejected(storage):
    # the state file can be swapped; we trust it no more than IPC input
    storage.ensure_dirs()
    storage.state_file.write_text(json.dumps({"host": "8.8.8.8", "name": "Stranger"}))
    assert storage.load_state() is None


def test_state_without_a_host_is_rejected(storage):
    storage.ensure_dirs()
    storage.state_file.write_text(json.dumps({"name": "Chromecast"}))
    assert storage.load_state() is None


def test_write_rejects_a_non_local_host(storage):
    from google_tv_remote.messages import ProtocolError
    with pytest.raises(ProtocolError):
        storage.save_state(host="8.8.8.8", name="Stranger")


def test_overwriting_state_leaves_a_single_file(storage):
    storage.save_state(host="192.168.1.42", name="Old")
    storage.save_state(host="192.168.1.42", name="New")
    assert storage.load_state()["name"] == "New"
    files = sorted(p.name for p in storage.state_file.parent.iterdir())
    assert files == ["state.json"], f"temporary files left behind: {files}"


def test_write_is_atomic_when_the_replace_fails(storage, monkeypatch):
    storage.save_state(host="192.168.1.42", name="Good")

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(OSError):
        storage.save_state(host="192.168.1.42", name="New")

    # the old state must survive a failed write, with no litter beside it
    assert storage.load_state() == {"host": "192.168.1.42", "name": "Good", "mac": ""}
    assert sorted(p.name for p in storage.state_file.parent.iterdir()) == ["state.json"]


def test_clear_state_removes_the_file(storage):
    storage.save_state(host="192.168.1.42", name="Chromecast")
    storage.clear_state()
    assert not storage.state_file.exists()
    assert storage.load_state() is None


def test_clear_state_survives_a_missing_file(storage):
    storage.clear_state()


def test_save_state_creates_the_directories_itself(storage):
    assert not storage.state_file.parent.exists()
    storage.save_state(host="192.168.1.42", name="Chromecast")
    assert mode_of(storage.state_file.parent) == 0o700


# --- the device MAC address ---

def test_writes_and_reads_the_mac(storage):
    storage.save_state(host="192.168.1.42", name="Chromecast", mac="AA:BB:CC:DD:EE:FF")
    assert storage.load_state()["mac"] == "AA:BB:CC:DD:EE:FF"


def test_state_without_a_mac_yields_an_empty_string(storage):
    storage.save_state(host="192.168.1.42", name="Chromecast")
    assert storage.load_state()["mac"] == ""


def test_mac_of_the_wrong_type_is_ignored(storage):
    storage.ensure_dirs()
    storage.state_file.write_text(json.dumps({"host": "192.168.1.42", "name": "X", "mac": 42}))
    assert storage.load_state()["mac"] == ""
