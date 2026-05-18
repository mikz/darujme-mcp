from __future__ import annotations

import os
from contextlib import suppress
from hashlib import sha256
from pathlib import Path

from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

KEYRING_SERVICE = "darujme-mcp"
KEYRING_API_ID_ACCOUNT = "api_id"
KEYRING_API_SECRET_ACCOUNT = "api_secret"
KEYRING_ORGANIZATION_ID_ACCOUNT = "organization_id"
CREDENTIAL_SCOPE_ID_LENGTH = 16


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    darujme_api_id: str | None = Field(default=None, alias="DARUJME_API_ID")
    darujme_api_secret: SecretStr | None = Field(default=None, alias="DARUJME_API_SECRET")
    darujme_organization_id: int | None = Field(default=None, alias="DARUJME_ORGANIZATION_ID")
    darujme_base_url: AnyHttpUrl = Field(
        default="https://www.darujme.cz/api/v1/",
        alias="DARUJME_BASE_URL",
    )
    darujme_timeout_seconds: float = Field(
        default=30.0,
        alias="DARUJME_TIMEOUT_SECONDS",
        gt=0,
    )


class DarujmeCredentials(BaseModel):
    """Validated Darujme API credentials. Construction fails for non-integer api_id or organization_id."""

    api_id: int = Field(ge=1, description="Darujme API key ID (integer assigned by Darujme)")
    api_secret: SecretStr = Field(description="Darujme API secret")
    organization_id: int = Field(ge=1, description="Darujme organization ID")


def credentials_scoped_to_cwd() -> bool:
    raw = os.environ.get("DARUJME_SCOPED_CREDENTIALS", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def credentials_file_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    root = Path(base) / "darujme-mcp"
    if credentials_scoped_to_cwd():
        return root / "scopes" / credential_scope_id() / "credentials.env"
    return root / "credentials.env"


def credential_scope_cwd() -> Path:
    return Path.cwd().resolve()


def credential_scope_id() -> str:
    scope = str(credential_scope_cwd()).encode("utf-8")
    return sha256(scope).hexdigest()[:CREDENTIAL_SCOPE_ID_LENGTH]


def keyring_service_name() -> str:
    if credentials_scoped_to_cwd():
        return f"{KEYRING_SERVICE}:{credential_scope_id()}"
    return KEYRING_SERVICE


def _load_from_keyring() -> DarujmeCredentials | None:
    try:
        import keyring
    except Exception:
        return None
    try:
        service = keyring_service_name()
        api_id = keyring.get_password(service, KEYRING_API_ID_ACCOUNT)
        api_secret = keyring.get_password(service, KEYRING_API_SECRET_ACCOUNT)
        organization_id = keyring.get_password(service, KEYRING_ORGANIZATION_ID_ACCOUNT)
    except Exception:
        return None
    if not (api_id and api_secret and organization_id):
        return None
    try:
        return DarujmeCredentials(
            api_id=api_id,
            api_secret=api_secret,
            organization_id=organization_id,
        )
    except ValidationError:
        return None


def _load_from_file() -> DarujmeCredentials | None:
    cfg = credentials_file_path()
    if not cfg.is_file():
        return None
    data: dict[str, str] = {}
    try:
        lines = cfg.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        data[key.strip()] = value.strip()
    api_id = data.get("DARUJME_API_ID") or data.get("api_id")
    api_secret = data.get("DARUJME_API_SECRET") or data.get("api_secret")
    organization_id = data.get("DARUJME_ORGANIZATION_ID") or data.get("organization_id")
    if not (api_id and api_secret and organization_id):
        return None
    try:
        return DarujmeCredentials(
            api_id=api_id,
            api_secret=api_secret,
            organization_id=organization_id,
        )
    except ValidationError:
        return None


def _credentials_from_settings(settings: Settings) -> DarujmeCredentials | None:
    if not (
        settings.darujme_api_id
        and settings.darujme_api_secret
        and settings.darujme_organization_id is not None
    ):
        return None
    try:
        return DarujmeCredentials(
            api_id=settings.darujme_api_id,
            api_secret=settings.darujme_api_secret,
            organization_id=settings.darujme_organization_id,
        )
    except ValidationError:
        return None


def load_credentials(settings: Settings) -> DarujmeCredentials | None:
    """Resolve credentials in priority order: env (via Settings) → keyring → file. Invalid sources skip to next."""
    return (
        _credentials_from_settings(settings)
        or _load_from_keyring()
        or _load_from_file()
    )


def store_credentials(credentials: DarujmeCredentials) -> None:
    api_id_str = str(credentials.api_id)
    api_secret_str = credentials.api_secret.get_secret_value()
    organization_id_str = str(credentials.organization_id)

    try:
        import keyring

        service = keyring_service_name()
        keyring.set_password(service, KEYRING_API_ID_ACCOUNT, api_id_str)
        keyring.set_password(service, KEYRING_API_SECRET_ACCOUNT, api_secret_str)
        keyring.set_password(service, KEYRING_ORGANIZATION_ID_ACCOUNT, organization_id_str)
    except Exception:
        pass

    cfg = credentials_file_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        "\n".join(
            [
                f"DARUJME_API_ID={api_id_str}",
                f"DARUJME_API_SECRET={api_secret_str}",
                f"DARUJME_ORGANIZATION_ID={organization_id_str}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    with suppress(OSError):
        cfg.chmod(0o600)


def load_settings() -> Settings:
    return Settings()
