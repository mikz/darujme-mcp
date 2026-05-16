from __future__ import annotations

import pytest

from normalization import normalize_pledge, normalize_transaction
from server import FindTransactionsQuery
from tests.fixtures import sample_pledge, sample_transaction


def test_transaction_redacts_donor_pii_by_default() -> None:
    transaction = normalize_transaction(
        sample_transaction(),
        include_donor_pii=False,
        include_raw=False,
    )

    assert transaction.donor.redacted is True
    assert transaction.donor.name is None
    assert transaction.donor.email is None
    assert transaction.raw is None
    assert transaction.pledge is not None
    assert transaction.pledge.pledge_id == 1203450
    assert transaction.outgoing_variable_symbol == "990001"


def test_transaction_includes_pii_and_raw_only_when_requested() -> None:
    raw = sample_transaction()
    transaction = normalize_transaction(raw, include_donor_pii=True, include_raw=True)

    assert transaction.donor.redacted is False
    assert transaction.donor.name == "Jana Novakova"
    assert transaction.donor.email == "jana@example.org"
    assert transaction.donor.custom_fields == {"rodne_cislo": "secret"}
    assert transaction.raw == raw


def test_pledge_redacts_comment_and_donor_without_pii() -> None:
    pledge = normalize_pledge(sample_pledge(), include_donor_pii=False, include_raw=False)

    assert pledge.comment is None
    assert pledge.donor.email is None


def test_include_raw_requires_donor_pii() -> None:
    with pytest.raises(ValueError, match="include_raw requires include_donor_pii"):
        FindTransactionsQuery.model_validate({"mode": "search", "include_raw": True})
