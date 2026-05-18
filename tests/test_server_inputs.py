from __future__ import annotations

from datetime import date

import pytest
import respx
from httpx import Response
from pydantic import TypeAdapter

from client import DarujmeClient
from models import FoundItemError
from normalization import normalize_transaction
from server import (
    FindPledgesQuery,
    FindProjectsQuery,
    FindPromotionsQuery,
    FindTransactionsQuery,
    SearchCursor,
    SettlementAggregateQuery,
    TransactionSearchQuery,
    _control_totals,
    _encode_cursor,
    _find_projects,
    _find_transactions,
    _pledge_params,
    _transaction_params,
)
from settings import DarujmeCredentials, Settings
from tests.fixtures import sample_project, sample_transaction


def settings() -> Settings:
    return Settings(_env_file=None, DARUJME_TIMEOUT_SECONDS=10)


def credentials() -> DarujmeCredentials:
    return DarujmeCredentials(api_id=42, api_secret="secret", organization_id=2)


def _client() -> DarujmeClient:
    return DarujmeClient(settings(), credentials())


def test_query_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError):
        TypeAdapter(FindTransactionsQuery).validate_python(
            {"query_type": "transaction_search", "token": "must-not-accept"}
        )


def test_transaction_query_schema_is_discriminated_union() -> None:
    schema = TypeAdapter(FindTransactionsQuery).json_schema()

    assert schema["discriminator"]["propertyName"] == "query_type"
    assert len(schema["oneOf"]) == 3


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
    query = TransactionSearchQuery(
        query_type="transaction_search",
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
        TypeAdapter(FindTransactionsQuery).validate_python(
            {"query_type": "transaction_search", "from_received_date": "2026-05-01"}
        )


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
    client = _client()

    result = await _find_transactions(
        client,
        TransactionSearchQuery(
            query_type="transaction_search",
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
    client = _client()
    original = TransactionSearchQuery(
        query_type="transaction_search", received_from=date(2026, 5, 1)
    )
    cursor = _encode_cursor(
        SearchCursor(kind="transactions", offset=100, filter_hash=original.filter_hash())
    )
    changed = TransactionSearchQuery(
        query_type="transaction_search", received_from=date(2026, 5, 2), cursor=cursor
    )

    result = await _find_transactions(client, changed)
    await client.aclose()

    assert result.error is not None
    assert result.error.code == "cursor_mismatch"


@respx.mock
async def test_settlement_aggregate_groups_one_day_outgoing_transactions() -> None:
    respx.get("https://www.darujme.cz/api/v1/organization/2/transactions-by-filter").mock(
        return_value=Response(
            200,
            json={
                "transactions": [
                    sample_transaction(
                        transactionId=6921582,
                        sentAmount={"cents": 100000, "currency": "CZK"},
                        outgoingAmount={"cents": 96300, "currency": "CZK"},
                        outgoingVs="260310661",
                        outgoingBankAccount="2603445200/2010",
                    ),
                    sample_transaction(
                        transactionId=6921374,
                        sentAmount={"cents": 100000, "currency": "CZK"},
                        outgoingAmount={"cents": 96300, "currency": "CZK"},
                        outgoingVs="260310661",
                        outgoingBankAccount="2603445200/2010",
                    ),
                    sample_transaction(
                        transactionId=6932731,
                        sentAmount={"cents": 10000, "currency": "CZK"},
                        outgoingAmount={"cents": 9630, "currency": "CZK"},
                        outgoingVs="260317516",
                        outgoingBankAccount="2603445200/2010",
                    ),
                ]
            },
        )
    )
    client = _client()

    result = await _find_transactions(
        client,
        SettlementAggregateQuery(
            query_type="settlement_aggregate",
            settled_from=date(2026, 3, 10),
            settled_to=date(2026, 3, 10),
            variable_symbol="260310661",
            currency="CZK",
        ),
    )
    await client.aclose()

    assert result.error is None
    assert len(result.settlements) == 1
    settlement = result.settlements[0]
    assert settlement.model_dump(exclude_none=True) == {
        "date": "2026-03-10",
        "bank_account": "2603445200/2010",
        "variable_symbol": "260310661",
        "currency": "CZK",
        "amount": "1926.00",
        "sent_total": "2000.00",
        "fee_total": "74.00",
        "transaction_count": 2,
        "transaction_ids": [6921582, 6921374],
    }
    assert settlement.sent_total == "2000.00"
    assert settlement.fee_total == "74.00"
    assert settlement.transaction_ids == [6921582, 6921374]


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
    client = _client()

    result = await _find_projects(client, FindProjectsQuery(mode="by_ids", ids=[4563, 9999]))
    await client.aclose()

    assert result.projects[0].project_id == 4563
    assert isinstance(result.projects[1], FoundItemError)
    assert result.projects[1].error.code == "not_found"
