# Design

`darujme-mcp` follows the same local shape as the sibling `fio-mcp` and `simpleshop-mcp` projects:

- `src/server.py` owns FastMCP tool contracts, cursor validation, result models for tool-only metadata, and login form wiring.
- `src/client.py` is a narrow Darujme API v1 wrapper. It only calls read endpoints and adds `apiId` and `apiSecret` as query parameters.
- `src/settings.py` loads `.env`, then a credential store scoped to the canonical server process `cwd`: OS keyring plus a `0600` fallback credential file. Legacy unscoped stores are not read.
- `src/normalization.py` converts Darujme native payloads to agent-friendly records with generic source fields.
- `src/models.py` contains normalized Pydantic response objects.

## Read-Only Boundary

V1 excludes Darujme write endpoints, including pledge custom-field updates and promotion creation. `darujme_prepare_gift_confirmations` only groups data already returned by read endpoints; it does not create PDFs, send emails, or update Darujme.

## Login Contract

Darujme login requires `api_id`, `api_secret`, and `organization_id`. The organization ID is not inferred from the token because Darujme API v1 does not expose token introspection or organization discovery, while organization-scoped endpoints require `organizationId` in the URL.

## Pagination

Darujme `pageSize` and `offset` are wrapped in opaque cursors. Cursor payloads include the tool kind, offset, and a hash of all filters and response-shaping flags. Reusing a cursor with changed filters returns `cursor_mismatch`.

## Privacy

Donor PII is redacted by default. This includes names, email, phone, address, company identifiers, custom fields, comments, and confirmation recipient fields. Raw payloads are only exposed when `include_donor_pii=true`, because Darujme raw pledge data can contain donor identity fields.

## Normalized Shape

Records expose common fields that agents can compose with other systems:

- `source_system`, `source_id`, `source_key`, `source_number`
- `dates`, `amounts`, `states`
- `project`, `promotion`, `donor`
- `raw` when explicitly requested

Transaction records also preserve Darujme-native fields such as `transaction_id`, `presentable_code`, `state`, `sent_amount`, `received_at`, `outgoing_amount`, `outgoing_vs`, `outgoing_bank_account`, and `last_modified_at`.
