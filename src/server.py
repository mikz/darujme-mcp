from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import json
import secrets
import threading
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs

import httpx
from fastmcp import FastMCP
from fastmcp.apps import UI_EXTENSION_ID
from fastmcp.exceptions import ToolError
from fastmcp.server.context import Context
from fastmcp.server.lifespan import lifespan
from prefab_ui.actions import Fetch, SetState, ShowToast
from prefab_ui.actions.mcp import CallTool
from prefab_ui.app import PrefabApp
from prefab_ui.components import Button, Column, Form, Heading, Input, Muted, Text
from prefab_ui.rx import Rx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from client import DarujmeClient, DarujmeError, NotAuthenticatedError
from models import (
    ControlTotals,
    DarujmeSettlementAggregate,
    DarujmeTransaction,
    ErrorInfo,
    FindPledgesResult,
    FindProjectsResult,
    FindPromotionsResult,
    FindSettlementAggregatesResult,
    FindTransactionsResult,
    FoundItemError,
    TestConnectionResult,
)
from normalization import (
    normalize_pledge,
    normalize_project,
    normalize_promotion,
    normalize_transaction,
)
from settings import load_settings, store_credentials

MAX_PAGE_LIMIT = 500
MAX_SETTLEMENT_RANGE_DAYS = 31
_LIVE_CLIENT: DarujmeClient | None = None

TransactionState = Literal[
    "pending",
    "pending_confirmation",
    "pending_update",
    "success",
    "success_money_on_account",
    "sent_to_organization",
    "failure",
    "error",
    "refund",
    "timeout",
    "canceled",
]
ProjectState = Literal["not_active", "active", "pending"]


@lifespan
async def app_lifespan(_server: FastMCP):
    global _LIVE_CLIENT
    client = DarujmeClient(load_settings())
    _LIVE_CLIENT = client
    try:
        yield {"darujme_client": client}
    finally:
        _LIVE_CLIENT = None
        await client.aclose()


mcp = FastMCP("Darujme", lifespan=app_lifespan)
LoginMode = Literal["auto", "direct", "prefab", "web"]
ResolvedLoginMode = Literal["direct", "prefab", "web"]
_WEB_LOGIN_SERVERS: dict[str, ThreadingHTTPServer] = {}


class DarujmeLogin(BaseModel):
    """Darujme login contract. All fields are required and stored locally."""

    api_id: str = Field(description="Darujme API key ID")
    api_secret: str = Field(description="Darujme API secret")
    organization_id: int = Field(
        description=(
            "Darujme organization ID. Required because Darujme API v1 does not expose "
            "a token introspection endpoint or list of organizations accessible to the token."
        ),
        ge=1,
    )


class LoginResult(BaseModel):
    ok: bool
    mode: ResolvedLoginMode
    status: Literal["logged_in", "needs_input", "unsupported", "error"]
    message: str
    url: str | None = None
    transport: str | None = None
    ui_supported: bool = False


class SearchCursor(BaseModel):
    v: int = 1
    kind: Literal["transactions", "settlements", "pledges", "projects", "promotions"]
    offset: int
    filter_hash: str


class PrivacyMixin(BaseModel):
    include_donor_pii: bool = Field(
        default=False,
        description=(
            "Privacy level 1 → 2. Defaults to `false` (level 1, redacted): donor names, "
            "email, phone, address, company IDs, custom fields, and confirmation "
            "recipient fields are stripped from the response. Set to `true` (level 2, "
            "personal) only when the downstream task — donor confirmations, tax receipts, "
            "audit-level export — actually requires the PII. Czech law 185/2009 requires "
            "names/addresses for donation receipts; internal accounting can usually stay "
            "at level 1."
        ),
    )
    include_raw: bool = Field(
        default=False,
        description=(
            "Privacy level 2 → 3. Defaults to `false`. Set to `true` to include the raw "
            "Darujme API payload alongside the normalized fields — useful for debugging "
            "schema drift or accessing fields not yet normalized. Requires "
            "`include_donor_pii=true` (enforced by a validator) so raw donor data is not "
            "exposed by accident at level 1."
        ),
    )

    @model_validator(mode="after")
    def validate_raw_privacy(self) -> PrivacyMixin:
        if self.include_raw and not self.include_donor_pii:
            raise ValueError("include_raw requires include_donor_pii=true")
        return self


class TransactionSearchQuery(PrivacyMixin):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "query_type": "transaction_search",
                    "received_from": "2026-05-01",
                    "received_to": "2026-05-16",
                    "transaction_states": ["success", "sent_to_organization"],
                    "limit": 100,
                    "include_donor_pii": False,
                },
            ]
        },
    )

    query_type: Literal["transaction_search"] = Field(
        description=(
            "Donor-level transaction search. Returns paginated normalized donations "
            "filtered by date / state / project / promotion (PII redacted unless "
            "include_donor_pii=true). Use for audit, donor-level export, project totals."
        ),
    )
    project_ids: list[int] = Field(
        default_factory=list,
        description="Filter to donations belonging to any of these Darujme project ids.",
    )
    promotion_ids: list[int] = Field(
        default_factory=list,
        description="Filter to donations belonging to any of these promotion ids.",
    )
    received_from: date | None = Field(
        default=None,
        description=(
            "Inclusive start (ISO YYYY-MM-DD) for when the donor's payment arrived at "
            "Darujme. NOT the same as outgoing_from — those bracket the Darujme→org leg."
        ),
    )
    received_to: date | None = Field(
        default=None,
        description="Inclusive end (ISO YYYY-MM-DD) for when the donor's payment arrived at Darujme.",
    )
    outgoing_from: date | None = Field(
        default=None,
        description=(
            "Inclusive start (ISO YYYY-MM-DD) for when Darujme settled the donation onto "
            "the organization's bank account. Use this to align donor transactions with "
            "Fio incoming transfers. For a quick payout-level rollup use "
            "query_type='settlement_aggregate' instead."
        ),
    )
    outgoing_to: date | None = Field(
        default=None,
        description="Inclusive end (ISO YYYY-MM-DD) for when Darujme settled to the org account.",
    )
    failed_from: date | None = Field(
        default=None,
        description="Inclusive start (ISO YYYY-MM-DD) for the donation failure date.",
    )
    failed_to: date | None = Field(
        default=None,
        description="Inclusive end (ISO YYYY-MM-DD) for the donation failure date.",
    )
    last_modified_date_time: str | None = Field(
        default=None,
        description=(
            "Filter to donations modified on or after this timestamp (ISO 8601). Useful "
            "for incremental sync."
        ),
    )
    transaction_states: list[TransactionState] = Field(
        default_factory=list,
        description=(
            "Filter by Darujme transaction state (success, sent_to_organization, "
            "pending_confirmation, failure, …). See darujme_get_metadata.transaction_states "
            "for the full list. Empty means all states."
        ),
    )
    limit: int = Field(
        default=100,
        ge=1,
        le=MAX_PAGE_LIMIT,
        description="Maximum transactions returned per page (default 100).",
    )
    cursor: str | None = Field(
        default=None,
        description=(
            "Pagination cursor from the previous response's next_cursor. Pass back with "
            "the same filters; the server rejects the cursor if any filter drifts."
        ),
    )

    @model_validator(mode="after")
    def validate_dates(self) -> TransactionSearchQuery:
        _validate_date_range(self.received_from, self.received_to, "received")
        _validate_date_range(self.outgoing_from, self.outgoing_to, "outgoing")
        _validate_date_range(self.failed_from, self.failed_to, "failed")
        return self

    def filter_hash(self) -> str:
        return _filter_hash(
            {
                "query_type": self.query_type,
                "project_ids": sorted(self.project_ids),
                "promotion_ids": sorted(self.promotion_ids),
                "received_from": _api_date(self.received_from),
                "received_to": _api_date(self.received_to),
                "outgoing_from": _api_date(self.outgoing_from),
                "outgoing_to": _api_date(self.outgoing_to),
                "failed_from": _api_date(self.failed_from),
                "failed_to": _api_date(self.failed_to),
                "last_modified_date_time": self.last_modified_date_time,
                "transaction_states": sorted(self.transaction_states),
                "include_donor_pii": self.include_donor_pii,
                "include_raw": self.include_raw,
            }
        )


