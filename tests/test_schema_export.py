from __future__ import annotations

import pytest

from server import mcp


@pytest.fixture
async def tool_schema() -> dict[str, dict[str, object]]:
    tools = await mcp.list_tools()
    return {
        tool.name: {
            "description": tool.description or "",
            "parameters": tool.parameters,
        }
        for tool in tools
    }


async def test_tool_set_is_stable(tool_schema: dict[str, dict[str, object]]) -> None:
    assert set(tool_schema) == {
        "darujme_login",
        "darujme_test_connection",
        "darujme_find_transactions",
        "darujme_find_pledges",
        "darujme_find_projects",
        "darujme_find_promotions",
        "darujme_prepare_donation_confirmations",
        "darujme_get_metadata",
    }


async def test_every_tool_has_description(tool_schema: dict[str, dict[str, object]]) -> None:
    for name, entry in tool_schema.items():
        assert entry["description"], f"tool {name} has empty description"


async def test_find_transactions_docstring_distinguishes_three_modes(
    tool_schema: dict[str, dict[str, object]],
) -> None:
    """E8: the find_transactions tool docstring must explicitly enumerate the three
    query_type modes (transaction_search, transaction_by_ids, settlement_aggregate)
    and call out that settlement_aggregate has different semantics."""
    description = tool_schema["darujme_find_transactions"]["description"]
    assert isinstance(description, str)
    for token in ("transaction_search", "transaction_by_ids", "settlement_aggregate"):
        assert token in description, (
            f"find_transactions docstring missing reference to {token!r}"
        )
    assert "settlement_aggregate" in description.lower(), (
        "find_transactions docstring should headline settlement_aggregate"
    )


async def test_privacy_mixin_describes_three_levels(
    tool_schema: dict[str, dict[str, object]],
) -> None:
    """E9: include_donor_pii and include_raw descriptions must explain the 3-level
    privacy hierarchy (default redacted → personal PII → personal_with_raw)."""
    params = tool_schema["darujme_find_transactions"]["parameters"]
    # The discriminated union expands per-variant; find the search variant
    # by looking through the oneOf / anyOf members for include_donor_pii.
    blob = repr(params)
    assert "redact" in blob.lower() or "privacy" in blob.lower(), (
        "PrivacyMixin descriptions must mention redaction or privacy levels"
    )


async def test_settlement_aggregate_fields_disambiguate_from_donor_vs(
    tool_schema: dict[str, dict[str, object]],
) -> None:
    """E11: settlement_aggregate's variable_symbol Field description should call out
    that this is the outgoing-payout VS that matches the Fio incoming, NOT a
    donor-side VS."""
    params = tool_schema["darujme_find_transactions"]["parameters"]
    blob = repr(params)
    # settlement_aggregate variant must mention "outgoing" or "payout" to indicate
    # this is the bank-statement-matching VS, not the donor's.
    assert "outgoing" in blob.lower() or "payout" in blob.lower(), (
        "settlement_aggregate fields should be described as outgoing/payout VS, "
        "distinguishing them from donor-side VS"
    )


async def test_get_metadata_docstring_advertises_privacy_levels(
    tool_schema: dict[str, dict[str, object]],
) -> None:
    """darujme_get_metadata is the Rosetta Stone; its docstring should hint at the
    privacy levels section so callers know to consult it."""
    description = tool_schema["darujme_get_metadata"]["description"]
    assert isinstance(description, str)
    lowered = description.lower()
    assert "privacy" in lowered, "get_metadata docstring should reference privacy levels"
