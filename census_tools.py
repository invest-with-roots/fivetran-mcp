"""Activations (formerly Census) tool definitions for the Fivetran MCP server.

Activations is Fivetran's reverse-ETL product (the rebranded Census). Its API is
SEPARATE from the core Fivetran API: a different host (app.getcensus.com) and a
different auth scheme (Bearer workspace access token, not Basic key:secret).

This module is intentionally self-contained so it can be merged into server.py's
TOOLS/PARAM_DEFINITIONS with a minimal patch, keeping upstream rebases clean.
Tools are routed to census_request() because their config carries `"api": "census"`.
"""

import json
import os
from typing import Any

import httpx

# Workspace access token (manages syncs/connections/models within one workspace).
CENSUS_API_TOKEN = os.getenv("CENSUS_API_TOKEN")
# US: https://app.getcensus.com  •  EU: https://app-eu.getcensus.com
CENSUS_BASE_URL = os.getenv("CENSUS_BASE_URL", "https://app.getcensus.com").rstrip("/")
# Reuse the same write gate as core Fivetran so writes stay off by default.
_ALLOW_WRITES = os.getenv("FIVETRAN_ALLOW_WRITES", "false").lower() == "true"


def get_census_auth_header() -> dict[str, str]:
    """Bearer auth header for the Activations (Census) API."""
    if not CENSUS_API_TOKEN:
        raise ValueError(
            "CENSUS_API_TOKEN must be set to use Activations (Census) tools. "
            "Generate a workspace access token in the Activations/Census UI."
        )
    return {
        "Authorization": f"Bearer {CENSUS_API_TOKEN}",
        "Accept": "application/json",
        "User-Agent": "fivetran-official-mcp",
    }


