from __future__ import annotations

import pytest

from client import DarujmeClient
from server import (
    FindProjectsQuery,
    FindTransactionsQuery,
    _find_projects,
    _find_transactions,
    _test_connection,
)
from settings import Settings

pytestmark = pytest.mark.e2e


def live_settings() -> Settings:
    settings = Settings()
    missing = [
        name
        for name, value in [
            ("DARUJME_API_ID", settings.darujme_api_id),
            ("DARUJME_API_SECRET", settings.darujme_api_secret),
            ("DARUJME_ORGANIZATION_ID", settings.darujme_organization_id),
        ]
        if value in (None, "")
    ]
    if missing:
        pytest.skip(f"Missing live Darujme credentials: {', '.join(missing)}")
    return settings


async def test_live_connection_and_read_only_searches() -> None:
    client = DarujmeClient(live_settings())
    try:
        connection = await _test_connection(client)
        assert connection.ok is True

        projects = await _find_projects(client, FindProjectsQuery(mode="search", limit=5))
        assert projects.error is None
        assert projects.control_totals is not None

        transactions = await _find_transactions(
            client, FindTransactionsQuery(mode="search", limit=5)
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
    settings = live_settings()
    settings.darujme_api_secret = type(settings.darujme_api_secret)("invalid-secret")
    client = DarujmeClient(settings)
    try:
        result = await _test_connection(client)
        assert result.ok is False
        assert result.error is not None
        assert result.error.code in {"auth_error", "darujme_error", "invalid_request"}
    finally:
        await client.aclose()