class TransactionByIdsQuery(PrivacyMixin):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "query_type": "transaction_by_ids",
                    "ids": [1203450],
                    "include_donor_pii": False,
                }
            ]
        },
    )

    query_type: Literal["transaction_by_ids"] = Field(
        description=(
            "Direct lookup of specific transactions by id. Use with ids from webhooks, "
            "confirmation e-mails, or a prior settlement_aggregate drill-down. Up to 100 "
            "ids per call."
        ),
    )
    ids: list[int] = Field(
        min_length=1,
        max_length=100,
        description="Transaction ids to fetch (1–100 per call). Order is preserved.",
    )

    def filter_hash(self) -> str:
        return _filter_hash(
            {
                "query_type": self.query_type,
                "ids": self.ids,
                "include_donor_pii": self.include_donor_pii,
                "include_raw": self.include_raw,
            }
        )


class SettlementAggregateQuery(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "query_type": "settlement_aggregate",
                    "settled_from": "2026-03-10",
                    "settled_to": "2026-03-10",
                    "bank_account": "2603445200/2010",
                    "variable_symbol": "260310661",
                    "currency": "CZK",
                    "amount": "1926.00",
                    "limit": 100,
                }
            ]
        },
    )

    query_type: Literal["settlement_aggregate"] = Field(
        description=(
            "⭐ Killer feature for Fio bank reconciliation. Aggregates transactions into "
            "organization payout rows — one row per Fio incoming transfer from Darujme — "
            "and returns outgoing_bank_account + outgoing_variable_symbol that exactly "
            "match what Fio shows on the day Darujme settles. Does NOT return individual "
            "donor records: use transaction_search or transaction_by_ids when you need "
            "donor-level detail (the aggregate row exposes transaction_ids for that)."
        )
    )
    settled_from: date = Field(
        description=(
            "Inclusive start (ISO YYYY-MM-DD) of the Darujme-to-org payout date window. "
            "Match against the date on Fio's incoming transfer row."
        ),
    )
    settled_to: date = Field(
        description=(
            "Inclusive end (ISO YYYY-MM-DD) of the payout date window. The window must "
            "not exceed MAX_SETTLEMENT_RANGE_DAYS (currently 31)."
        ),
    )
    bank_account: str | None = Field(
        default=None,
        description=(
            "Optional filter on the organization's bank account that received the payout, "
            "in Czech account/bank_code form (e.g. '2603445200/2010'). Use when you have "
            "multiple receiving accounts and need to isolate one specific Fio line."
        ),
    )
    variable_symbol: str | None = Field(
        default=None,
        description=(
            "Optional filter on the variable_symbol Darujme used on the outgoing payout "
            "(matches the VS on the Fio incoming transfer for this settlement day). NOT "
            "the donor's payment VS — that's only on individual transactions returned by "
            "query_type='transaction_search'."
        ),
    )
    currency: str | None = Field(
        default=None,
        description="Optional payout currency filter (e.g. 'CZK', 'EUR').",
    )
    amount: Decimal | None = Field(
        default=None,
        description=(
            "Optional filter on the amount the organization received in the payout "
            "(post-Darujme-fees), as a decimal. Combine with settled_from/to and "
            "variable_symbol to uniquely identify a payout."
        ),
    )
    project_ids: list[int] = Field(
        default_factory=list,
        description=(
            "Filter to payouts whose donor transactions belong to any of these project "
            "ids. Aggregation is still per-payout, not per-project."
        ),
    )
    promotion_ids: list[int] = Field(
        default_factory=list,
        description="Filter to payouts whose donor transactions belong to any of these promotion ids.",
    )
    limit: int = Field(
        default=100,
        ge=1,
        le=MAX_PAGE_LIMIT,
        description="Maximum payout aggregate rows per page (default 100).",
    )
    cursor: str | None = Field(
        default=None,
        description="Pagination cursor from a previous response's next_cursor (same filter set required).",
    )

    @model_validator(mode="after")
    def validate_dates(self) -> SettlementAggregateQuery:
        _validate_date_range(self.settled_from, self.settled_to, "settled")
        if (self.settled_to - self.settled_from).days + 1 > MAX_SETTLEMENT_RANGE_DAYS:
            raise ValueError(f"settled date range cannot exceed {MAX_SETTLEMENT_RANGE_DAYS} days")
        return self

    def filter_hash(self) -> str:
        return _filter_hash(
            {
                "query_type": self.query_type,
                "settled_from": _api_date(self.settled_from),
                "settled_to": _api_date(self.settled_to),
                "bank_account": _bank_account(self.bank_account),
                "variable_symbol": self.variable_symbol,
                "currency": self.currency,
                "amount": str(self.amount) if self.amount is not None else None,
                "project_ids": sorted(self.project_ids),
                "promotion_ids": sorted(self.promotion_ids),
            }
        )


FindTransactionsQuery = Annotated[
    TransactionSearchQuery | TransactionByIdsQuery | SettlementAggregateQuery,
    Field(discriminator="query_type"),
]


class FindPledgesQuery(PrivacyMixin):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "mode": "search",
                    "from_pledged_date": "2026-05-01",
                    "to_pledged_date": "2026-05-16",
                    "received_from": "2026-05-01",
                    "received_to": "2026-05-16",
                    "limit": 100,
                    "include_donor_pii": False,
                },
                {"mode": "by_vs", "variable_symbol": "2026000001"},
            ]
        },
    )

    mode: Literal["search", "by_ids", "by_vs"]
    ids: list[int] = Field(default_factory=list, max_length=100)
    variable_symbol: str | None = None
    project_id: int | None = Field(default=None, ge=1)
    project_ids: list[int] = Field(default_factory=list)
    promotion_ids: list[int] = Field(default_factory=list)
    from_pledged_date: date | None = None
    to_pledged_date: date | None = None
    received_from: date | None = None
    received_to: date | None = None
    outgoing_from: date | None = None
    outgoing_to: date | None = None
    last_modified_date_time: str | None = None
    payment_methods: list[str] = Field(default_factory=list)
    recurrent_states: list[str] = Field(default_factory=list)
    limit: int = Field(default=100, ge=1, le=MAX_PAGE_LIMIT)
    cursor: str | None = None

    @model_validator(mode="after")
    def validate_mode(self) -> FindPledgesQuery:
        if self.mode == "by_vs":
            if not self.variable_symbol:
                raise ValueError("variable_symbol is required when mode is by_vs")
            if self.ids:
                raise ValueError("ids are only allowed when mode is by_ids")
        else:
            _validate_ids_mode(self.mode, self.ids, "ids")
        if self.project_id is not None and self.project_ids:
            raise ValueError("Use project_id or project_ids, not both")
        _validate_date_range(self.from_pledged_date, self.to_pledged_date, "pledged")
        _validate_date_range(self.received_from, self.received_to, "received")
        _validate_date_range(self.outgoing_from, self.outgoing_to, "outgoing")
        return self

    def filter_hash(self) -> str:
        return _filter_hash(
            {
                "mode": self.mode,
                "ids": self.ids,
                "variable_symbol": self.variable_symbol,
                "project_id": self.project_id,
                "project_ids": sorted(self.project_ids),
                "promotion_ids": sorted(self.promotion_ids),
                "from_pledged_date": _api_date(self.from_pledged_date),
                "to_pledged_date": _api_date(self.to_pledged_date),
                "received_from": _api_date(self.received_from),
                "received_to": _api_date(self.received_to),
                "outgoing_from": _api_date(self.outgoing_from),
                "outgoing_to": _api_date(self.outgoing_to),
                "last_modified_date_time": self.last_modified_date_time,
                "payment_methods": sorted(self.payment_methods),
                "recurrent_states": sorted(self.recurrent_states),
                "include_donor_pii": self.include_donor_pii,
                "include_raw": self.include_raw,
            }
        )


class FindProjectsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["search", "by_ids"]
    ids: list[int] = Field(default_factory=list, max_length=100)
    state: ProjectState | None = None
    limit: int = Field(default=100, ge=1, le=MAX_PAGE_LIMIT)
    cursor: str | None = None
    include_raw: bool = False

    @model_validator(mode="after")
    def validate_mode(self) -> FindProjectsQuery:
        _validate_ids_mode(self.mode, self.ids, "ids")
        return self

    def filter_hash(self) -> str:
        return _filter_hash(
            {
                "mode": self.mode,
                "ids": self.ids,
                "state": self.state,
                "include_raw": self.include_raw,
            }
        )


class FindPromotionsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["search", "by_ids"]
    ids: list[int] = Field(default_factory=list, max_length=100)
    project_id: int | None = Field(default=None, ge=1)
    project_ids: list[int] = Field(default_factory=list, max_length=100)
    limit: int = Field(default=100, ge=1, le=MAX_PAGE_LIMIT)
    cursor: str | None = None
    include_raw: bool = False

    @model_validator(mode="after")
    def validate_mode(self) -> FindPromotionsQuery:
        _validate_ids_mode(self.mode, self.ids, "ids")
        if self.mode == "search" and self.project_id is None and not self.project_ids:
            raise ValueError("project_id or project_ids is required when mode is search")
        return self

    def filter_hash(self) -> str:
        return _filter_hash(
            {
                "mode": self.mode,
                "ids": self.ids,
                "project_id": self.project_id,
                "project_ids": self.project_ids,
                "include_raw": self.include_raw,
            }
        )


class DonationConfirmationRequest(PrivacyMixin):
    model_config = ConfigDict(extra="forbid")

    received_from: date | None = None
    received_to: date | None = None
    project_ids: list[int] = Field(default_factory=list)
    promotion_ids: list[int] = Field(default_factory=list)
    transaction_states: list[TransactionState] = Field(
        default_factory=lambda: ["success", "success_money_on_account", "sent_to_organization"]
    )
    limit: int = Field(default=100, ge=1, le=MAX_PAGE_LIMIT)
    cursor: str | None = None

    @model_validator(mode="after")
    def validate_ranges(self) -> DonationConfirmationRequest:
        _validate_date_range(self.received_from, self.received_to, "received")
        return self


class ConfirmationGroup(BaseModel):
    donor: dict[str, Any]
    pledge_id: int | None = None
    project_id: int | None = None
    promotion_id: int | None = None
    transactions: list[dict[str, Any]] = Field(default_factory=list)
    total_by_currency: dict[str, str] = Field(default_factory=dict)


class DonationConfirmationsResult(BaseModel):
    groups: list[ConfirmationGroup] = Field(default_factory=list)
    next_cursor: str | None = None
    control_totals: ControlTotals | None = None
    error: ErrorInfo | None = None


class MetadataEntry(BaseModel):
    code: str | int
    name: str
    description: str | None = None


class MetadataLimits(BaseModel):
    max_page_limit: int
    max_settlement_range_days: int
    cursor_pagination: str


class MetadataResult(BaseModel):
    source_documents: list[str] = Field(default_factory=list)
    comparison_fields: dict[str, Any] = Field(default_factory=dict)
    setup_tools: list[str] = Field(default_factory=list)
    login_contract: dict[str, Any] = Field(default_factory=dict)
    query_modes: dict[str, list[str]] = Field(default_factory=dict)
    transaction_states: list[MetadataEntry] = Field(default_factory=list)
    project_states: list[MetadataEntry] = Field(default_factory=list)
    payment_methods: list[MetadataEntry] = Field(default_factory=list)
    currencies: list[str] = Field(default_factory=list)
    privacy: dict[str, Any] = Field(default_factory=dict)
    limits: MetadataLimits
    error_codes: list[MetadataEntry] = Field(default_factory=list)
    side_effects: list[dict[str, str]] = Field(default_factory=list)


def _login_on_submit(login: DarujmeLogin) -> str:
    base_url = "https://www.darujme.cz/api/v1/"
    if _LIVE_CLIENT is not None:
        base_url = _LIVE_CLIENT.base_url
    params = {"apiId": login.api_id, "apiSecret": login.api_secret}
    try:
        response = httpx.get(
            f"{base_url.rstrip('/')}/organization/{login.organization_id}/projects",
            params=params,
            timeout=10.0,
        )
    except httpx.RequestError as exc:
        return f"network_error: Network error contacting Darujme: {exc}"
    if response.status_code in {401, 403}:
        return "auth_error: Darujme rejected the credentials."
    if response.status_code >= 400:
        return f"darujme_error: Darujme returned HTTP {response.status_code}: {response.text[:200]}"

    store_credentials(login.api_id, login.api_secret, login.organization_id)
    if _LIVE_CLIENT is not None:
        _LIVE_CLIENT.set_credentials_sync(login.api_id, login.api_secret, login.organization_id)
    return (
        "Logged in to Darujme organization "
        f"{login.organization_id}. The read-only donation tools are ready to use."
    )


async def darujme_login(
    ctx: Context,
    mode: LoginMode = "auto",
    credentials: Annotated[
        DarujmeLogin | None,
        Field(
            description=(
                "Credentials for mode=direct. Omit for auto, prefab, or web. "
                "Direct mode sends secrets through the MCP tool call."
            )
        ),
    ] = None,
) -> LoginResult | PrefabApp:
    """Sign in to Darujme using auto, direct, Prefab UI, or localhost web login."""
    selected = _resolve_login_mode(ctx, mode)
    if selected == "prefab":
        if not ctx.client_supports_extension(UI_EXTENSION_ID):
            return _login_result(
                ctx,
                mode="prefab",
                status="unsupported",
                message="This MCP client does not advertise the Apps UI extension.",
                ok=False,
            )
        return _darujme_login_prefab_app()
    if selected == "web":
        url = _start_darujme_web_login()
        return _login_result(
            ctx,
            mode="web",
            status="needs_input",
            message=f"Open this local URL in a browser to sign in to Darujme: {url}",
            ok=True,
            url=url,
        )
    if credentials is None:
        return _login_result(
            ctx,
            mode="direct",
            status="error",
            message=(
                "mode=direct accepts credentials in the tool call and requires "
                "credentials.api_id, credentials.api_secret, and "
                "credentials.organization_id."
            ),
            ok=False,
        )
    return _login_result_from_submit(ctx, "direct", _login_on_submit(credentials))


