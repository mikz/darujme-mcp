from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorInfo(BaseModel):
    code: str
    message: str


class Money(BaseModel):
    cents: int | None = None
    currency: str | None = None
    amount: str | None = None


class DarujmeDates(BaseModel):
    pledged_at: str | None = None
    received_at: str | None = None
    outgoing_at: str | None = None
    failed_at: str | None = None
    last_modified_at: str | None = None
    active_until: str | None = None


class DarujmeAmounts(BaseModel):
    pledged: Money | None = None
    sent: Money | None = None
    outgoing: Money | None = None
    collected_estimate: Money | None = None
    target: Money | None = None


class DarujmeStates(BaseModel):
    state: str | None = None
    payment_method: str | None = None
    recurrent_state: str | None = None
    is_recurrent: bool | None = None
    want_donation_certificate: bool | None = None


class DarujmeProjectRef(BaseModel):
    project_id: int | None = None
    title: dict[str, str] = Field(default_factory=dict)


class DarujmePromotionRef(BaseModel):
    promotion_id: int | None = None
    project_id: int | None = None
    title: dict[str, str] = Field(default_factory=dict)


class DarujmeDonor(BaseModel):
    redacted: bool = True
    first_name: str | None = None
    last_name: str | None = None
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    address: dict[str, Any] = Field(default_factory=dict)
    company_name: str | None = None
    company_identification_number: str | None = None
    company_vat_identification_number: str | None = None
    custom_fields: dict[str, Any] = Field(default_factory=dict)
    confirmation_recipient: dict[str, Any] = Field(default_factory=dict)


class PledgeSummary(BaseModel):
    pledge_id: int | None = None
    organization_id: int | None = None
    project_id: int | None = None
    promotion_id: int | None = None
    payment_method: str | None = None
    recurrent_state: str | None = None
    pledged_amount: Money | None = None
    pledged_at: str | None = None


class DarujmeTransaction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_system: str = "darujme"
    source_id: int
    source_key: str
    source_number: str | None = None
    organization_id: int | None = None
    transaction_id: int
    presentable_code: str | None = None
    state: str | None = None
    sent_amount: Money | None = None
    received_at: str | None = None
    outgoing_amount: Money | None = None
    outgoing_vs: str | None = None
    outgoing_bank_account: str | None = None
    last_modified_at: str | None = None
    dates: DarujmeDates = Field(default_factory=DarujmeDates)
    amounts: DarujmeAmounts = Field(default_factory=DarujmeAmounts)
    states: DarujmeStates = Field(default_factory=DarujmeStates)
    project: DarujmeProjectRef | None = None
    promotion: DarujmePromotionRef | None = None
    donor: DarujmeDonor = Field(default_factory=DarujmeDonor)
    pledge: PledgeSummary | None = None
    raw: dict[str, Any] | None = None


class DarujmePledge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_system: str = "darujme"
    source_id: int
    source_key: str
    source_number: str | None = None
    organization_id: int | None = None
    pledge_id: int
    project: DarujmeProjectRef | None = None
    promotion: DarujmePromotionRef | None = None
    dates: DarujmeDates = Field(default_factory=DarujmeDates)
    amounts: DarujmeAmounts = Field(default_factory=DarujmeAmounts)
    states: DarujmeStates = Field(default_factory=DarujmeStates)
    donor: DarujmeDonor = Field(default_factory=DarujmeDonor)
    transactions: list[DarujmeTransaction] = Field(default_factory=list)
    comment: str | None = None
    raw: dict[str, Any] | None = None


class DarujmeProject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_system: str = "darujme"
    source_id: int
    source_key: str
    source_number: str | None = None
    project_id: int
    promotion_id: None = None
    organization_id: int | None = None
    organization: dict[str, Any] = Field(default_factory=dict)
    title: dict[str, str] = Field(default_factory=dict)
    synopsis: dict[str, str] = Field(default_factory=dict)
    content: dict[str, str] = Field(default_factory=dict)
    donate_url: str | None = None
    dates: DarujmeDates = Field(default_factory=DarujmeDates)
    amounts: DarujmeAmounts = Field(default_factory=DarujmeAmounts)
    states: DarujmeStates = Field(default_factory=DarujmeStates)
    donors_count: int | None = None
    tags: list[dict[str, Any]] = Field(default_factory=list)
    raw: dict[str, Any] | None = None


class DarujmePromotion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_system: str = "darujme"
    source_id: int
    source_key: str
    source_number: str | None = None
    promotion_id: int
    project_id: int | None = None
    organization_id: int | None = None
    organization: dict[str, Any] = Field(default_factory=dict)
    title: dict[str, str] = Field(default_factory=dict)
    synopsis: dict[str, str] = Field(default_factory=dict)
    content: dict[str, str] = Field(default_factory=dict)
    donate_url: str | None = None
    dates: DarujmeDates = Field(default_factory=DarujmeDates)
    amounts: DarujmeAmounts = Field(default_factory=DarujmeAmounts)
    states: DarujmeStates = Field(default_factory=DarujmeStates)
    donors_count: int | None = None
    tags: list[dict[str, Any]] = Field(default_factory=list)
    raw: dict[str, Any] | None = None


class FoundItemError(BaseModel):
    id: int | str
    ok: bool = False
    error: ErrorInfo


class ControlTotals(BaseModel):
    count: int
    by_currency: dict[str, dict[str, str | int]] = Field(default_factory=dict)
    by_state: dict[str, int] = Field(default_factory=dict)


class FindTransactionsResult(BaseModel):
    transactions: list[DarujmeTransaction | FoundItemError] = Field(default_factory=list)
    next_cursor: str | None = None
    control_totals: ControlTotals | None = None
    error: ErrorInfo | None = None


class FindPledgesResult(BaseModel):
    pledges: list[DarujmePledge | FoundItemError] = Field(default_factory=list)
    next_cursor: str | None = None
    control_totals: ControlTotals | None = None
    error: ErrorInfo | None = None


class FindProjectsResult(BaseModel):
    projects: list[DarujmeProject | FoundItemError] = Field(default_factory=list)
    next_cursor: str | None = None
    control_totals: ControlTotals | None = None
    error: ErrorInfo | None = None


class FindPromotionsResult(BaseModel):
    promotions: list[DarujmePromotion | FoundItemError] = Field(default_factory=list)
    next_cursor: str | None = None
    control_totals: ControlTotals | None = None
    error: ErrorInfo | None = None


class TestConnectionResult(BaseModel):
    ok: bool
    organization_id: int | None = None
    error: ErrorInfo | None = None
