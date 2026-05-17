# Tool Reference

Operational notes for maintainers running the darujme-mcp server. All
runtime information that an LLM client needs to use the tools — query
modes, parameter descriptions, the 3-level privacy hierarchy, settlement
aggregate semantics, transaction states, error codes — is exported
through the MCP JSON Schema and the `darujme_get_metadata` tool response.
This file is for humans who maintain the server.

## Read-only contract

All Darujme tools are read-only and use only Darujme API v1 read
endpoints. No tool mutates donor, pledge, project, or promotion data.
`darujme_login` only mutates local credential storage.

## Authentication and credential storage

`darujme_login` requires three values:

- `api_id`
- `api_secret`
- `organization_id`

The organization id is required because Darujme API v1 does not expose
token introspection or organization discovery — credentials are organization-
scoped in the URL. The server validates at login time via
`GET /organization/{organization_id}/projects`.

Credentials are stored in the cwd-scoped local credential store (system
keyring with file fallback).

### Pre-seeded credentials

```bash
export DARUJME_API_ID="..."
export DARUJME_API_SECRET="..."
export DARUJME_ORGANIZATION_ID="..."
```

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `DARUJME_BASE_URL` | `https://www.darujme.cz/api/v1/` | Override only for tests / sandboxes |
| `DARUJME_TIMEOUT_SECONDS` | `30` | Per-request timeout |

## Upstream endpoint map

For maintainers who need to trace MCP tool calls back to Darujme API v1
URLs:

| MCP tool / mode | Upstream call |
|---|---|
| `darujme_find_transactions` + `transaction_search` | `GET /organization/{org}/transactions-by-filter` |
| `darujme_find_transactions` + `transaction_by_ids` | `GET /organization/{org}/transaction/{id}` per id (itemized errors) |
| `darujme_find_transactions` + `settlement_aggregate` | `GET /organization/{org}/transactions-by-filter` per settled day, aggregated locally |
| `darujme_find_pledges` + `search` | `GET /organization/{org}/pledges-by-filter` |
| `darujme_find_pledges` + `by_ids` | `GET /organization/{org}/pledge/{id}` per id |
| `darujme_find_pledges` + `by_vs` | `GET /organization/{org}/pledges-by-vs/{vs}` |
| `darujme_find_projects` + `search` | `GET /organization/{org}/projects` |
| `darujme_find_projects` + `by_ids` | `GET /project/{id}` per id |
| `darujme_find_promotions` + `search` | `GET /project/{id}/promotions` per project id |
| `darujme_find_promotions` + `by_ids` | `GET /promotion/{id}` per id |

## Settlement aggregate implementation notes

Darujme does not return a reliable outgoing-date field on transactions; the
MCP maps each `settled_from`/`settled_to` date to Darujme's outgoing-date
filter, fetches the matching transactions, and groups them locally by
`(date, outgoing_bank_account, outgoing_variable_symbol, currency)` to
produce one aggregate row per Fio incoming-transfer line.

`MAX_SETTLEMENT_RANGE_DAYS` caps the requested window (currently 31) to
avoid pulling more than one month of donations per call.

## Side effects to remember

`darujme_login` writes to keyring + scoped credential file. No tool mutates
Darujme data; all reads are GET requests.

## Troubleshooting

| Symptom | Diagnosis |
|---|---|
| `not_configured` | Run `darujme_login` (or set the three `DARUJME_*` env vars and restart) |
| `auth_error` | api_id / api_secret rejected; check the organization's API settings |
| `not_found` | The requested transaction / pledge / project / promotion id does not exist for this organization |
| `invalid_request` | Filter combination or date range invalid (e.g. settlement window > 31 days) |
| `invalid_response` | Darujme returned an unexpected payload shape; report upstream |
| `cursor_mismatch` | Filter set changed between paginated calls; restart pagination without `cursor` |
| `network_error` | DNS / connect / timeout — verify `DARUJME_BASE_URL` and network |

The complete error-code catalog and the privacy-level table are in
`darujme_get_metadata`.
