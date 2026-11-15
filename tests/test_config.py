import os
import stat

import pytest

from pdrive.config import (ConfigFile, FileCredentialStore, KeyringCredentialStore, Settings, config_dir,
                           credential_store, parse_size)
from pdrive.remote import PdriveError, UsageError


class FakeKeyring:
    def __init__(self):
        self.data = {}

    def get_password(self, svc, user):
        return self.data.get((svc, user))

    def set_password(self, svc, user, pw):
        self.data[(svc, user)] = pw

    def delete_password(self, svc, user):
        del self.data[(svc, user)]


@pytest.mark.parametrize("text,expected", [
    ("0", 0), ("500", 500), ("500k", 512000), ("10M", 10 << 20), ("1.5G", int(1.5 * (1 << 30))),
    ("20MB", 20 << 20), ("20MiB", 20 << 20), (" 3 m ", 3 << 20),
])
def test_parse_size(text, expected):
    assert parse_size(text) == expected


@pytest.mark.parametrize("bad", ["", "fast", "-1", "10X"])
def test_parse_size_rejects(bad):
    with pytest.raises(UsageError):
        parse_size(bad)


def test_config_dir_precedence(tmp_path):
    assert config_dir({"PDRIVE_CONFIG_DIR": "/a"}) == type(tmp_path)("/a")
    assert config_dir({"XDG_CONFIG_HOME": "/x"}) == type(tmp_path)("/x/pdrive")


def test_config_file_roundtrip(tmp_path):
    cfg = ConfigFile(tmp_path / "cfg")
    assert cfg.get("jobs") is None
    cfg.set("jobs", "3")
    cfg.set("share_url", "https://x/y#Key%")  # no interpolation surprises
    assert ConfigFile(tmp_path / "cfg").items() == {"jobs": "3", "share_url": "https://x/y#Key%"}
    assert cfg.unset("jobs") and not cfg.unset("jobs")


def test_config_set_validates(tmp_path):
    cfg = ConfigFile(tmp_path)
    with pytest.raises(UsageError):
        cfg.set("jobs", "0")
    with pytest.raises(UsageError):
        cfg.set("dns", "google")
    with pytest.raises(UsageError, match="unknown setting"):
        cfg.set("colour", "blue")
    assert not (tmp_path / "config").exists()


def test_settings_precedence(tmp_path):
    cfg = ConfigFile(tmp_path)
    s = Settings(cfg, env={})
    assert s.get("jobs") == 4 and s.source("jobs") == "default"
    cfg.set("jobs", "2")
    assert s.get("jobs") == 2 and s.source("jobs") == "config"
    s = Settings(cfg, env={"PDRIVE_JOBS": "3"})
    assert s.get("jobs") == 3 and s.source("jobs") == "env"
    s = Settings(cfg, env={"PDRIVE_JOBS": "3"}, overrides={"jobs": 5, "share_url": None})
    assert s.get("jobs") == 5 and s.source("jobs") == "flag"
    assert s.get("share_url") is None  # None overrides are ignored


def test_file_credentials_are_private(tmp_path):
    store = FileCredentialStore(tmp_path)
    store.set("https://u#K", "pw")
    assert stat.S_IMODE(os.stat(tmp_path / "credentials").st_mode) == 0o600
    assert FileCredentialStore(tmp_path).get("https://u#K") == "pw"
    assert store.get("https://other") is None
    assert store.delete("https://u#K") and store.get("https://u#K") is None


def test_store_selection(tmp_path):
    kr = FakeKeyring()
    assert isinstance(credential_store("auto", tmp_path, lambda: kr), KeyringCredentialStore)
    assert isinstance(credential_store("auto", tmp_path, lambda: None), FileCredentialStore)
    assert isinstance(credential_store("file", tmp_path, lambda: kr), FileCredentialStore)
    with pytest.raises(PdriveError, match="keyring"):
        credential_store("keyring", tmp_path, lambda: None)


def test_password_lookup_order(tmp_path):
    cfg = ConfigFile(tmp_path)
    kr = FakeKeyring()
    FileCredentialStore(tmp_path).set("U", "from-file")
    assert Settings(cfg, env={}).password("U", keyring_loader=lambda: kr) == "from-file"
    KeyringCredentialStore(kr).set("U", "from-keyring")
    assert Settings(cfg, env={}).password("U", keyring_loader=lambda: kr) == "from-keyring"
    assert Settings(cfg, env={"PDRIVE_PASSWORD": "env"}).password("U", keyring_loader=lambda: kr) == "env"
    assert Settings(cfg, env={}).password("other", keyring_loader=lambda: None) is None
