from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from models import (
    DarujmeAmounts,
    DarujmeDates,
    DarujmeDonor,
    DarujmePledge,
    DarujmeProject,
    DarujmeProjectRef,
    DarujmePromotion,
    DarujmePromotionRef,
    DarujmeStates,
    DarujmeTransaction,
    Money,
    PledgeSummary,
)


def normalize_money(raw: Any) -> Money | None:
    if not isinstance(raw, dict):
        return None
    cents = _int_or_none(raw.get("cents"))
    currency = _string_or_none(raw.get("currency"))
    amount = None
    if cents is not None:
        amount = str((Decimal(cents) / Decimal(100)).quantize(Decimal("0.01")))
    return Money(cents=cents, currency=currency, amount=amount)


def normalize_transaction(
    raw: dict[str, Any],
    *,
    include_donor_pii: bool,
    include_raw: bool,
) -> DarujmeTransaction:
    transaction_id = _required_int(raw.get("transactionId"), "transactionId")
    pledge = raw.get("pledge") if isinstance(raw.get("pledge"), dict) else None
    project_id = _int_or_none(pledge.get("projectId")) if pledge else None
    promotion_id = _int_or_none(pledge.get("promotionId")) if pledge else None
    organization_id = _int_or_none(raw.get("organizationId"))
    if organization_id is None and pledge is not None:
        organization_id = _int_or_none(pledge.get("organizationId"))

    sent_amount = normalize_money(raw.get("sentAmount"))
    outgoing_amount = normalize_money(raw.get("outgoingAmount"))
    return DarujmeTransaction(
        source_id=transaction_id,
        source_key=f"darujme:transaction:{transaction_id}",
        source_number=_string_or_none(raw.get("presentableCode")),
        organization_id=organization_id,
        transaction_id=transaction_id,
        presentable_code=_string_or_none(raw.get("presentableCode")),
        state=_string_or_none(raw.get("state")),
        sent_amount=sent_amount,
        received_at=_string_or_none(raw.get("receivedAt")),
        outgoing_amount=outgoing_amount,
        outgoing_variable_symbol=_string_or_none(raw.get("outgoingVs")),
        outgoing_bank_account=_string_or_none(raw.get("outgoingBankAccount")),
        last_modified_at=_string_or_none(raw.get("lastModifiedDateTime")),
        dates=DarujmeDates(
            received_at=_string_or_none(raw.get("receivedAt")),
            last_modified_at=_string_or_none(raw.get("lastModifiedDateTime")),
            pledged_at=_string_or_none(pledge.get("pledgedAt")) if pledge else None,
        ),
        amounts=DarujmeAmounts(
            sent=sent_amount,
            outgoing=outgoing_amount,
            pledged=normalize_money(pledge.get("pledgedAmount")) if pledge else None,
        ),
        states=DarujmeStates(state=_string_or_none(raw.get("state"))),
        project=DarujmeProjectRef(project_id=project_id) if project_id is not None else None,
        promotion=DarujmePromotionRef(promotion_id=promotion_id, project_id=project_id)
        if promotion_id is not None
        else None,
        donor=normalize_donor(
            pledge.get("donor") if pledge else None, include_pii=include_donor_pii
        ),
        pledge=normalize_pledge_summary(pledge) if pledge else None,
        raw=raw if include_raw else None,
    )


def normalize_pledge(
    raw: dict[str, Any],
    *,
    include_donor_pii: bool,
    include_raw: bool,
) -> DarujmePledge:
    pledge_id = _required_int(raw.get("pledgeId"), "pledgeId")
    project_id = _int_or_none(raw.get("projectId"))
    promotion_id = _int_or_none(raw.get("promotionId"))
    transactions = raw.get("transactions")
    transaction_records = (
        [
            normalize_transaction(
                {**transaction, "pledge": raw},
                include_donor_pii=include_donor_pii,
                include_raw=include_raw,
            )
            for transaction in transactions
            if isinstance(transaction, dict)
        ]
        if isinstance(transactions, list)
        else []
    )
    return DarujmePledge(
        source_id=pledge_id,
        source_key=f"darujme:pledge:{pledge_id}",
        source_number=_string_or_none(raw.get("pledgeId")),
        organization_id=_int_or_none(raw.get("organizationId")),
        pledge_id=pledge_id,
        project=DarujmeProjectRef(project_id=project_id) if project_id is not None else None,
        promotion=DarujmePromotionRef(promotion_id=promotion_id, project_id=project_id)
        if promotion_id is not None
        else None,
        dates=DarujmeDates(
            pledged_at=_string_or_none(raw.get("pledgedAt")),
            last_modified_at=_string_or_none(raw.get("lastModifiedDateTime")),
        ),
        amounts=DarujmeAmounts(pledged=normalize_money(raw.get("pledgedAmount"))),
        states=DarujmeStates(
            payment_method=_string_or_none(raw.get("paymentMethod")),
            recurrent_state=_string_or_none(raw.get("recurrentState")),
            is_recurrent=_bool_or_none(raw.get("isRecurrent")),
            want_donation_certificate=_bool_or_none(raw.get("wantDonationCertificate")),
        ),
        donor=normalize_donor(raw.get("donor"), include_pii=include_donor_pii),
        transactions=transaction_records,
        comment=_string_or_none(raw.get("comment")) if include_donor_pii else None,
        raw=raw if include_raw else None,
    )