def _resolve_login_mode(ctx: Context, mode: LoginMode) -> ResolvedLoginMode:
    if mode != "auto":
        return mode
    if ctx.client_supports_extension(UI_EXTENSION_ID):
        return "prefab"
    return "web"


def _login_result(
    ctx: Context,
    *,
    mode: ResolvedLoginMode,
    status: Literal["logged_in", "needs_input", "unsupported", "error"],
    message: str,
    ok: bool,
    url: str | None = None,
) -> LoginResult:
    return LoginResult(
        ok=ok,
        mode=mode,
        status=status,
        message=message,
        url=url,
        transport=ctx.transport,
        ui_supported=ctx.client_supports_extension(UI_EXTENSION_ID),
    )


def _login_result_from_submit(ctx: Context, mode: ResolvedLoginMode, message: str) -> LoginResult:
    ok = message.startswith("Logged in to Darujme organization")
    return _login_result(
        ctx,
        mode=mode,
        status="logged_in" if ok else "error",
        message=message,
        ok=ok,
    )


def _darujme_login_prefab_app(web_submit_url: str | None = None) -> PrefabApp:
    credentials = {
        "api_id": Rx("api_id"),
        "api_secret": Rx("api_secret"),
        "organization_id": Rx("organization_id"),
    }
    if web_submit_url:
        submit_action = Fetch(
            web_submit_url,
            method="POST",
            headers={"Content-Type": "application/json"},
            body=credentials,
            onSuccess=[
                SetState("message", "{{ $result.message }}"),
                ShowToast("{{ $result.message }}", variant="success"),
            ],
            onError=ShowToast("Darujme login failed.", variant="error"),
        )
    else:
        submit_action = CallTool(
            "darujme_login",
            arguments={"mode": "direct", "credentials": credentials},
            onSuccess=[
                SetState("message", "{{ $result.message }}"),
                ShowToast("{{ $result.message }}", variant="success"),
            ],
            onError=ShowToast("Darujme login failed.", variant="error"),
        )

    with Column(gap=4, css_class="p-6 max-w-md") as view:
        Heading("Sign in to Darujme", level=2)
        Muted("Credentials are validated with Darujme and stored locally for this MCP scope.")
        with Form(onSubmit=submit_action):
            Input(name="api_id", placeholder="API ID", required=True)
            Input(name="api_secret", inputType="password", placeholder="API secret", required=True)
            Input(
                name="organization_id",
                inputType="number",
                placeholder="Organization ID",
                required=True,
                min=1,
            )
            Button("Sign in", buttonType="submit")
        Text(content=Rx("message"))
    return PrefabApp(title="Darujme Login", view=view, state={"message": ""})


class _DarujmeLoginHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        server = self.server
        token = getattr(server, "login_token", "")
        if self.path.rstrip("/") != f"/{token}":
            self.send_error(404)
            return
        submit_url = f"http://127.0.0.1:{server.server_port}/{token}/submit"
        html = _darujme_login_prefab_app(submit_url).html()
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        server = self.server
        token = getattr(server, "login_token", "")
        if self.path != f"/{token}/submit":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        try:
            if "application/json" in self.headers.get("Content-Type", ""):
                payload = json.loads(raw or "{}")
            else:
                parsed = parse_qs(raw)
                payload = {key: values[-1] for key, values in parsed.items()}
            message = _login_on_submit(DarujmeLogin.model_validate(payload))
            ok = message.startswith("Logged in to Darujme organization")
            self._send_json({"ok": ok, "message": message})
        except Exception as exc:
            self._send_json({"ok": False, "message": str(exc)}, status=400)

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def _start_darujme_web_login() -> str:
    token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DarujmeLoginHandler)
    server.login_token = token  # type: ignore[attr-defined]
    _WEB_LOGIN_SERVERS[token] = server
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}/{token}"


def _requires_login(fn):
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        ctx = kwargs.get("ctx")
        if ctx is None:
            for arg in args:
                if isinstance(arg, Context):
                    ctx = arg
                    break
        if ctx is None:
            raise RuntimeError("_requires_login: Context missing from tool call")
        client: DarujmeClient = ctx.lifespan_context["darujme_client"]
        if not client.is_authenticated():
            hint = (
                "Not signed in to Darujme. Call the `darujme_login` tool first. "
                "It supports auto, direct, Prefab, and web login modes."
            )
            if not ctx.client_supports_extension(UI_EXTENSION_ID):
                hint += (
                    " This client does not render inline forms; use mode=web or mode=direct, "
                    "set DARUJME_API_ID, DARUJME_API_SECRET, and DARUJME_ORGANIZATION_ID, "
                    "or pre-seeded in the cwd-scoped credential store."
                )
            raise ToolError(hint)
        return await fn(*args, **kwargs)

    return wrapper


@mcp.tool
@_requires_login
async def darujme_test_connection(ctx: Context) -> TestConnectionResult:
    """Verify configured Darujme credentials (api_id, api_secret, organization_id) by
    issuing one read-only API call.

    No state changes. Use to diagnose auth failures before running other tools.
    """
    return await _test_connection(_client_from_context(ctx))


@mcp.tool
@_requires_login
async def darujme_find_transactions(
    query: Annotated[
        FindTransactionsQuery,
        Field(description="Object query for Darujme transactions. Pass an object, not a string."),
    ],
    ctx: Context,
) -> FindTransactionsResult | FindSettlementAggregatesResult:
    """Find Darujme donation transactions by filter or by ID, or aggregate them into
    bank-payout rows for Fio reconciliation. Three modes selected via `query.query_type`:

    1. `transaction_search` — donor-level search by received_at / outgoing_at / state /
       project / promotion. Returns paginated normalized donations (PII redacted unless
       include_donor_pii=true). Use for audit, donor-level export, project totals.

    2. `transaction_by_ids` — bulk fetch up to 100 specific transaction IDs. Use with IDs
       from webhooks, confirmation e-mails, or a prior settlement_aggregate drill-down.

    3. `settlement_aggregate` — ⭐ killer feature for monthly bank reconciliation. Aggregates
       donations into organization PAYOUT rows (one row per Fio incoming transfer from
       Darujme), returning `bank_account` and `variable_symbol` that exactly match what
       Fio shows on the day Darujme settles. Does NOT return individual donor records —
       use `transaction_search` (or `transaction_by_ids` with the returned
       `transaction_ids`) when donor-level detail is required.

    Privacy: donor PII is redacted by default. Set `include_donor_pii=true` only when the
    downstream task (confirmations, tax receipts, audit) actually requires it. See
    PrivacyMixin Field descriptions and darujme_get_metadata.privacy for the 3-level
    hierarchy.

    EXAMPLE (Fio reconciliation): a Fio incoming line shows `date=2026-05-10`,
    `amount=1926.00 CZK`, VS=`260510661`, account=`2603445200/2010`. Call this tool with:
        query_type='settlement_aggregate', settled_from='2026-05-10',
        settled_to='2026-05-10', bank_account='2603445200/2010',
        variable_symbol='260510661'
    → returns the matching aggregate row plus `transaction_ids` you can drill down via
    query_type='transaction_by_ids'.
    """
    return await _find_transactions(_client_from_context(ctx), query)


