from __future__ import annotations

import pytest
import respx
from httpx import Response

from client import DarujmeClient, DarujmeError
from settings import Settings
from tests.fixtures import sample_project, sample_transaction


def settings() -> Settings:
    return Settings(
        _env_file=None,
        DARUJME_API_ID="api-id",
        DARUJME_API_SECRET="secret",
        DARUJME_ORGANIZATION_ID=2,
        DARUJME_TIMEOUT_SECONDS=10,
    )


@respx.mock
async def test_search_transactions_uses_auth_and_filter_params() -> None:
    route = respx.get("https://www.darujme.cz/api/v1/organization/2/transactions-by-filter").mock(
        return_value=Response(200, json={"transactions": [sample_transaction()]})
    )
    client = DarujmeClient(settings())

    result = await client.search_transactions(
        {
            "fromReceivedDate": "2026-05-01",
            "pageSize": 10,
            "offset": 20,
            "transactionState[]": ["success"],
        }
    )
    await client.aclose()

    assert result[0]["transactionId"] == 7654321
    assert route.called
    request = route.calls.last.request
    assert request.url.params["apiId"] == "api-id"
    assert request.url.params["apiSecret"] == "secret"
    assert request.url.params["fromReceivedDate"] == "2026-05-01"
    assert request.url.params["pageSize"] == "10"
    assert request.url.params["offset"] == "20"


@respx.mock
async def test_project_listing_shape() -> None:
    respx.get("https://www.darujme.cz/api/v1/organization/2/projects").mock(
        return_value=Response(200, json={"count": 1, "projects": [sample_project()]})
    )
    client = DarujmeClient(settings())

    result = await client.list_projects({"state": "active"})
    await client.aclose()

    assert result[0]["projectId"] == 4563


@respx.mock
async def test_maps_auth_error_without_leaking_secret() -> None:
    respx.get("https://www.darujme.cz/api/v1/organization/2/projects").mock(
        return_value=Response(403, text="forbidden")
    )
    client = DarujmeClient(settings())

    with pytest.raises(DarujmeError) as error:
        await client.test_connection()
    await client.aclose()

    assert error.value.code == "auth_error"
    assert "secret" not in str(error.value)
