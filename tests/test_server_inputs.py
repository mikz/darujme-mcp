from __future__ import annotations

from datetime import date

import pytest
import respx
from httpx import Response

from client import DarujmeClient
from models import FoundItemError
from normalization import normalize_transaction
from server import (
    FindPledgesQuery,
    FindProjectsQuery,
    FindPromotionsQuery,
    FindTransactionsQuery,
    SearchCursor,
    _control_totals,
    _encode_cursor,
    _filter_transactions,
    _find_projects,
    _find_transactions,
    _pledge_params,
    _transaction_params,
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


def test_transaction_search_uses_consistent_date_names() -> None:
    query = FindTransactionsQuery(
        mode="search",
        received_from=date(2026, 5, 1),
        received_to=date(2026, 5, 2),
        outgoing_from=date(2026, 5, 3),
        outgoing_to=date(2026, 5, 4),
        failed_from=date(2026, 5, 5),
        failed_to=date(2026, 5, 6),
    )

    params = _transaction_params(query, offset=10)

    assert params["fromReceivedDate"] == "2026-05-01"
    assert params["toReceivedDate"] == "2026-05-02"
    assert params["fromOutgoingDate"] == "2026-05-03"
    assert params["toOutgoingDate"] == "2026-05-04"
    assert params["fromFailedDate"] == "2026-05-05"
    assert params["toFailedDate"] == "2026-05-06"
    assert params["offset"] == 10


def test_old_transaction_date_aliases_are_rejected() -> None:
    with pytest.raises(ValueError):
        FindTransactionsQuery.model_validate({"mode": "search", "from_received_date": "2026-05-01"})


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
            received_from=date(2026, 5, 1),
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
    original = FindTransactionsQuery(mode="search", received_from=date(2026, 5, 1))
    cursor = _encode_cursor(
        SearchCursor(kind="transactions", offset=100, filter_hash=original.filter_hash())
    )
    changed = FindTransactionsQuery(mode="search", received_from=date(2026, 5, 2), cursor=cursor)

    result = await _find_transactions(client, changed)
    await client.aclose()

    assert result.error is not None
    assert result.error.code == "cursor_mismatch"


def test_outgoing_filters_are_local_and_named_like_response_fields() -> None:
    first = sample_transaction(
        transactionId=1,
        outgoingVs="260310661",
        outgoingAmount={"cents": 192600, "currency": "CZK"},
        outgoingBankAccount="123456789/0800",
    )
    second = sample_transaction(
        transactionId=2,
        outgoingVs="other",
        outgoingAmount={"cents": 9630, "currency": "CZK"},
        outgoingBankAccount="987654321/0300",
    )
    query = FindTransactionsQuery(
        mode="search",
        outgoing_variable_symbol="260310661",
        outgoing_amount="1926.00",
        outgoing_currency="CZK",
        outgoing_bank_account="123456789/0800",
    )

    records = [
        normalize_transaction(first, include_donor_pii=False, include_raw=False),
        normalize_transaction(second, include_donor_pii=False, include_raw=False),
    ]

    filtered = _filter_transactions(records, query)

    assert [record.transaction_id for record in filtered] == [1]
    assert filtered[0].outgoing_variable_symbol == "260310661"


def test_transaction_control_totals_split_sent_and_outgoing_amounts() -> None:
    record = normalize_transaction(
        sample_transaction(
            sentAmount={"cents": 200000, "currency": "CZK"},
            outgoingAmount={"cents": 192600, "currency": "CZK"},
        ),
        include_donor_pii=False,
        include_raw=False,
    )

    totals = _control_totals([record])

    assert totals.sent_by_currency["CZK"] == {"count": 1, "amount": "2000.00"}
    assert totals.outgoing_by_currency["CZK"] == {"count": 1, "amount": "1926.00"}


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
