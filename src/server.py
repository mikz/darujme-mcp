from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import json
from datetime import date
from typing import Annotated, Any, Literal

import httpx
from fastmcp import FastMCP
from fastmcp.apps import UI_EXTENSION_ID
from fastmcp.apps.form import FormInput
from fastmcp.exceptions import ToolError
from fastmcp.server.context import Context
from fastmcp.server.lifespan import lifespan
from pydantic import BaseModel, ConfigDict, Field, model_validator

from client import DarujmeClient, DarujmeError, NotAuthenticatedError
from models import (
    ControlTotals,
    ErrorInfo,
    FindPledgesResult,
    FindProjectsResult,
    FindPromotionsResult,
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


class SearchCursor(BaseModel):
    v: int = 1
    kind: Literal["transactions", "pledges", "projects", "promotions"]
    offset: int
    filter_hash: str


class PrivacyMixin(BaseModel):
    include_donor_pii: bool = Field(
        default=False,
        description=(
            "Return donor names, email, phone, address, company IDs, custom fields, and "
            "confirmation recipient fields. Defaults to redacted."
        ),
    )
    include_raw: bool = Field(
        default=False,
        description="Return raw Darujme payloads. Requires include_donor_pii=true.",
    )

    @model_validator(mode="after")
    def validate_raw_privacy(self) -> PrivacyMixin:
        if self.include_raw and not self.include_donor_pii:
            raise ValueError("include_raw requires include_donor_pii=true")
        return self


class FindTransactionsQuery(PrivacyMixin):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "mode": "search",
                    "from_received_date": "2026-05-01",
                    "to_received_date": "2026-05-16",
                    "transaction_states": ["success", "sent_to_organization"],
                    "limit": 100,
                    "include_donor_pii": False,
                },
                {"mode": "by_ids", "ids": [1203450], "include_donor_pii": False},
            ]
        },
    )

    mode: Literal["search", "by_ids"] = Field(
        description='Use "search" with filters or "by_ids" for known transaction IDs.'
    )
    ids: list[int] = Field(default_factory=list, max_length=100)
    project_ids: list[int] = Field(default_factory=list)
    promotion_ids: list[int] = Field(default_factory=list)
    from_received_date: date | None = None
    to_received_date: date | None = None
    from_outgoing_date: date | None = None
    to_outgoing_date: date | None = None
    from_failed_date: date | None = None
    to_failed_date: date | None = None
    last_modified_date_time: str | None = None
    transaction_states: list[TransactionState] = Field(default_factory=list)
    limit: int = Field(default=100, ge=1, le=MAX_PAGE_LIMIT)
    cursor: str | None = None

    @model_validator(mode="after")
    def validate_mode(self) -> FindTransactionsQuery:
        _validate_ids_mode(self.mode, self.ids, "ids")
        return self

    def filter_hash(self) -> str:
        return _filter_hash(
            {
                "mode": self.mode,
                "ids": self.ids,
                "project_ids": sorted(self.project_ids),
                "promotion_ids": sorted(self.promotion_ids),
                "from_received_date": _api_date(self.from_received_date),
                "to_received_date": _api_date(self.to_received_date),
                "from_outgoing_date": _api_date(self.from_outgoing_date),
                "to_outgoing_date": _api_date(self.to_outgoing_date),
                "from_failed_date": _api_date(self.from_failed_date),
                "to_failed_date": _api_date(self.to_failed_date),
                "last_modified_date_time": self.last_modified_date_time,
                "transaction_states": sorted(self.transaction_states),
                "include_donor_pii": self.include_donor_pii,
                "include_raw": self.include_raw,
            }
        )


class FindPledgesQuery(PrivacyMixin):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "mode": "search",
                    "from_pledged_date": "2026-05-01",
                    "to_pledged_date": "2026-05-16",
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
    from_received_date: date | None = None
    to_received_date: date | None = None
    from_outgoing_date: date | None = None
    to_outgoing_date: date | None = None
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
                "from_received_date": _api_date(self.from_received_date),
                "to_received_date": _api_date(self.to_received_date),
                "from_outgoing_date": _api_date(self.from_outgoing_date),
                "to_outgoing_date": _api_date(self.to_outgoing_date),
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


class GiftConfirmationRequest(PrivacyMixin):
    model_config = ConfigDict(extra="forbid")

    from_received_date: date | None = None
    to_received_date: date | None = None
    project_ids: list[int] = Field(default_factory=list)
    promotion_ids: list[int] = Field(default_factory=list)
    transaction_states: list[TransactionState] = Field(
        default_factory=lambda: ["success", "success_money_on_account", "sent_to_organization"]
    )
    limit: int = Field(default=100, ge=1, le=MAX_PAGE_LIMIT)
    cursor: str | None = None


class ConfirmationGroup(BaseModel):
    donor: dict[str, Any]
    pledge_id: int | None = None
    project_id: int | None = None
    promotion_id: int | None = None
    transactions: list[dict[str, Any]] = Field(default_factory=list)
    total_by_currency: dict[str, str] = Field(default_factory=dict)


class GiftConfirmationsResult(BaseModel):
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
    cursor_pagination: str


class MetadataResult(BaseModel):
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


mcp.add_provider(
    FormInput(
        model=DarujmeLogin,
        tool_name="darujme_login",
        title="Sign in to Darujme",
        submit_text="Sign in",
        on_submit=_login_on_submit,
    )
)


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
                "Not signed in to Darujme. Call the `darujme_login` tool first; "
                "the user will be prompted for api_id, api_secret, and organization_id."
            )
            if not ctx.client_supports_extension(UI_EXTENSION_ID):
                hint += (
                    " This client does not render inline forms; credentials can also be set "
                    "with DARUJME_API_ID, DARUJME_API_SECRET, and DARUJME_ORGANIZATION_ID, "
                    "or pre-seeded in the cwd-scoped credential store."
                )
            raise ToolError(hint)
        return await fn(*args, **kwargs)

    return wrapper