async def census_request(
    method: str,
    endpoint: str,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Make a request to the Activations (Census) API."""
    if method != "GET" and not _ALLOW_WRITES:
        raise ValueError(
            f"Write operations ({method}) are disabled. "
            "Set FIVETRAN_ALLOW_WRITES=true to enable."
        )
    url = f"{CENSUS_BASE_URL}{endpoint}"
    async with httpx.AsyncClient() as client:
        response = await client.request(
            method=method,
            url=url,
            headers=get_census_auth_header(),
            params=params,
            json=json_body,
            timeout=30.0,
        )
        response.raise_for_status()
        return response.json()


# Auto-pagination safety bounds.
#
# Census list records are LARGE: each sync carries its full `mappings`,
# `source_attributes`, and `destination_attributes` (~48 KB/record), so a single
# 25-record page is ~1.2 MB and all syncs run to several MB. Returning that
# verbatim blows the MCP response/token limit, and aggregating every page into
# one payload was the real cause of the 502 origin_bad_gateway (an oversized
# origin response), not an infinite loop.
#
# So we: (1) bound page count, (2) drop heavy nested fields per record — keeping
# scalars like id/name/status/timestamps; the full objects remain available via
# the detail tools (census_get_*), and (3) enforce an overall byte budget. Any
# of these limits sets `truncated`/`fields_omitted_for_size` so the caller knows
# data was reduced. We also stop if `next_page` fails to advance, so a
# misbehaving response can never spin the loop forever.
CENSUS_PER_PAGE = 100
CENSUS_MAX_PAGES = 20
CENSUS_HEAVY_FIELD_BYTES = 1024  # drop a record's nested field if it serializes larger than this
CENSUS_MAX_RESPONSE_BYTES = 300_000  # overall budget for the combined `data`


def _slim_record(record: Any) -> tuple[Any, list[str]]:
    """Drop a record's heavy nested (dict/list) fields; keep scalars + small fields.

    Returns (slimmed_record, dropped_field_names). Non-dict records pass through.
    """
    if not isinstance(record, dict):
        return record, []
    slim: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in record.items():
        if isinstance(value, (dict, list)):
            size = len(json.dumps(value, default=str))
            if size > CENSUS_HEAVY_FIELD_BYTES:
                dropped.append(key)
                continue
        slim[key] = value
    return slim, dropped


async def census_request_all_pages(
    endpoint: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """GET pages from a Census list endpoint and return slimmed, bounded items.

    Census uses page-based pagination: response has `data` (list) and
    `pagination.next_page` (None when there are no more pages). Always a GET,
    so no write-permission check needed.

    Bounded by CENSUS_MAX_PAGES, CENSUS_MAX_RESPONSE_BYTES, and per-record
    field slimming (see note above). When any bound trims the result, the
    response carries `truncated: true` / `fields_omitted_for_size` plus a `note`.
    """
    all_items: list[Any] = []
    dropped_fields: set[str] = set()
    params = dict(params or {})
    params["per_page"] = CENSUS_PER_PAGE
    page = 1
    seen_pages: set[Any] = set()
    api_total: int | None = None
    used_bytes = 0
    truncated = False
    async with httpx.AsyncClient() as client:
        while True:
            # Anti-stall guard: if the API hands back a next_page we've already
            # fetched (or one that doesn't advance), stop rather than loop forever.
            if page in seen_pages:
                break
            seen_pages.add(page)

            params["page"] = page
            url = f"{CENSUS_BASE_URL}{endpoint}"
            response = await client.request(
                method="GET",
                url=url,
                headers=get_census_auth_header(),
                params=params,
                timeout=30.0,
            )
            response.raise_for_status()
            result = response.json()
            data = result.get("data", [])
            if not isinstance(data, list):
                # Not a paginated list payload — return the raw response unchanged.
                return result

            pagination = result.get("pagination") or {}
            if isinstance(pagination.get("total_records"), int):
                api_total = pagination["total_records"]

            # Slim each record and enforce the overall byte budget.
            for record in data:
                slim, dropped = _slim_record(record)
                dropped_fields.update(dropped)
                used_bytes += len(json.dumps(slim, default=str))
                if used_bytes > CENSUS_MAX_RESPONSE_BYTES:
                    truncated = True
                    break
                all_items.append(slim)
            if truncated:
                break

            next_page = pagination.get("next_page")
            if not next_page:
                break
            if len(seen_pages) >= CENSUS_MAX_PAGES:
                truncated = True
                break
            page = next_page

    out: dict[str, Any] = {
        "status": "success",
        "total_records": api_total if api_total is not None else len(all_items),
        "returned_records": len(all_items),
        "data": all_items,
    }
    if dropped_fields:
        out["fields_omitted_for_size"] = sorted(dropped_fields)
    if truncated or (api_total is not None and api_total > len(all_items)):
        out["truncated"] = True
    if out.get("truncated") or dropped_fields:
        out["note"] = (
            f"Returned {len(all_items)} of {out['total_records']} records; "
            "large nested fields were omitted to stay within response limits. "
            "Use the detail tools (e.g. census_get_sync) for a specific record's full config."
        )
    return out


# Path params used by Census tools that aren't already in server.py's PARAM_DEFINITIONS.
CENSUS_PARAM_DEFINITIONS = {
    "sync_id": {"type": "string", "description": "The unique identifier for the Activations (Census) sync"},
    "source_id": {"type": "string", "description": "The unique identifier for the Activations source connection"},
}

# All entries carry "api": "census" so execute_tool() routes them to census_request().
# Endpoints include the /api/v1 prefix (CENSUS_BASE_URL is just the host).
CENSUS_TOOLS = {
    "census_list_syncs": {
        "description": "Activations: List all syncs in the workspace (reverse-ETL pipelines from a source to a destination).",
        "schema_file": "open-api-definitions/census/census_list_syncs.json",
        "method": "GET",
        "endpoint": "/api/v1/syncs",
        "auto_paginate": True,
        "api": "census",
    },
    "census_get_sync": {
        "description": "Activations: Get configuration and status details for a single sync.",
        "schema_file": "open-api-definitions/census/census_get_sync.json",
        "method": "GET",
        "endpoint": "/api/v1/syncs/{sync_id}",
        "params": ["sync_id"],
        "api": "census",
    },
    "census_list_sync_runs": {
        "description": "Activations: List recent run history (status, records processed, errors) for a sync.",
        "schema_file": "open-api-definitions/census/census_list_sync_runs.json",
        "method": "GET",
        "endpoint": "/api/v1/syncs/{sync_id}/sync_runs",
        "params": ["sync_id"],
        "auto_paginate": True,
        "api": "census",
    },
    "census_list_sources": {
        "description": "Activations: List source connections (the data warehouses/databases syncs read from).",
        "schema_file": "open-api-definitions/census/census_list_sources.json",
        "method": "GET",
        "endpoint": "/api/v1/sources",
        "auto_paginate": True,
        "api": "census",
    },
    "census_get_source": {
        "description": "Activations: Get details for a single source connection.",
        "schema_file": "open-api-definitions/census/census_get_source.json",
        "method": "GET",
        "endpoint": "/api/v1/sources/{source_id}",
        "params": ["source_id"],
        "api": "census",
    },
    "census_list_destinations": {
        "description": "Activations: List destination connections (the SaaS tools/apps syncs write to).",
        "schema_file": "open-api-definitions/census/census_list_destinations.json",
        "method": "GET",
        "endpoint": "/api/v1/destinations",
        "auto_paginate": True,
        "api": "census",
    },
    "census_get_destination": {
        "description": "Activations: Get details for a single destination connection.",
        "schema_file": "open-api-definitions/census/census_get_destination.json",
        "method": "GET",
        "endpoint": "/api/v1/destinations/{destination_id}",
        "params": ["destination_id"],
        "api": "census",
    },
    "census_list_destination_objects": {
        "description": "Activations: List the objects (e.g. tables/entities) available in a destination connection.",
        "schema_file": "open-api-definitions/census/census_list_destination_objects.json",
        "method": "GET",
        "endpoint": "/api/v1/destinations/{destination_id}/objects",
        "params": ["destination_id"],
        "auto_paginate": True,
        "api": "census",
    },
    "census_list_models": {
        "description": "Activations: List SQL models defined in the workspace (saved queries used as sync sources).",
        "schema_file": "open-api-definitions/census/census_list_models.json",
        "method": "GET",
        "endpoint": "/api/v1/models",
        "auto_paginate": True,
        "api": "census",
    },
    "census_trigger_sync": {
        "description": "⚠️ WRITE OPERATION - Confirm with user before calling. Activations: Manually trigger a sync run.",
        "schema_file": "open-api-definitions/census/census_trigger_sync.json",
        "method": "POST",
        "endpoint": "/api/v1/syncs/{sync_id}/trigger",
        "params": ["sync_id"],
        "api": "census",
    },
}