@mcp.tool
@_requires_login
async def darujme_find_pledges(
    query: Annotated[
        FindPledgesQuery,
        Field(description="Object query for Darujme pledges. Pass an object, not a string."),
    ],
    ctx: Context,
) -> FindPledgesResult:
    """Find Darujme recurring pledges (monthly / quarterly / one-time future commitments)
    by filter, by ID, or by a donation's variable_symbol.

    Modes via `query.mode`:
      - `search` — filter by date range, project, promotion, payment method, etc.
      - `by_ids` — fetch specific pledge ids (1–100 per call).
      - `by_vs` — look up pledges by the VS of a donation they generated (exact match).

    Use for audit of pledge state, matching incoming payments to pledge records, or
    pledge-level reporting. Donor PII is redacted by default; opt in via
    `include_donor_pii=true` for outreach use cases.
    """
    return await _find_pledges(_client_from_context(ctx), query)


@mcp.tool
@_requires_login
async def darujme_find_projects(
    query: Annotated[
        FindProjectsQuery,
        Field(description="Object query for Darujme projects. Pass an object, not a string."),
    ],
    ctx: Context,
) -> FindProjectsResult:
    """Find Darujme fundraising projects by listing or by explicit project ids.

    Projects are top-level fundraising entities (e.g. 'Annual Food Drive 2026');
    promotions are peer-to-peer variants within a project (see darujme_find_promotions).

    Modes via `query.mode`:
      - `search` — list projects, optionally filtered by lifecycle `state` (active /
        pending / not_active).
      - `by_ids` — fetch specific project records (1–100 per call).
    """
    return await _find_projects(_client_from_context(ctx), query)


@mcp.tool
@_requires_login
async def darujme_find_promotions(
    query: Annotated[
        FindPromotionsQuery,
        Field(description="Object query for Darujme promotions. Pass an object, not a string."),
    ],
    ctx: Context,
) -> FindPromotionsResult:
    """Find Darujme peer-to-peer promotions inside one or more projects (e.g. individual
    "Runners for Hope" fundraisers under a charity marathon).

    Each promotion has its own donors and pledge pool. Modes via `query.mode`:
      - `search` — list promotions for one or more projects (`project_id` or
        `project_ids` required).
      - `by_ids` — fetch specific promotion records (1–100 per call).
    """
    return await _find_promotions(_client_from_context(ctx), query)


@mcp.tool
@_requires_login
async def darujme_prepare_donation_confirmations(
    request: Annotated[
        DonationConfirmationRequest,
        Field(
            description=(
                "Read-only grouping of eligible donations for later confirmation workflows. "
                "No PDFs are generated and nothing is sent."
            )
        ),
    ],
    ctx: Context,
) -> DonationConfirmationsResult:
    """Group eligible donations by donor (email / name / company id / pledge) and sum
    amounts by currency for downstream confirmation workflows.

    ⚠ This tool does NOT generate PDFs or send emails — it only assembles the data your
    client / agent uses to template confirmations. Workflow:

    1. Call this tool with received_from / received_to and optional project / promotion
       filters.
    2. Iterate the returned groups; for each group build your confirmation text / PDF /
       email externally.
    3. Send or print confirmations outside this MCP.

    Defaults: `transaction_states = ['success', 'success_money_on_account',
    'sent_to_organization']` (only completed donations, excluding pending / failed).
    Override `transaction_states` if you need a different scope.
    """
    return await _prepare_donation_confirmations(_client_from_context(ctx), request)


@mcp.tool
async def darujme_get_metadata() -> MetadataResult:
    """Return the Rosetta Stone for interpreting Darujme MCP responses.

    Covers query_modes (which tool / which mode to call for each scenario), transaction
    states, project states, payment methods, currencies, privacy levels (the 3-level
    hierarchy enforced by PrivacyMixin), API limits, error codes, and side-effect notes.

    Read-only with no side effects. Call once at session start and cache the result.
    """
    return _metadata_result()


def _client_from_context(ctx: Context) -> DarujmeClient:
    return ctx.lifespan_context["darujme_client"]


async def _test_connection(client: DarujmeClient) -> TestConnectionResult:
    try:
        await client.test_connection()
        return TestConnectionResult(ok=True, organization_id=client.organization_id)
    except Exception as exc:
        return TestConnectionResult(
            ok=False,
            organization_id=client.organization_id,
            error=_error_info(exc),
        )


async def _find_transactions(
    client: DarujmeClient,
    query: FindTransactionsQuery,
) -> FindTransactionsResult | FindSettlementAggregatesResult:
    if isinstance(query, SettlementAggregateQuery):
        return await _find_settlement_aggregates(client, query)
    if isinstance(query, TransactionByIdsQuery):
        try:
            records = await _fetch_by_ids(
                query.ids,
                client.get_transaction,
                lambda raw: normalize_transaction(
                    raw,
                    include_donor_pii=query.include_donor_pii,
                    include_raw=query.include_raw,
                ),
            )
            return FindTransactionsResult(
                transactions=records,
                control_totals=_control_totals(
                    [item for item in records if not isinstance(item, FoundItemError)]
                ),
            )
        except Exception as exc:
            return FindTransactionsResult(error=_error_info(exc))
    if query.cursor:
        cursor_result = _decode_cursor_result(
            query.cursor,
            kind="transactions",
            filter_hash=query.filter_hash(),
        )
        if isinstance(cursor_result, ErrorInfo):
            return FindTransactionsResult(error=cursor_result)
        offset = cursor_result.offset
    else:
        offset = 0
    try:
        raws = await client.search_transactions(_transaction_params(query, offset=offset))
        records = [
            normalize_transaction(
                raw,
                include_donor_pii=query.include_donor_pii,
                include_raw=query.include_raw,
            )
            for raw in raws
        ]
        next_cursor = _next_cursor(
            "transactions", query.filter_hash(), offset, query.limit, len(raws)
        )
        return FindTransactionsResult(
            transactions=records,
            next_cursor=next_cursor,
            control_totals=_control_totals(records),
        )
    except Exception as exc:
        return FindTransactionsResult(error=_error_info(exc))


async def _find_settlement_aggregates(
    client: DarujmeClient,
    query: SettlementAggregateQuery,
) -> FindSettlementAggregatesResult:
    if query.cursor:
        cursor_result = _decode_cursor_result(
            query.cursor,
            kind="settlements",
            filter_hash=query.filter_hash(),
        )
        if isinstance(cursor_result, ErrorInfo):
            return FindSettlementAggregatesResult(error=cursor_result)
        offset = cursor_result.offset
    else:
        offset = 0
    try:
        records: list[tuple[str, DarujmeTransaction]] = []
        current = query.settled_from
        while current <= query.settled_to:
            day_records = await _fetch_settlement_transactions_for_day(client, query, current)
            records.extend(day_records)
            current += timedelta(days=1)

        settlements = _aggregate_settlements(records)
        settlements = _filter_settlement_aggregates(settlements, query)
        page = settlements[offset : offset + query.limit]
        next_cursor = _next_cursor_from_total(
            "settlements", query.filter_hash(), offset, query.limit, len(settlements)
        )
        return FindSettlementAggregatesResult(
            settlements=page,
            next_cursor=next_cursor,
        )
    except Exception as exc:
        return FindSettlementAggregatesResult(error=_error_info(exc))


async def _fetch_settlement_transactions_for_day(
    client: DarujmeClient,
    query: SettlementAggregateQuery,
    day: date,
) -> list[tuple[str, DarujmeTransaction]]:
    records: list[DarujmeTransaction] = []
    offset = 0
    while True:
        raws = await client.search_transactions(
            {
                "projectIds[]": query.project_ids,
                "promotionIds[]": query.promotion_ids,
                "fromOutgoingDate": _api_date(day),
                "toOutgoingDate": _api_date(day),
                "transactionState[]": ["sent_to_organization"],
                "pageSize": MAX_PAGE_LIMIT,
                "offset": offset,
            }
        )
        records.extend(
            normalize_transaction(raw, include_donor_pii=False, include_raw=False) for raw in raws
        )
        if len(raws) < MAX_PAGE_LIMIT:
            break
        offset += MAX_PAGE_LIMIT
    return [(_api_date(day) or "", record) for record in records]


