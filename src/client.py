from __future__ import annotations

from typing import Any

import httpx
from pydantic import SecretStr

from settings import Settings


class DarujmeError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: int | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.payload = payload


class NotAuthenticatedError(DarujmeError):
    def __init__(self) -> None:
        super().__init__(
            "Darujme credentials not configured. Call darujme_login first.",
            code="not_configured",
        )


class DarujmeClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_url = str(settings.darujme_base_url).rstrip("/") + "/"
        self._api_id = settings.darujme_api_id
        self._api_secret = settings.darujme_api_secret
        self._organization_id = settings.darujme_organization_id
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(settings.darujme_timeout_seconds),
            follow_redirects=False,
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def organization_id(self) -> int | None:
        return self._organization_id

    def is_authenticated(self) -> bool:
        return bool(self._api_id and self._api_secret and self._organization_id is not None)

    async def aclose(self) -> None:
        await self._client.aclose()

    def set_credentials_sync(self, api_id: str, api_secret: str, organization_id: int) -> None:
        self._api_id = api_id
        self._api_secret = SecretStr(api_secret)
        self._organization_id = organization_id
        self._settings.darujme_api_id = api_id
        self._settings.darujme_api_secret = SecretStr(api_secret)
        self._settings.darujme_organization_id = organization_id

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        self._require_credentials()
        response = await self._client.request(
            method,
            path.lstrip("/"),
            params=self._auth_params(params),
        )
        return _parse_response(response)

    async def test_connection(self) -> Any:
        organization_id = self._require_organization_id()
        return await self.request("GET", f"organization/{organization_id}/projects", params={})

    async def get_transaction(self, transaction_id: int) -> dict[str, Any]:
        organization_id = self._require_organization_id()
        payload = await self.request(
            "GET", f"organization/{organization_id}/transaction/{transaction_id}"
        )
        return _expect_object_key(payload, "transaction")

    async def search_transactions(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        organization_id = self._require_organization_id()
        payload = await self.request(
            "GET",
            f"organization/{organization_id}/transactions-by-filter",
            params=_clean_params(params),
        )
        return _expect_list_key(payload, "transactions")

    async def get_pledge(self, pledge_id: int) -> dict[str, Any]:
        organization_id = self._require_organization_id()
        payload = await self.request("GET", f"organization/{organization_id}/pledge/{pledge_id}")
        return _expect_object_key(payload, "pledge")

    async def search_pledges(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        organization_id = self._require_organization_id()
        payload = await self.request(
            "GET",
            f"organization/{organization_id}/pledges-by-filter",
            params=_clean_params(params),
        )
        return _expect_list_key(payload, "pledges")

    async def pledges_by_vs(self, vs: str) -> list[dict[str, Any]]:
        organization_id = self._require_organization_id()
        payload = await self.request("GET", f"organization/{organization_id}/pledges-by-vs/{vs}")
        return _expect_list_key(payload, "pledges")

    async def get_project(self, project_id: int) -> dict[str, Any]:
        payload = await self.request("GET", f"project/{project_id}")
        return _expect_object_key(payload, "project")

    async def list_projects(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        organization_id = self._require_organization_id()
        payload = await self.request(
            "GET",
            f"organization/{organization_id}/projects",
            params=_clean_params(params),
        )
        return _expect_list_key(payload, "projects")

    async def get_promotion(self, promotion_id: int) -> dict[str, Any]:
        payload = await self.request("GET", f"promotion/{promotion_id}")
        return _expect_object_key(payload, "promotion")

    async def list_promotions(self, project_id: int) -> list[dict[str, Any]]:
        payload = await self.request("GET", f"project/{project_id}/promotions")
        return _expect_list_key(payload, "promotions")

    def _auth_params(self, params: dict[str, Any] | None) -> dict[str, Any]:
        self._require_credentials()
        assert self._api_id is not None
        assert self._api_secret is not None
        merged = dict(params or {})
        try:
            merged["apiId"] = int(self._api_id)
        except (TypeError, ValueError) as exc:
            raise DarujmeError(
                f"darujme_api_id must be an integer (got {self._api_id!r})",
                code="invalid_api_id",
            ) from exc
        merged["apiSecret"] = self._api_secret.get_secret_value()
        return merged

    def _require_credentials(self) -> None:
        if not self._api_id or not self._api_secret or self._organization_id is None:
            raise NotAuthenticatedError()

    def _require_organization_id(self) -> int:
        self._require_credentials()
        assert self._organization_id is not None
        return self._organization_id


def _parse_response(response: httpx.Response) -> Any:
    try:
        payload = response.json()
    except ValueError:
        payload = response.text
    if response.status_code >= 400:
        raise _darujme_error(response.status_code, payload)
    return payload


def _darujme_error(status_code: int, payload: Any) -> DarujmeError:
    code = "darujme_error"
    message = f"Darujme API request failed: HTTP {status_code}"
    if status_code in {401, 403}:
        code = "auth_error"
        message = "Darujme rejected the credentials"
    elif status_code == 404:
        code = "not_found"
    elif status_code == 422:
        code = "invalid_request"
    elif status_code >= 500:
        code = "darujme_internal_error"
    if isinstance(payload, dict):
        candidate = payload.get("message") or payload.get("error") or payload.get("errorMessage")
        if candidate:
            message = str(candidate)
    elif isinstance(payload, str) and payload.strip():
        message = payload.strip()[:500]
    return DarujmeError(message, code=code, status_code=status_code, payload=payload)


def _expect_object_key(payload: Any, key: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get(key), dict):
        raise DarujmeError(
            f"Darujme response did not contain object key {key}",
            code="invalid_response",
            payload=payload,
        )
    return payload[key]


def _expect_list_key(payload: Any, key: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
        raise DarujmeError(
            f"Darujme response did not contain list key {key}",
            code="invalid_response",
            payload=payload,
        )
    return [item for item in payload[key] if isinstance(item, dict)]


def _clean_params(params: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, list):
            if value:
                clean[key] = value
            continue
        clean[key] = value
    return clean
