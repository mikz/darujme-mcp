# Design

`darujme-mcp` follows the same local MCP package shape as the sibling projects:

- `src/server.py` owns FastMCP tool contracts, cursor validation, result models for tool-only metadata, and login form wiring.
- `src/client.py` is a narrow Darujme API v1 wrapper. It only calls read endpoints and adds `apiId` and `apiSecret` as query parameters.
- `src/settings.py` loads `.env`, then a global credential store: OS keyring service `darujme-mcp` plus a `0600` fallback credential file at `~/.config/darujme-mcp/credentials.env`. Setting `DARUJME_SCOPED_CREDENTIALS=1` isolates credentials per server process `cwd`.
- `src/normalization.py` converts Darujme native payloads to agent-friendly records with generic source fields.
- `src/models.py` contains normalized Pydantic response objects.

## Read-Only Boundary

V1 excludes Darujme write endpoints, including pledge custom-field updates and promotion creation. `darujme_prepare_donation_confirmations` only groups data already returned by read endpoints; it does not create PDFs, send emails, or update Darujme.

## Login Contract

Darujme login requires `api_id`, `api_secret`, and `organization_id`. The organization ID is not inferred from the token because Darujme API v1 does not expose token introspection or organization discovery, while organization-scoped endpoints require `organizationId` in the URL.

## Pagination

Darujme `pageSize` and `offset` are wrapped in opaque cursors. Cursor payloads include the tool kind, offset, and a hash of all filters and response-shaping flags. Reusing a cursor with changed filters returns `cursor_mismatch`.

## Privacy

Donor PII is redacted by default. This includes names, email, phone, address, company identifiers, custom fields, comments, and confirmation recipient fields. Raw payloads are only exposed when `include_donor_pii=true`, because Darujme raw pledge data can contain donor identity fields.

## Normalized Shape

Records expose source-native identifiers and grouped field families:

- `transaction_id`, `pledge_id`, `project_id`, or `promotion_id`
- `dates`, `amounts`, `states`
- `project`, `promotion`, `donor`
- `raw` when explicitly requested

Transaction records also preserve Darujme-native fields such as `transaction_id`, `presentable_code`, `state`, `sent_amount`, `received_at`, `outgoing_amount`, `outgoing_variable_symbol`, `outgoing_bank_account`, and `last_modified_at`.

`darujme_find_transactions` uses `query_type` as a discriminator. Besides
transaction row search and ID lookup, `settlement_aggregate` returns
organization payout rows for bank payout reconciliation and bank statement
matching. Rows are grouped from real outgoing transaction lines by settled day,
outgoing bank account, outgoing variable symbol, and currency.
The aggregate path queries Darujme one outgoing day at a time because the API
supports outgoing-date filtering but does not reliably return an outgoing date
field on each transaction.

Search tool dates use `*_from` and `*_to` field names consistently:
`received_from`, `received_to`, `outgoing_from`, `outgoing_to`,
`failed_from`, and `failed_to`. These fields map directly to Darujme's
`fromReceivedDate`, `toReceivedDate`, `fromOutgoingDate`, `toOutgoingDate`,
`fromFailedDate`, and `toFailedDate` API parameters.