async def _find_pledges(client: DarujmeClient, query: FindPledgesQuery) -> FindPledgesResult:
    if query.cursor:
        cursor_result = _decode_cursor_result(
            query.cursor,
            kind="pledges",
            filter_hash=query.filter_hash(),
        )
        if isinstance(cursor_result, ErrorInfo):
            return FindPledgesResult(error=cursor_result)
        offset = cursor_result.offset
    else:
        offset = 0
    try:
        if query.mode == "by_ids":
            records = await _fetch_by_ids(
                query.ids,
                client.get_pledge,
                lambda raw: normalize_pledge(
                    raw,
                    include_donor_pii=query.include_donor_pii,
                    include_raw=query.include_raw,
                ),
            )
            return FindPledgesResult(
                pledges=records,
                control_totals=_control_totals(
                    [item for item in records if not isinstance(item, FoundItemError)]
                ),
            )
        if query.mode == "by_vs":
            assert query.variable_symbol is not None
            raws = await client.pledges_by_vs(query.variable_symbol)
            page = raws[offset : offset + query.limit]
            next_cursor = _next_cursor_from_total(
                "pledges", query.filter_hash(), offset, query.limit, len(raws)
            )
        else:
            page = await client.search_pledges(_pledge_params(query, offset=offset))
            next_cursor = _next_cursor(
                "pledges", query.filter_hash(), offset, query.limit, len(page)
            )
        records = [
            normalize_pledge(
                raw,
                include_donor_pii=query.include_donor_pii,
                include_raw=query.include_raw,
            )
            for raw in page
        ]
        return FindPledgesResult(
            pledges=records,
            next_cursor=next_cursor,
            control_totals=_control_totals(records),
        )
    except Exception as exc:
        return FindPledgesResult(error=_error_info(exc))


async def _find_projects(client: DarujmeClient, query: FindProjectsQuery) -> FindProjectsResult:
    if query.cursor:
        cursor_result = _decode_cursor_result(
            query.cursor,
            kind="projects",
            filter_hash=query.filter_hash(),
        )
        if isinstance(cursor_result, ErrorInfo):
            return FindProjectsResult(error=cursor_result)
        offset = cursor_result.offset
    else:
        offset = 0
    try:
        if query.mode == "by_ids":
            records = await _fetch_by_ids(
                query.ids,
                client.get_project,
                lambda raw: normalize_project(raw, include_raw=query.include_raw),
            )
            return FindProjectsResult(
                projects=records,
                control_totals=_control_totals(
                    [item for item in records if not isinstance(item, FoundItemError)]
                ),
            )
        raws = await client.list_projects({"state": query.state})
        page = raws[offset : offset + query.limit]
        records = [normalize_project(raw, include_raw=query.include_raw) for raw in page]
        next_cursor = _next_cursor_from_total(
            "projects", query.filter_hash(), offset, query.limit, len(raws)
        )
        return FindProjectsResult(
            projects=records,
            next_cursor=next_cursor,
            control_totals=_control_totals(records),
        )
    except Exception as exc:
        return FindProjectsResult(error=_error_info(exc))


async def _find_promotions(
    client: DarujmeClient,
    query: FindPromotionsQuery,
) -> FindPromotionsResult:
    if query.cursor:
        cursor_result = _decode_cursor_result(
            query.cursor,
            kind="promotions",
            filter_hash=query.filter_hash(),
        )
        if isinstance(cursor_result, ErrorInfo):
            return FindPromotionsResult(error=cursor_result)
        offset = cursor_result.offset
    else:
        offset = 0
    try:
        if query.mode == "by_ids":
            records = await _fetch_by_ids(
                query.ids,
                client.get_promotion,
                lambda raw: normalize_promotion(raw, include_raw=query.include_raw),
            )
            return FindPromotionsResult(
                promotions=records,
                control_totals=_control_totals(
                    [item for item in records if not isinstance(item, FoundItemError)]
                ),
            )
        project_ids = query.project_ids or (
            [query.project_id] if query.project_id is not None else []
        )
        raws: list[dict[str, Any]] = []
        for project_id in project_ids:
            raws.extend(await client.list_promotions(project_id))
        page = raws[offset : offset + query.limit]
        records = [normalize_promotion(raw, include_raw=query.include_raw) for raw in page]
        next_cursor = _next_cursor_from_total(
            "promotions", query.filter_hash(), offset, query.limit, len(raws)
        )
        return FindPromotionsResult(
            promotions=records,
            next_cursor=next_cursor,
            control_totals=_control_totals(records),
        )
    except Exception as exc:
        return FindPromotionsResult(error=_error_info(exc))


async def _prepare_donation_confirmations(
    client: DarujmeClient,
    request: DonationConfirmationRequest,
) -> DonationConfirmationsResult:
    query = TransactionSearchQuery(
        query_type="transaction_search",
        project_ids=request.project_ids,
        promotion_ids=request.promotion_ids,
        received_from=request.received_from,
        received_to=request.received_to,
        transaction_states=request.transaction_states,
        limit=request.limit,
        cursor=request.cursor,
        include_donor_pii=request.include_donor_pii,
        include_raw=request.include_raw,
    )
    result = await _find_transactions(client, query)
    if result.error is not None:
        return DonationConfirmationsResult(error=result.error)
    groups: dict[str, ConfirmationGroup] = {}
    for transaction in result.transactions:
        if isinstance(transaction, FoundItemError):
            continue
        pledge_id = transaction.pledge.pledge_id if transaction.pledge else None
        donor_key = _donor_key(transaction.donor.model_dump(mode="json"), pledge_id)
        group = groups.setdefault(
            donor_key,
            ConfirmationGroup(
                donor=transaction.donor.model_dump(mode="json", exclude_none=True),
                pledge_id=pledge_id,
                project_id=transaction.pledge.project_id if transaction.pledge else None,
                promotion_id=transaction.pledge.promotion_id if transaction.pledge else None,
            ),
        )
        sent = transaction.sent_amount
        group.transactions.append(
            {
                "transaction_id": transaction.transaction_id,
                "presentable_code": transaction.presentable_code,
                "received_at": transaction.received_at,
                "state": transaction.state,
                "sent_amount": sent.model_dump(mode="json") if sent else None,
            }
        )
        if sent and sent.currency and sent.amount:
            current = float(group.total_by_currency.get(sent.currency, "0"))
            group.total_by_currency[sent.currency] = f"{current + float(sent.amount):.2f}"
    return DonationConfirmationsResult(
        groups=list(groups.values()),
        next_cursor=result.next_cursor,
        control_totals=result.control_totals,
    )


async def _fetch_by_ids(ids: list[int], getter: Any, normalizer: Any) -> list[Any]:
    records: list[Any] = []
    for item_id in ids:
        try:
            records.append(normalizer(await getter(item_id)))
        except Exception as exc:
            records.append(FoundItemError(id=item_id, error=_error_info(exc)))
    return records