@mcp.tool
@_requires_login
async def darujme_test_connection(ctx: Context) -> TestConnectionResult:
    """Verify that configured Darujme credentials can perform a read-only API call."""
    return await _test_connection(_client_from_context(ctx))


@mcp.tool
@_requires_login
async def darujme_find_transactions(
    query: Annotated[
        FindTransactionsQuery,
        Field(description="Object query for Darujme transactions. Pass an object, not a string."),
    ],
    ctx: Context,
) -> FindTransactionsResult:
    """Find Darujme transactions by search filters or known transaction IDs."""
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
    """Find Darujme pledges by search filters, known pledge IDs, or variable symbol."""
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
    """Find Darujme projects by organization listing or known project IDs."""
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
    """Find Darujme peer-to-peer promotions by project listing or known promotion IDs."""
    return await _find_promotions(_client_from_context(ctx), query)


@mcp.tool
@_requires_login
async def darujme_prepare_gift_confirmations(
    request: Annotated[
        GiftConfirmationRequest,
        Field(
            description=(
                "Read-only grouping of eligible gifts for later confirmation workflows. "
                "No PDFs are generated and nothing is sent."
            )
        ),
    ],
    ctx: Context,
) -> GiftConfirmationsResult:
    """Prepare read-only gift confirmation groups from transaction and pledge data."""
    return await _prepare_gift_confirmations(_client_from_context(ctx), request)


@mcp.tool
async def darujme_get_metadata() -> MetadataResult:
    """Return Darujme states, query modes, privacy controls, limits, and side-effect notes."""
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
) -> FindTransactionsResult:
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
        if query.mode == "by_ids":
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


async def _prepare_gift_confirmations(
    client: DarujmeClient,
    request: GiftConfirmationRequest,
) -> GiftConfirmationsResult:
    query = FindTransactionsQuery(
        mode="search",
        project_ids=request.project_ids,
        promotion_ids=request.promotion_ids,
        from_received_date=request.from_received_date,
        to_received_date=request.to_received_date,
        transaction_states=request.transaction_states,
        limit=request.limit,
        cursor=request.cursor,
        include_donor_pii=request.include_donor_pii,
        include_raw=request.include_raw,
    )
    result = await _find_transactions(client, query)
    if result.error is not None:
        return GiftConfirmationsResult(error=result.error)
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
    return GiftConfirmationsResult(
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


def _transaction_params(query: FindTransactionsQuery, *, offset: int) -> dict[str, Any]:
    return {
        "projectIds[]": query.project_ids,
        "promotionIds[]": query.promotion_ids,
        "fromReceivedDate": _api_date(query.from_received_date),
        "toReceivedDate": _api_date(query.to_received_date),
        "fromOutgoingDate": _api_date(query.from_outgoing_date),
        "toOutgoingDate": _api_date(query.to_outgoing_date),
        "fromFailedDate": _api_date(query.from_failed_date),
        "toFailedDate": _api_date(query.to_failed_date),
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
        "fromReceivedDate": _api_date(query.from_received_date),
        "toReceivedDate": _api_date(query.to_received_date),
        "fromOutgoingDate": _api_date(query.from_outgoing_date),
        "toOutgoingDate": _api_date(query.to_outgoing_date),
        "lastModifiedDateTime": query.last_modified_date_time,
        "paymentMethod[]": query.payment_methods,
        "recurrentState[]": query.recurrent_states,
        "pageSize": query.limit,
        "offset": offset,
    }


def _metadata_result() -> MetadataResult:
    return MetadataResult(
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
            "darujme_find_transactions": ["search", "by_ids"],
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
            "default": "Donor PII is redacted.",
            "include_donor_pii": (
                "Required for names, email, phone, address, company IDs, custom fields, "
                "and confirmation recipient fields."
            ),
            "include_raw": "Requires include_donor_pii=true.",
        },
        limits=MetadataLimits(
            max_page_limit=MAX_PAGE_LIMIT,
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
    kind: Literal["transactions", "pledges", "projects", "promotions"],
    filter_hash: str,
    offset: int,
    limit: int,
    returned_count: int,
) -> str | None:
    if returned_count < limit:
        return None
    return _encode_cursor(SearchCursor(kind=kind, offset=offset + limit, filter_hash=filter_hash))


def _next_cursor_from_total(
    kind: Literal["transactions", "pledges", "projects", "promotions"],
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
    kind: Literal["transactions", "pledges", "projects", "promotions"],
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
    by_state: dict[str, int] = {}
    for record in records:
        state = getattr(record, "state", None) or getattr(record.states, "state", None) or "unknown"
        by_state[state] = by_state.get(state, 0) + 1
        amounts = getattr(record, "amounts", None)
        money = None
        if amounts is not None:
            money = amounts.sent or amounts.pledged or amounts.collected_estimate or amounts.target
        if money is not None and money.currency is not None and money.amount is not None:
            bucket = by_currency.setdefault(money.currency, {"count": 0, "amount": "0"})
            bucket["count"] = int(bucket["count"]) + 1
            bucket["amount"] = str(float(str(bucket["amount"])) + float(money.amount))
    return ControlTotals(count=len(records), by_currency=by_currency, by_state=by_state)


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


if __name__ == "__main__":
    main()
