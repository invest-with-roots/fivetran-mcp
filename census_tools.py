"""Activations (formerly Census) tool definitions for the Fivetran MCP server.

Activations is Fivetran's reverse-ETL product (the rebranded Census). Its API is
SEPARATE from the core Fivetran API: a different host (app.getcensus.com) and a
different auth scheme (Bearer workspace access token, not Basic key:secret).

This module is intentionally self-contained so it can be merged into server.py's
TOOLS/PARAM_DEFINITIONS with a minimal patch, keeping upstream rebases clean.
Tools are routed to census_request() because their config carries `"api": "census"`.
"""

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
        "api": "census",
    },
    "census_list_sources": {
        "description": "Activations: List source connections (the data warehouses/databases syncs read from).",
        "schema_file": "open-api-definitions/census/census_list_sources.json",
        "method": "GET",
        "endpoint": "/api/v1/sources",
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
        "api": "census",
    },
    "census_list_models": {
        "description": "Activations: List SQL models defined in the workspace (saved queries used as sync sources).",
        "schema_file": "open-api-definitions/census/census_list_models.json",
        "method": "GET",
        "endpoint": "/api/v1/models",
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