def _transaction_params(query: TransactionSearchQuery, *, offset: int) -> dict[str, Any]:
    return {
        "projectIds[]": query.project_ids,
        "promotionIds[]": query.promotion_ids,
        "fromReceivedDate": _api_date(query.received_from),
        "toReceivedDate": _api_date(query.received_to),
        "fromOutgoingDate": _api_date(query.outgoing_from),
        "toOutgoingDate": _api_date(query.outgoing_to),
        "fromFailedDate": _api_date(query.failed_from),
        "toFailedDate": _api_date(query.failed_to),
        "lastModifiedDateTime": query.last_modified_date_time,
        "transactionState[]": query.transaction_states,
        "pageSize": query.limit,
        "offset": offset,
    }


def _pledge_params(query: FindPledgesQuery, *, offset: int) -> dict[str, Any]:
    project_id = query.project_id
    if project_id is None and query.project_ids:
        project_id = query.project_ids[0]
    return {
        "projectId": project_id,
        "fromPledgedDate": _api_date(query.from_pledged_date),
        "toPledgedDate": _api_date(query.to_pledged_date),
        "fromReceivedDate": _api_date(query.received_from),
        "toReceivedDate": _api_date(query.received_to),
        "fromOutgoingDate": _api_date(query.outgoing_from),
        "toOutgoingDate": _api_date(query.outgoing_to),
        "lastModifiedDateTime": query.last_modified_date_time,
        "paymentMethod[]": query.payment_methods,
        "recurrentState[]": query.recurrent_states,
        "pageSize": query.limit,
        "offset": offset,
    }


def _aggregate_settlements(
    records: list[tuple[str, DarujmeTransaction]]
) -> list[DarujmeSettlementAggregate]:
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for settled_date, transaction in records:
        outgoing = transaction.outgoing_amount
        sent = transaction.sent_amount
        if (
            not settled_date
            or outgoing is None
            or outgoing.amount is None
            or outgoing.currency is None
            or transaction.outgoing_bank_account is None
            or transaction.outgoing_variable_symbol is None
        ):
            continue
        key = (
            settled_date,
            transaction.outgoing_bank_account,
            transaction.outgoing_variable_symbol,
            outgoing.currency,
        )
        group = groups.setdefault(
            key,
            {
                "outgoing_total": Decimal("0"),
                "sent_total": Decimal("0"),
                "transaction_ids": [],
            },
        )
        group["outgoing_total"] += Decimal(outgoing.amount)
        if sent is not None and sent.amount is not None:
            group["sent_total"] += Decimal(sent.amount)
        group["transaction_ids"].append(transaction.transaction_id)

    settlements: list[DarujmeSettlementAggregate] = []
    for (settled_date, account, variable_symbol, currency), values in groups.items():
        outgoing_total = values["outgoing_total"]
        sent_total = values["sent_total"]
        settlements.append(
            DarujmeSettlementAggregate(
                date=settled_date,
                bank_account=account,
                variable_symbol=variable_symbol,
                currency=currency,
                amount=_format_money(outgoing_total),
                sent_total=_format_money(sent_total),
                fee_total=_format_money(sent_total - outgoing_total),
                transaction_count=len(values["transaction_ids"]),
                transaction_ids=values["transaction_ids"],
            )
        )
    return sorted(
        settlements,
        key=lambda item: (
            item.date,
            item.bank_account,
            item.variable_symbol,
            item.currency,
        ),
    )


def _filter_settlement_aggregates(
    settlements: list[DarujmeSettlementAggregate],
    query: SettlementAggregateQuery,
) -> list[DarujmeSettlementAggregate]:
    result = settlements
    bank_account = _bank_account(query.bank_account)
    if bank_account:
        result = [
            settlement
            for settlement in result
            if settlement.bank_account == bank_account
        ]
    if query.variable_symbol:
        result = [
            settlement
            for settlement in result
            if settlement.variable_symbol == query.variable_symbol
        ]
    if query.currency:
        currency = query.currency.upper()
        result = [settlement for settlement in result if settlement.currency.upper() == currency]
    if query.amount is not None:
        result = [
            settlement
            for settlement in result
            if Decimal(settlement.amount) == query.amount
        ]
    return result


def _money_amount(money: Any) -> Decimal | None:
    amount = getattr(money, "amount", None)
    if amount is None:
        return None
    try:
        return Decimal(str(amount))
    except (InvalidOperation, ValueError):
        return None


def _format_money(amount: Decimal) -> str:
    return str(amount.quantize(Decimal("0.01")))


