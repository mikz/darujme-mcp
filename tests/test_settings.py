from __future__ import annotations

import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr, ValidationError

import settings as settings_module
from settings import (
    KEYRING_SERVICE,
    DarujmeCredentials,
    Settings,
    credential_scope_id,
    credentials_file_path,
    keyring_service_name,
    load_credentials,
    store_credentials,
)


def _creds(api_id: int = 42, secret: str = "secret", org_id: int = 2) -> DarujmeCredentials:
    return DarujmeCredentials(api_id=api_id, api_secret=SecretStr(secret), organization_id=org_id)


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
    store_credentials(_creds(api_id=42, secret="secret", org_id=2))

    loaded = load_credentials(Settings())

    assert loaded is not None
    assert loaded.api_id == 42
    assert loaded.api_secret.get_secret_value() == "secret"
    assert loaded.organization_id == 2
    assert credentials_file_path().stat().st_mode & 0o777 == 0o600


def test_invalid_keyring_credentials_fall_through_to_file(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setitem(
        sys.modules,
        "keyring",
        SimpleNamespace(
            get_password=lambda service, account: {
                "api_id": "api-id",
                "api_secret": "secret",
                "organization_id": "2",
            }[account],
            set_password=lambda *args, **kwargs: None,
        ),
    )
    store_credentials(_creds(api_id=99, secret="real-secret", org_id=1))

    loaded = load_credentials(Settings())

    assert loaded is not None
    assert loaded.api_id == 99
    assert loaded.organization_id == 1
    assert loaded.api_secret.get_secret_value() == "real-secret"


def test_credentials_model_rejects_non_integer_api_id() -> None:
    with pytest.raises(ValidationError):
        DarujmeCredentials(api_id="api-id", api_secret=SecretStr("secret"), organization_id=2)


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
    monkeypatch.setenv("DARUJME_SCOPED_CREDENTIALS", "1")
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

    store_credentials(_creds(api_id=42, secret="secret", org_id=2))

    services = {service for service, _ in stored}
    assert services == {keyring_service_name()}
    assert KEYRING_SERVICE not in services


def test_credentials_file_is_scoped_to_cwd(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("DARUJME_SCOPED_CREDENTIALS", "1")
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
    store_credentials(_creds(api_id=11, secret="first-secret", org_id=1))
    first_cfg = credentials_file_path()

    monkeypatch.chdir(second)
    store_credentials(_creds(api_id=22, secret="second-secret", org_id=2))
    second_cfg = credentials_file_path()
    second_loaded = load_credentials(Settings())

    monkeypatch.chdir(first)
    first_loaded = load_credentials(Settings())

    assert first_cfg != second_cfg
    assert first_cfg.parent.parent.name == "scopes"
    assert first_loaded is not None and first_loaded.api_id == 11
    assert second_loaded is not None and second_loaded.api_id == 22
    assert stat.S_IMODE(first_cfg.stat().st_mode) == 0o600


def test_credentials_path_is_global_by_default(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("DARUJME_SCOPED_CREDENTIALS", raising=False)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    monkeypatch.chdir(first)
    first_cfg = credentials_file_path()
    monkeypatch.chdir(second)
    second_cfg = credentials_file_path()

    assert first_cfg == second_cfg
    assert first_cfg.parent.name == "darujme-mcp"
    assert "scopes" not in first_cfg.parts


def test_keyring_service_is_unscoped_by_default(monkeypatch) -> None:
    monkeypatch.delenv("DARUJME_SCOPED_CREDENTIALS", raising=False)
    assert keyring_service_name() == KEYRING_SERVICE
