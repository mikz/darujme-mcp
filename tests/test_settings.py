from __future__ import annotations

import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import settings as settings_module
from settings import (
    KEYRING_SERVICE,
    credential_scope_id,
    credentials_file_path,
    keyring_service_name,
    load_settings,
    store_credentials,
)


def test_loads_stored_credentials_from_fallback_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("settings._load_from_keyring", lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "keyring",
        SimpleNamespace(set_password=lambda service, account, password: None),
    )
    store_credentials("api-id", "secret", 2)

    loaded = load_settings()

    assert loaded.darujme_api_id == "api-id"
    assert loaded.darujme_api_secret is not None
    assert loaded.darujme_api_secret.get_secret_value() == "secret"
    assert loaded.darujme_organization_id == 2
    assert credentials_file_path().stat().st_mode & 0o777 == 0o600


def test_scope_id_uses_canonical_cwd(monkeypatch, tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    monkeypatch.chdir(first)
    first_scope = credential_scope_id()
    monkeypatch.chdir(second)
    second_scope = credential_scope_id()
    monkeypatch.chdir(first / ".")

    assert first_scope != second_scope
    assert credential_scope_id() == first_scope


def test_keyring_service_is_scoped_to_cwd(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    stored: dict[tuple[str, str], str] = {}
    monkeypatch.setitem(
        sys.modules,
        "keyring",
        SimpleNamespace(
            set_password=lambda service, account, password: stored.__setitem__(
                (service, account), password
            )
        ),
    )

    store_credentials("api-id", "secret", 2)

    services = {service for service, _ in stored}
    assert services == {keyring_service_name()}
    assert KEYRING_SERVICE not in services


def test_credentials_file_is_scoped_to_cwd(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(settings_module, "_load_from_keyring", lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "keyring",
        SimpleNamespace(set_password=lambda service, account, password: None),
    )
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    monkeypatch.chdir(first)
    store_credentials("first-id", "first-secret", 1)
    first_cfg = credentials_file_path()

    monkeypatch.chdir(second)
    store_credentials("second-id", "second-secret", 2)
    second_cfg = credentials_file_path()
    second_loaded = load_settings()

    monkeypatch.chdir(first)
    first_loaded = load_settings()

    assert first_cfg != second_cfg
    assert first_cfg.parent.parent.name == "scopes"
    assert first_loaded.darujme_api_id == "first-id"
    assert second_loaded.darujme_api_id == "second-id"
    assert stat.S_IMODE(first_cfg.stat().st_mode) == 0o600