def _bank_account(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.replace(" ", "")
    if "/" not in normalized:
        return normalized
    account, bank_code = normalized.split("/", 1)
    if not account or not bank_code:
        return normalized
    return f"{account}/{bank_code}"


def _metadata_result() -> MetadataResult:
    return MetadataResult(
        source_documents=[
            "https://www.darujme.cz/doc/api/v1/index.html",
            "https://documenter.getpostman.com/view/10150431/T1LS9jWA",
            "https://www.darujme.cz/dar/api/darujme_api.php?api_id=%s&api_secret=%s&typ_dotazu=1",
        ],
        comparison_fields={
            "money_format": "fixed_two_decimal_string",
            "bank_account_format": "account/bank_code",
            "date_filters": [
                "received_from",
                "received_to",
                "outgoing_from",
                "outgoing_to",
                "failed_from",
                "failed_to",
            ],
            "settlement_date_filters": ["settled_from", "settled_to"],
            "donor_amount": "sent_amount",
            "bank_account_fields": ["outgoing_bank_account", "settlements.bank_account"],
            "bank_payout_fields": [
                "outgoing_amount",
                "outgoing_currency",
                "outgoing_variable_symbol",
                "outgoing_bank_account",
            ],
            "settlement_aggregate_fields": [
                "date",
                "bank_account",
                "variable_symbol",
                "currency",
                "amount",
                "sent_total",
                "fee_total",
                "transaction_count",
                "transaction_ids",
            ],
            "settlement_aggregate_usage": (
                "Use query_type=settlement_aggregate for bank payout reconciliation and "
                "bank statement matching against organization payout rows."
            ),
            "control_totals": ["sent_by_currency", "outgoing_by_currency"],
        },
        setup_tools=["darujme_login"],
        login_contract={
            "required_fields": ["api_id", "api_secret", "organization_id"],
            "environment_variables": [
                "DARUJME_API_ID",
                "DARUJME_API_SECRET",
                "DARUJME_ORGANIZATION_ID",
            ],
            "organization_id_required_reason": (
                "Darujme API v1 requires organizationId in organization-scoped URLs and "
                "does not expose token introspection or organization discovery."
            ),
        },
        query_modes={
            "darujme_find_transactions": [
                "transaction_search",
                "transaction_by_ids",
                "settlement_aggregate",
            ],
            "darujme_find_pledges": ["search", "by_ids", "by_vs"],
            "darujme_find_projects": ["search", "by_ids"],
            "darujme_find_promotions": ["search", "by_ids"],
        },
        transaction_states=[
            MetadataEntry(code=state, name=state.replace("_", " ").title())
            for state in TransactionState.__args__
        ],
        project_states=[
            MetadataEntry(code=state, name=state.replace("_", " ").title())
            for state in ProjectState.__args__
        ],
        payment_methods=[
            MetadataEntry(code="proxypay_charge", name="ProxyPay charge"),
            MetadataEntry(code="gp_webpay_charge", name="GP webpay card charge"),
            MetadataEntry(code="funds_transfer", name="Bank transfer"),
            MetadataEntry(code="payu_transfer", name="PayU transfer"),
            MetadataEntry(code="csas_permanent_payment", name="CSAS permanent payment"),
        ],
        currencies=["CZK", "EUR", "GBP", "USD"],
        privacy={
            "default": "Donor PII is redacted (privacy level 1).",
            "levels": [
                {
                    "level": 1,
                    "name": "redacted",
                    "flag_values": {"include_donor_pii": False, "include_raw": False},
                    "description": (
                        "Default. Donor PII stripped (names, email, phone, address, "
                        "company_id, custom_fields, confirmation_recipient). Use for "
                        "audit, bulk export, summary stats — minimizes PII storage."
                    ),
                    "visible_donor_fields": [],
                },
                {
                    "level": 2,
                    "name": "personal",
                    "flag_values": {"include_donor_pii": True, "include_raw": False},
                    "description": (
                        "Full donor PII included. Use for donor confirmations, customer "
                        "service, tax receipt generation."
                    ),
                    "visible_donor_fields": [
                        "name",
                        "email",
                        "phone",
                        "address",
                        "company_id",
                        "custom_fields",
                        "confirmation_recipient",
                    ],
                },
                {
                    "level": 3,
                    "name": "personal_with_raw",
                    "flag_values": {"include_donor_pii": True, "include_raw": True},
                    "description": (
                        "Adds raw Darujme JSON payload. Rare — use only for debugging or "
                        "integrations that need the unmodified API shape."
                    ),
                    "visible_donor_fields": [
                        "name",
                        "email",
                        "phone",
                        "address",
                        "company_id",
                        "custom_fields",
                        "confirmation_recipient",
                        "raw",
                    ],
                },
            ],
        },
        limits=MetadataLimits(
            max_page_limit=MAX_PAGE_LIMIT,
            max_settlement_range_days=MAX_SETTLEMENT_RANGE_DAYS,
            cursor_pagination="Darujme pageSize and offset wrapped in opaque cursors.",
        ),
        error_codes=[
            MetadataEntry(code="not_configured", name="Credentials are not configured"),
            MetadataEntry(code="auth_error", name="Darujme rejected the credentials"),
            MetadataEntry(code="not_found", name="Requested object was not found"),
            MetadataEntry(code="invalid_request", name="Darujme rejected request parameters"),
            MetadataEntry(code="invalid_response", name="Darujme returned an unexpected shape"),
            MetadataEntry(code="invalid_cursor", name="Cursor cannot be decoded"),
            MetadataEntry(code="cursor_mismatch", name="Cursor does not match this query"),
            MetadataEntry(code="network_error", name="Network request failed"),
            MetadataEntry(code="darujme_error", name="Darujme API request failed"),
            MetadataEntry(code="error", name="Unexpected local error"),
        ],
        side_effects=[
            {
                "tool": "all read tools",
                "side_effect": "none",
                "description": "V1 only uses read-only Darujme API endpoints.",
            }
        ],
    )


def _next_cursor(
    kind: Literal["transactions", "settlements", "pledges", "projects", "promotions"],
    filter_hash: str,
    offset: int,
    limit: int,
    returned_count: int,
) -> str | None:
    if returned_count < limit:
        return None
    return _encode_cursor(SearchCursor(kind=kind, offset=offset + limit, filter_hash=filter_hash))


def _next_cursor_from_total(
    kind: Literal["transactions", "settlements", "pledges", "projects", "promotions"],
    filter_hash: str,
    offset: int,
    limit: int,
    total_count: int,
) -> str | None:
    if offset + limit >= total_count:
        return None
    return _encode_cursor(SearchCursor(kind=kind, offset=offset + limit, filter_hash=filter_hash))


def _encode_cursor(cursor: SearchCursor) -> str:
    payload = cursor.model_dump(mode="json")
    encoded = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode("utf-8"))
    return encoded.decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> SearchCursor:
    padding = "=" * (-len(value) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(value + padding).decode("utf-8"))
        return SearchCursor.model_validate(payload)
    except Exception as exc:
        raise ValueError("Cursor cannot be decoded") from exc


def _decode_cursor_result(
    cursor: str,
    *,
    kind: Literal["transactions", "settlements", "pledges", "projects", "promotions"],
    filter_hash: str,
) -> SearchCursor | ErrorInfo:
    try:
        parsed = _decode_cursor(cursor)
    except ValueError as exc:
        return ErrorInfo(code="invalid_cursor", message=str(exc))
    if parsed.kind != kind or parsed.filter_hash != filter_hash:
        return ErrorInfo(code="cursor_mismatch", message="Cursor does not match this query")
    return parsed


def _control_totals(records: list[Any]) -> ControlTotals:
    by_currency: dict[str, dict[str, str | int]] = {}
    sent_by_currency: dict[str, dict[str, str | int]] = {}
    outgoing_by_currency: dict[str, dict[str, str | int]] = {}
    by_state: dict[str, int] = {}
    for record in records:
        states = getattr(record, "states", None)
        state = getattr(record, "state", None) or getattr(states, "state", None) or "unknown"
        by_state[state] = by_state.get(state, 0) + 1
        amounts = getattr(record, "amounts", None)
        money = None
        if amounts is not None:
            money = amounts.sent or amounts.pledged or amounts.collected_estimate or amounts.target
        elif isinstance(record, DarujmeTransaction):
            money = record.sent_amount
        if money is not None and money.currency is not None and money.amount is not None:
            _add_money_total(by_currency, money)
        if isinstance(record, DarujmeTransaction):
            _add_money_total(sent_by_currency, record.sent_amount)
            _add_money_total(outgoing_by_currency, record.outgoing_amount)
        elif amounts is not None:
            _add_money_total(sent_by_currency, amounts.sent)
            _add_money_total(outgoing_by_currency, amounts.outgoing)
    return ControlTotals(
        count=len(records),
        by_currency=by_currency,
        sent_by_currency=sent_by_currency,
        outgoing_by_currency=outgoing_by_currency,
        by_state=by_state,
    )


def _add_money_total(bucket_map: dict[str, dict[str, str | int]], money: Any) -> None:
    if money is None or money.currency is None or money.amount is None:
        return
    bucket = bucket_map.setdefault(money.currency, {"count": 0, "amount": "0"})
    bucket["count"] = int(bucket["count"]) + 1
    bucket["amount"] = f"{float(str(bucket['amount'])) + float(money.amount):.2f}"


def _donor_key(donor: dict[str, Any], pledge_id: int | None) -> str:
    if donor.get("redacted"):
        return f"pledge:{pledge_id or 'unknown'}"
    for key in ("email", "name", "company_identification_number"):
        if donor.get(key):
            return f"{key}:{donor[key]}"
    return f"pledge:{pledge_id or 'unknown'}"


def _api_date(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _validate_ids_mode(mode: str, ids: list[int], label: str) -> None:
    if mode == "by_ids":
        if not ids:
            raise ValueError(f"{label} are required when mode is by_ids")
    elif ids:
        raise ValueError(f"{label} are only allowed when mode is by_ids")


def _validate_date_range(start: date | None, end: date | None, label: str) -> None:
    if start is not None and end is not None and end < start:
        raise ValueError(f"{label}_to must be on or after {label}_from")


def _filter_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _error_info(exc: Exception) -> ErrorInfo:
    if isinstance(exc, DarujmeError):
        return ErrorInfo(code=exc.code, message=str(exc))
    if isinstance(exc, httpx.RequestError):
        return ErrorInfo(code="network_error", message=str(exc))
    if isinstance(exc, NotAuthenticatedError):
        return ErrorInfo(code="not_configured", message=str(exc))
    return ErrorInfo(code="error", message=str(exc))


def main() -> None:
    asyncio.run(mcp.run_async())


mcp.tool(darujme_login, app=True)


if __name__ == "__main__":
    main()
