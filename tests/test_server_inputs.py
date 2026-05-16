from __future__ import annotations

from datetime import date

import pytest
import respx
from fastmcp import Client
from httpx import Response

import server as server_module
from client import DarujmeClient
from models import FoundItemError
from server import (
    DarujmeLogin,
    FindPledgesQuery,
    FindProjectsQuery,
    FindPromotionsQuery,
    FindTransactionsQuery,
    SearchCursor,
    _encode_cursor,
    _find_projects,
    _find_transactions,
    _metadata_result,
    _pledge_params,
)
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


def test_query_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError):
        FindTransactionsQuery.model_validate({"mode": "search", "token": "must-not-accept"})


def test_by_id_modes_require_ids() -> None:
    with pytest.raises(ValueError, match="ids are required"):
        FindProjectsQuery(mode="by_ids")
    with pytest.raises(ValueError, match="project_id or project_ids"):
        FindPromotionsQuery(mode="search")


def test_login_contract_requires_organization_id() -> None:
    schema = DarujmeLogin.model_json_schema()

    assert schema["required"] == ["api_id", "api_secret", "organization_id"]
    assert "does not expose" in schema["properties"]["organization_id"]["description"]


def test_pledge_search_uses_documented_project_filter() -> None:
    query = FindPledgesQuery(
        mode="search",
        project_ids=[4563],
        from_pledged_date=date(2026, 5, 1),
    )

    params = _pledge_params(query, offset=25)

    assert params["projectId"] == 4563
    assert "projectIds[]" not in params
    assert params["fromPledgedDate"] == "2026-05-01"
    assert params["offset"] == 25


async def test_exposed_login_is_unified_login_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_module, "load_settings", settings)

    async with Client(server_module.mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert "darujme_login" in tools
    properties = tools["darujme_login"].inputSchema["properties"]
    assert properties["mode"]["enum"] == ["auto", "direct", "prefab", "web"]
    assert set(properties) == {"mode", "credentials"}
    credentials_schema = properties["credentials"]["anyOf"][0]
    assert credentials_schema["required"] == ["api_id", "api_secret", "organization_id"]
    assert "api_secret" in credentials_schema["properties"]


@respx.mock
async def test_find_transactions_pages_with_opaque_cursor() -> None:
    respx.get("https://www.darujme.cz/api/v1/organization/2/transactions-by-filter").mock(
        return_value=Response(
            200,
            json={
                "transactions": [
                    sample_transaction(transactionId=1),
                    sample_transaction(transactionId=2),
                ]
            },
        )
    )
    client = DarujmeClient(settings())

    result = await _find_transactions(
        client,
        FindTransactionsQuery(
            mode="search",
            from_received_date=date(2026, 5, 1),
            limit=2,
        ),
    )
    await client.aclose()

    assert [
        item.transaction_id for item in result.transactions if not isinstance(item, FoundItemError)
    ] == [1, 2]
    assert result.next_cursor is not None
    assert result.control_totals is not None
    assert result.control_totals.count == 2


async def test_cursor_filter_drift_is_rejected() -> None:
    client = DarujmeClient(settings())
    original = FindTransactionsQuery(mode="search", from_received_date=date(2026, 5, 1))
    cursor = _encode_cursor(
        SearchCursor(kind="transactions", offset=100, filter_hash=original.filter_hash())
    )
    changed = FindTransactionsQuery(
        mode="search", from_received_date=date(2026, 5, 2), cursor=cursor
    )

    result = await _find_transactions(client, changed)
    await client.aclose()

    assert result.error is not None
    assert result.error.code == "cursor_mismatch"


@respx.mock
async def test_find_projects_by_id_itemizes_errors() -> None:
    respx.get("https://www.darujme.cz/api/v1/project/4563").mock(
        return_value=Response(200, json={"project": sample_project()})
    )
    respx.get("https://www.darujme.cz/api/v1/project/9999").mock(
        return_value=Response(404, text="not found")
    )
    client = DarujmeClient(settings())

    result = await _find_projects(client, FindProjectsQuery(mode="by_ids", ids=[4563, 9999]))
    await client.aclose()

    assert result.projects[0].project_id == 4563
    assert isinstance(result.projects[1], FoundItemError)
    assert result.projects[1].error.code == "not_found"


def test_metadata_exposes_contract() -> None:
    metadata = _metadata_result()

    assert metadata.login_contract["required_fields"] == [
        "api_id",
        "api_secret",
        "organization_id",
    ]
    assert "organization discovery" in metadata.login_contract["organization_id_required_reason"]
    assert metadata.query_modes["darujme_find_pledges"] == ["search", "by_ids", "by_vs"]
    assert metadata.privacy["include_raw"] == "Requires include_donor_pii=true."
    assert {entry.code for entry in metadata.error_codes} >= {
        "auth_error",
        "invalid_cursor",
        "cursor_mismatch",
    }
