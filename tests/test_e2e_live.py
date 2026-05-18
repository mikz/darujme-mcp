from __future__ import annotations

import pytest

from client import DarujmeClient
from server import (
    FindProjectsQuery,
    TransactionSearchQuery,
    _find_projects,
    _find_transactions,
    _test_connection,
)
from settings import DarujmeCredentials, Settings, load_credentials

pytestmark = pytest.mark.e2e


def live_setup() -> tuple[Settings, DarujmeCredentials]:
    settings = Settings()
    credentials = load_credentials(settings)
    if credentials is None:
        pytest.skip(
            "Missing live Darujme credentials: set DARUJME_API_ID, DARUJME_API_SECRET, "
            "DARUJME_ORGANIZATION_ID via env, keyring, or ~/.config/darujme-mcp/credentials.env."
        )
    return settings, credentials


async def test_live_connection_and_read_only_searches() -> None:
    settings, credentials = live_setup()
    client = DarujmeClient(settings, credentials)
    try:
        connection = await _test_connection(client)
        assert connection.ok is True

        projects = await _find_projects(client, FindProjectsQuery(mode="search", limit=5))
        assert projects.error is None
        assert projects.control_totals is not None

        transactions = await _find_transactions(
            client, TransactionSearchQuery(query_type="transaction_search", limit=5)
        )
        assert transactions.error is None
        assert transactions.control_totals is not None
        assert all(
            getattr(item, "donor", None) is None or item.donor.redacted
            for item in transactions.transactions
        )
    finally:
        await client.aclose()


async def test_live_invalid_secret_returns_structured_auth_error() -> None:
    settings, credentials = live_setup()
    bad_credentials = credentials.model_copy(update={"api_secret": "invalid-secret"})
    client = DarujmeClient(settings, bad_credentials)
    try:
        result = await _test_connection(client)
        assert result.ok is False
        assert result.error is not None
        assert result.error.code in {"auth_error", "darujme_error", "invalid_request"}
    finally:
        await client.aclose()
