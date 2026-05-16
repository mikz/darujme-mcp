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

Query variants use `query_type` as a schema discriminator:

- `transaction_search`: calls `GET /organization/{organizationId}/transactions-by-filter`.
- `transaction_by_ids`: calls `GET /organization/{organizationId}/transaction/{transactionId}` for each ID and itemizes errors.
- `settlement_aggregate`: bank payout reconciliation path. Calls `transactions-by-filter` per settled day and returns organization payout rows grouped by outgoing bank account, outgoing variable symbol, and currency for bank statement matching.

Transaction search filters include project IDs, promotion IDs,
received/outgoing/failed dates, last modified timestamp, transaction states,
`limit`, and `cursor`.

Transaction date filters use `received_from`, `received_to`, `outgoing_from`,
`outgoing_to`, `failed_from`, and `failed_to`. These are translated to the
Darujme API parameters `fromReceivedDate`, `toReceivedDate`,
`fromOutgoingDate`, `toOutgoingDate`, `fromFailedDate`, and `toFailedDate`.

Settlement aggregate queries use `settled_from` and `settled_to`; the MCP maps
each day to Darujme's outgoing date filter because Darujme does not return a
reliable outgoing date field on transactions. For bank statement matching, pass
`bank_account`, `variable_symbol`, `currency`, and `amount` when those values
are known. Aggregate organization payout rows expose
`date`, `bank_account`, `variable_symbol`, `currency`, `amount`, `sent_total`,
`fee_total`, `transaction_count`, and `transaction_ids`.

```json
{
  "query_type": "settlement_aggregate",
  "settled_from": "2026-03-10",
  "settled_to": "2026-03-10",
  "bank_account": "2603445200/2010",
  "variable_symbol": "260310661",
  "currency": "CZK",
  "amount": "1926.00",
  "limit": 100
}
```

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