def normalize_project(raw: dict[str, Any], *, include_raw: bool) -> DarujmeProject:
    project_id = _required_int(raw.get("projectId"), "projectId")
    organization = raw.get("organization") if isinstance(raw.get("organization"), dict) else {}
    return DarujmeProject(
        source_id=project_id,
        source_key=f"darujme:project:{project_id}",
        source_number=str(project_id),
        project_id=project_id,
        organization_id=_int_or_none(organization.get("organizationId")),
        organization=organization,
        title=_dict_of_strings(raw.get("title")),
        synopsis=_dict_of_strings(raw.get("synopsis")),
        content=_dict_of_strings(raw.get("content")),
        donate_url=_string_or_none(raw.get("donateUrl")),
        dates=DarujmeDates(active_until=_string_or_none(raw.get("activeUntil"))),
        amounts=DarujmeAmounts(
            collected_estimate=normalize_money(raw.get("collectedAmountEstimate")),
            target=normalize_money(raw.get("targetAmount")),
        ),
        donors_count=_int_or_none(raw.get("donorsCount")),
        tags=_list_of_dicts(raw.get("tags")),
        raw=raw if include_raw else None,
    )


def normalize_promotion(raw: dict[str, Any], *, include_raw: bool) -> DarujmePromotion:
    promotion_id = _required_int(raw.get("promotionId"), "promotionId")
    organization = raw.get("organization") if isinstance(raw.get("organization"), dict) else {}
    return DarujmePromotion(
        source_id=promotion_id,
        source_key=f"darujme:promotion:{promotion_id}",
        source_number=str(promotion_id),
        promotion_id=promotion_id,
        project_id=_int_or_none(raw.get("projectId")),
        organization_id=_int_or_none(organization.get("organizationId")),
        organization=organization,
        title=_dict_of_strings(raw.get("title")),
        synopsis=_dict_of_strings(raw.get("synopsis")),
        content=_dict_of_strings(raw.get("content")),
        donate_url=_string_or_none(raw.get("donateUrl")),
        dates=DarujmeDates(active_until=_string_or_none(raw.get("activeUntil"))),
        amounts=DarujmeAmounts(
            collected_estimate=normalize_money(raw.get("collectedAmountEstimate")),
            target=normalize_money(raw.get("targetAmount")),
        ),
        donors_count=_int_or_none(raw.get("donorsCount")),
        tags=_list_of_dicts(raw.get("tags")),
        raw=raw if include_raw else None,
    )


def normalize_pledge_summary(raw: dict[str, Any]) -> PledgeSummary:
    return PledgeSummary(
        pledge_id=_int_or_none(raw.get("pledgeId")),
        organization_id=_int_or_none(raw.get("organizationId")),
        project_id=_int_or_none(raw.get("projectId")),
        promotion_id=_int_or_none(raw.get("promotionId")),
        payment_method=_string_or_none(raw.get("paymentMethod")),
        recurrent_state=_string_or_none(raw.get("recurrentState")),
        pledged_amount=normalize_money(raw.get("pledgedAmount")),
        pledged_at=_string_or_none(raw.get("pledgedAt")),
    )


def normalize_donor(raw: Any, *, include_pii: bool) -> DarujmeDonor:
    if not include_pii or not isinstance(raw, dict):
        return DarujmeDonor(redacted=True)
    first_name = _string_or_none(raw.get("firstName"))
    last_name = _string_or_none(raw.get("lastName"))
    name = " ".join(part for part in [first_name, last_name] if part) or None
    address = raw.get("address") if isinstance(raw.get("address"), dict) else {}
    custom_fields = raw.get("customFields") if isinstance(raw.get("customFields"), dict) else {}
    confirmation = (
        raw.get("confirmationRecipient")
        if isinstance(raw.get("confirmationRecipient"), dict)
        else {}
    )
    return DarujmeDonor(
        redacted=False,
        first_name=first_name,
        last_name=last_name,
        name=name,
        email=_string_or_none(raw.get("email")),
        phone=_string_or_none(raw.get("phone")),
        address=address,
        company_name=_string_or_none(raw.get("companyName")),
        company_identification_number=_string_or_none(raw.get("companyIdentificationNumber")),
        company_vat_identification_number=_string_or_none(
            raw.get("companyVatIdentificationNumber")
        ),
        custom_fields=custom_fields,
        confirmation_recipient=confirmation,
    )


def _required_int(value: Any, field: str) -> int:
    result = _int_or_none(value)
    if result is None:
        raise ValueError(f"Darujme payload is missing {field}")
    return result


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value)
    string = str(value).strip()
    return string or None


def _dict_of_strings(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items() if item is not None}


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
