from __future__ import annotations

from pathlib import Path

from settings import credentials_file_path, load_settings, store_credentials


def test_loads_stored_credentials_from_fallback_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("settings._load_from_keyring", lambda: None)
    store_credentials("api-id", "secret", 2)

    loaded = load_settings()

    assert loaded.darujme_api_id == "api-id"
    assert loaded.darujme_api_secret is not None
    assert loaded.darujme_api_secret.get_secret_value() == "secret"
    assert loaded.darujme_organization_id == 2
    assert credentials_file_path().stat().st_mode & 0o777 == 0o600
