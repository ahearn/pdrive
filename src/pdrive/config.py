"""Settings and credential caching.

Settings live in ``$PDRIVE_CONFIG_DIR`` or ``$XDG_CONFIG_HOME/pdrive`` (default ``~/.config/pdrive``),
file ``config`` (INI, section ``[pdrive]``). The share password is kept out of that file: it goes to
the system keyring when the optional ``keyring`` package has a usable backend, else to ``credentials``
(mode 0600) next to the config.

Precedence for every value: command-line flag > environment variable > config file > default.
"""
from __future__ import annotations

import configparser
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Protocol

from .remote import PdriveError, UsageError

SECTION = "pdrive"
KEYRING_SERVICE = "pdrive"


# -- value parsing -------------------------------------------------------------
_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)(i?b?)?\s*$", re.I)


def parse_size(text: str) -> int:
    """Parse ``10M``, ``1.5G``, ``500k``, ``0`` into bytes (binary multiples, like curl --limit-rate)."""
    m = _SIZE_RE.match(str(text))
    if not m:
        raise UsageError(f"invalid size: {text!r} (examples: 500k, 10M, 1.5G)")
    mult = {"": 1, "k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40}[m.group(2).lower()]
    return int(float(m.group(1)) * mult)


def _positive_int(text: str) -> int:
    try:
        v = int(text)
    except ValueError:
        raise UsageError(f"invalid number: {text!r}") from None
    if v < 1:
        raise UsageError(f"must be at least 1: {text!r}")
    return v


def _choice(*options: str) -> Callable[[str], str]:
    def check(text: str) -> str:
        if text not in options:
            raise UsageError(f"invalid value {text!r}; choose from: {', '.join(options)}")
        return text
    return check


@dataclass(frozen=True)
class Key:
    name: str
    env: str
    default: object
    parse: Callable[[str], object]
    help: str


KEYS: dict[str, Key] = {k.name: k for k in [
    Key("share_url", "PDRIVE_SHARE_URL", None, str, "public share link, including the #fragment"),
    Key("jobs", "PDRIVE_JOBS", 4, _positive_int, "parallel transfers (the SDK runs at most 5 per session)"),
    Key("bridges", "PDRIVE_BRIDGES", 8, _positive_int,
        "max bridge processes (each decrypts on its own core); extra ones start only when busy"),
    Key("limit_rate", "PDRIVE_LIMIT_RATE", 0, parse_size, "upload rate cap in bytes/s, e.g. 20M (0 = unlimited)"),
    Key("dns", "PDRIVE_DNS", "system", _choice("system", "cloudflare"), "resolver for the bridge: system | cloudflare"),
    Key("node", "PDRIVE_NODE", None, str, "path to the node binary (default: PATH, then fnm's default)"),
    Key("password_store", "PDRIVE_PASSWORD_STORE", "auto", _choice("auto", "keyring", "file"),
        "where login saves the password: auto | keyring | file"),
]}


def config_dir(env: Mapping[str, str] = os.environ) -> Path:
    if env.get("PDRIVE_CONFIG_DIR"):
        return Path(env["PDRIVE_CONFIG_DIR"])
    base = env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "pdrive"


# -- config file -----------------------------------------------------------------
class ConfigFile:
    def __init__(self, directory: Path):
        self.dir = directory
        self.path = directory / "config"

    def _load(self) -> configparser.ConfigParser:
        cp = configparser.ConfigParser(interpolation=None)
        if self.path.exists():
            cp.read(self.path, encoding="utf-8")
        if not cp.has_section(SECTION):
            cp.add_section(SECTION)
        return cp

    def items(self) -> dict[str, str]:
        return dict(self._load()[SECTION])

    def get(self, name: str) -> Optional[str]:
        return self.items().get(name)

    def set(self, name: str, value: str) -> None:
        key = KEYS.get(name)
        if key is None:
            raise UsageError(f"unknown setting: {name} (known: {', '.join(KEYS)})")
        key.parse(value)  # validate before saving
        cp = self._load()
        cp[SECTION][name] = value
        self._write(cp)

    def unset(self, name: str) -> bool:
        cp = self._load()
        removed = cp.remove_option(SECTION, name)
        if removed:
            self._write(cp)
        return removed

    def _write(self, cp: configparser.ConfigParser) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            cp.write(f)
        os.replace(tmp, self.path)


# -- credentials -----------------------------------------------------------------
class CredentialStore(Protocol):
    name: str
    def get(self, share_url: str) -> Optional[str]: ...
    def set(self, share_url: str, password: str) -> None: ...
    def delete(self, share_url: str) -> bool: ...


class FileCredentialStore:
    """Passwords in an INI file readable only by the user (0600), keyed by share URL."""

    name = "file"

    def __init__(self, directory: Path):
        self.dir = directory
        self.path = directory / "credentials"

    def _load(self) -> configparser.ConfigParser:
        cp = configparser.ConfigParser(interpolation=None)
        cp.optionxform = str  # type: ignore[assignment]  # URLs are case-sensitive
        if self.path.exists():
            cp.read(self.path, encoding="utf-8")
        return cp

    def get(self, share_url: str) -> Optional[str]:
        cp = self._load()
        return cp.get(share_url, "password", fallback=None) if cp.has_section(share_url) else None

    def set(self, share_url: str, password: str) -> None:
        cp = self._load()
        if not cp.has_section(share_url):
            cp.add_section(share_url)
        cp[share_url]["password"] = password
        self._write(cp)

    def delete(self, share_url: str) -> bool:
        cp = self._load()
        removed = cp.remove_section(share_url)
        if removed:
            self._write(cp)
        return removed

    def _write(self, cp: configparser.ConfigParser) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            cp.write(f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)


class KeyringCredentialStore:
    name = "keyring"

    def __init__(self, keyring_module):
        self._kr = keyring_module

    def get(self, share_url: str) -> Optional[str]:
        return self._kr.get_password(KEYRING_SERVICE, share_url)

    def set(self, share_url: str, password: str) -> None:
        self._kr.set_password(KEYRING_SERVICE, share_url, password)

    def delete(self, share_url: str) -> bool:
        try:
            self._kr.delete_password(KEYRING_SERVICE, share_url)
            return True
        except Exception:
            return False


def _usable_keyring():
    try:
        import keyring  # optional dependency
        from keyring.backends import fail
    except ImportError:
        return None
    try:
        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
    except Exception:
        return None
    return keyring


def credential_store(choice: str, directory: Path, keyring_loader=_usable_keyring) -> CredentialStore:
    if choice in ("auto", "keyring"):
        kr = keyring_loader()
        if kr is not None:
            return KeyringCredentialStore(kr)
        if choice == "keyring":
            raise PdriveError("no usable keyring backend (pip install keyring, and run a secret service)")
    return FileCredentialStore(directory)


# -- resolved settings -----------------------------------------------------------
class Settings:
    """Effective settings: CLI overrides > environment > config file > defaults."""

    def __init__(self, cfg: ConfigFile, env: Mapping[str, str] = os.environ,
                 overrides: Optional[dict[str, object]] = None):
        self.cfg = cfg
        self.env = env
        self.overrides = {k: v for k, v in (overrides or {}).items() if v is not None}

    def source(self, name: str) -> str:
        key = KEYS[name]
        if name in self.overrides:
            return "flag"
        if self.env.get(key.env):
            return "env"
        if self.cfg.get(name) is not None:
            return "config"
        return "default"

    def get(self, name: str):
        key = KEYS[name]
        if name in self.overrides:
            return self.overrides[name]
        raw = self.env.get(key.env) or self.cfg.get(name)
        return key.default if raw is None else key.parse(raw)

    def store(self, keyring_loader=_usable_keyring) -> CredentialStore:
        return credential_store(self.get("password_store"), self.cfg.dir, keyring_loader)

    def password(self, share_url: str, keyring_loader=_usable_keyring) -> Optional[str]:
        if "password" in self.overrides:
            return str(self.overrides["password"])
        if "PDRIVE_PASSWORD" in self.env:
            return self.env["PDRIVE_PASSWORD"]
        # Look in both stores so changing password_store later doesn't lose a saved password.
        stores: list[CredentialStore] = [FileCredentialStore(self.cfg.dir)]
        kr = keyring_loader()
        if kr is not None:
            stores.insert(0, KeyringCredentialStore(kr))
        for s in stores:
            try:
                pw = s.get(share_url)
            except Exception:
                pw = None
            if pw is not None:
                return pw
        return None
