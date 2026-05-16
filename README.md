# darujme-mcp

Read-only FastMCP server for Darujme API v1 donation data.

V1 exposes Darujme transactions, pledges, projects, and peer-to-peer promotions in stable normalized shapes for accounting and donor-support agents. It intentionally does not mutate Darujme data, generate/send confirmations, or implement Fio-specific reconciliation heuristics.

## Setup

```bash
mise install
mise exec -- uv sync --locked
```

Credentials can be supplied with `.env`:

```dotenv
DARUJME_API_ID=...
DARUJME_API_SECRET=...
DARUJME_ORGANIZATION_ID=...
```

or with the `darujme_login` MCP tool. It supports `mode: "auto" | "direct" |
"prefab" | "web"`: Apps-capable clients get an inline Prefab form, while
clients such as Codex can use direct arguments or a localhost web form.
`DARUJME_ORGANIZATION_ID` is part of the login contract because Darujme API v1
requires `organizationId` in organization-scoped URLs and does not expose token
introspection or organization discovery. Stored credentials are scoped to the
canonical server process `cwd`: OS keyring service `darujme-mcp:<scope-id>` when
available, plus `${XDG_CONFIG_HOME:-$HOME/.config}/darujme-mcp/scopes/<scope-id>/credentials.env`
with mode `0600`. Legacy unscoped stores are not read.

## Run

```bash
mise exec -- uv run --locked darujme-mcp
mise run mcp-reload
```

## Tools

- `darujme_login`: stores and validates required `api_id`, `api_secret`, and `organization_id` using `auto`, `direct`, `prefab`, or `web` mode.
- `darujme_test_connection`: safe read-only credential check.
- `darujme_find_transactions`: `mode: "search" | "by_ids"`.
- `darujme_find_pledges`: `mode: "search" | "by_ids" | "by_vs"`.
- `darujme_find_projects`: `mode: "search" | "by_ids"`.
- `darujme_find_promotions`: `mode: "search" | "by_ids"`.
- `darujme_prepare_gift_confirmations`: read-only grouping for later confirmation workflows.
- `darujme_get_metadata`: states, modes, privacy, limits, and error metadata.

Donor PII is redacted by default. Set `include_donor_pii=true` to return names, contacts, addresses, company IDs, custom fields, and confirmation recipient fields. `include_raw=true` requires `include_donor_pii=true`.

## Checks

```bash
mise exec -- uv run --locked pytest
mise exec -- uv run --locked ruff check .
mise exec -- uv run --locked ruff format --check src tests
mise run e2e
```
