# Tool Contract

## `darujme_login`

Unified login tool. `mode` accepts `auto`, `direct`, `prefab`, or `web`.
`auto` uses Prefab when the MCP client advertises Apps UI support, otherwise it
returns a localhost web-login URL. `direct` accepts `api_id`, `api_secret`, and
`organization_id` in the `credentials` object. The server validates with
`GET /organization/{organizationId}/projects`, then stores credentials in the
cwd-scoped local credential store. The organization ID is required because
Darujme API v1 does not expose token introspection or organization discovery.

## `darujme_test_connection`

Returns:

```json
{ "ok": true, "organization_id": 2, "error": null }
```

## `darujme_find_transactions`

Query modes:

- `search`: calls `GET /organization/{organizationId}/transactions-by-filter`.
- `by_ids`: calls `GET /organization/{organizationId}/transaction/{transactionId}` for each ID and itemizes errors.

Search filters include project IDs, promotion IDs, received/outgoing/failed dates, last modified timestamp, transaction states, `limit`, and `cursor`.

Date filters use `received_from`, `received_to`, `outgoing_from`,
`outgoing_to`, `failed_from`, and `failed_to`. These are translated to the
Darujme API parameters `fromReceivedDate`, `toReceivedDate`,
`fromOutgoingDate`, `toOutgoingDate`, `fromFailedDate`, and `toFailedDate`.

Payout matching fields are exposed with full names:
`outgoing_variable_symbol`, `outgoing_amount`, `outgoing_currency`, and
`outgoing_bank_account`. `outgoing_variable_symbol` maps Darujme's native
`outgoingVs` response property.

`control_totals` includes `sent_by_currency` and `outgoing_by_currency` so the
donor-sent amount and organization payout amount are visible separately.

## `darujme_find_pledges`

Query modes:

- `search`: calls `GET /organization/{organizationId}/pledges-by-filter`.
- `by_ids`: calls `GET /organization/{organizationId}/pledge/{pledgeId}` for each ID.
- `by_vs`: calls `GET /organization/{organizationId}/pledges-by-vs/{vs}`.

Search date filters use `from_pledged_date`, `to_pledged_date`,
`received_from`, `received_to`, `outgoing_from`, and `outgoing_to`.

## `darujme_find_projects`

Query modes:

- `search`: calls `GET /organization/{organizationId}/projects`.
- `by_ids`: calls `GET /project/{projectId}` for each ID.

## `darujme_find_promotions`

Query modes:

- `search`: calls `GET /project/{projectId}/promotions` for one or more project IDs.
- `by_ids`: calls `GET /promotion/{promotionId}` for each ID.

## `darujme_prepare_donation_confirmations`

Read-only helper that fetches eligible transactions and groups them by donor/pledge for downstream confirmation workflows. It has no Darujme side effects.

## `darujme_get_metadata`

Returns query modes, transaction/project states, payment methods, currencies, privacy controls, limits, error codes, and read-only side-effect notes.
