from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import json
import secrets
import threading
from datetime import date
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
    DarujmeTransaction,
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
                    "received_from": "2026-05-01",
                    "received_to": "2026-05-16",
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
    received_from: date | None = None
    received_to: date | None = None
    outgoing_from: date | None = None
    outgoing_to: date | None = None
    failed_from: date | None = None
    failed_to: date | None = None
    outgoing_variable_symbol: str | None = None
    outgoing_amount: Decimal | None = None
    outgoing_currency: str | None = None
    outgoing_bank_account: str | None = None
    last_modified_date_time: str | None = None
    transaction_states: list[TransactionState] = Field(default_factory=list)
    limit: int = Field(default=100, ge=1, le=MAX_PAGE_LIMIT)
    cursor: str | None = None

    @model_validator(mode="after")
    def validate_mode(self) -> FindTransactionsQuery:
        _validate_ids_mode(self.mode, self.ids, "ids")
        _validate_date_range(self.received_from, self.received_to, "received")
        _validate_date_range(self.outgoing_from, self.outgoing_to, "outgoing")
        _validate_date_range(self.failed_from, self.failed_to, "failed")
        return self

    def filter_hash(self) -> str:
        return _filter_hash(
            {
                "mode": self.mode,
                "ids": self.ids,
                "project_ids": sorted(self.project_ids),
                "promotion_ids": sorted(self.promotion_ids),
                "received_from": _api_date(self.received_from),
                "received_to": _api_date(self.received_to),
                "outgoing_from": _api_date(self.outgoing_from),
                "outgoing_to": _api_date(self.outgoing_to),
                "failed_from": _api_date(self.failed_from),
                "failed_to": _api_date(self.failed_to),
                "outgoing_variable_symbol": self.outgoing_variable_symbol,
                "outgoing_amount": str(self.outgoing_amount)
                if self.outgoing_amount is not None
                else None,
                "outgoing_currency": self.outgoing_currency,
                "outgoing_bank_account": self.outgoing_bank_account,
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
                "mode=direct requires credentials.api_id, credentials.api_secret, "
                "and credentials.organization_id."
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
    """Prepare read-only donation confirmation groups from transaction and pledge data."""
    return await _prepare_donation_confirmations(_client_from_context(ctx), request)


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
        records = _filter_transactions(
            [
                normalize_transaction(
                    raw,
                    include_donor_pii=query.include_donor_pii,
                    include_raw=query.include_raw,
                )
                for raw in raws
            ],
            query,
        )
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


async def _prepare_donation_confirmations(
    client: DarujmeClient,
    request: DonationConfirmationRequest,
) -> DonationConfirmationsResult:
    query = FindTransactionsQuery(
        mode="search",
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


def _transaction_params(query: FindTransactionsQuery, *, offset: int) -> dict[str, Any]:
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


def _filter_transactions(
    records: list[DarujmeTransaction],
    query: FindTransactionsQuery,
) -> list[DarujmeTransaction]:
    result = records
    if query.outgoing_variable_symbol:
        result = [
            transaction
            for transaction in result
            if transaction.outgoing_variable_symbol == query.outgoing_variable_symbol
        ]
    if query.outgoing_amount is not None:
        result = [
            transaction
            for transaction in result
            if _money_amount(transaction.outgoing_amount) == query.outgoing_amount
        ]
    if query.outgoing_currency:
        result = [
            transaction
            for transaction in result
            if transaction.outgoing_amount is not None
            and transaction.outgoing_amount.currency == query.outgoing_currency
        ]
    if query.outgoing_bank_account:
        result = [
            transaction
            for transaction in result
            if transaction.outgoing_bank_account == query.outgoing_bank_account
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


def _metadata_result() -> MetadataResult:
    return MetadataResult(
        source_documents=[
            "https://www.darujme.cz/doc/api/v1/index.html",
            "https://documenter.getpostman.com/view/10150431/T1LS9jWA",
            "https://www.darujme.cz/dar/api/darujme_api.php?api_id=%s&api_secret=%s&typ_dotazu=1",
        ],
        comparison_fields={
            "date_filters": [
                "received_from",
                "received_to",
                "outgoing_from",
                "outgoing_to",
                "failed_from",
                "failed_to",
            ],
            "donor_amount": "sent_amount",
            "bank_payout_fields": [
                "outgoing_amount",
                "outgoing_currency",
                "outgoing_variable_symbol",
                "outgoing_bank_account",
            ],
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
    sent_by_currency: dict[str, dict[str, str | int]] = {}
    outgoing_by_currency: dict[str, dict[str, str | int]] = {}
    by_state: dict[str, int] = {}
    for record in records:
        state = getattr(record, "state", None) or getattr(record.states, "state", None) or "unknown"
        by_state[state] = by_state.get(state, 0) + 1
        amounts = getattr(record, "amounts", None)
        money = None
        if amounts is not None:
            money = amounts.sent or amounts.pledged or amounts.collected_estimate or amounts.target
        if money is not None and money.currency is not None and money.amount is not None:
            _add_money_total(by_currency, money)
        if amounts is not None:
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
