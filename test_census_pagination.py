"""Tests for census_request_all_pages — bounded, slimmed auto-pagination.

Regression coverage for the production 502. Two root causes:
  1. The original loop was unbounded (no page cap, no anti-stall guard) and its
     multi-page path was never exercised before shipping.
  2. Census list records are huge (a 25-record page is ~1.2 MB); aggregating
     every page produced a multi-MB response that the origin/proxy couldn't
     return -> 502 origin_bad_gateway.

These tests drive the real page-advancement, per-record slimming, and byte-budget
logic with mocked HTTP responses.
"""

import asyncio
import os

# census_tools reads CENSUS_API_TOKEN into a module global at import time.
os.environ.setdefault("CENSUS_API_TOKEN", "test_token")

import census_tools
from census_tools import (
    census_request_all_pages,
    CENSUS_MAX_PAGES,
)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """Stands in for httpx.AsyncClient; returns whatever `responder(page)` gives."""

    def __init__(self, responder):
        self._responder = responder
        self.request_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, headers=None, params=None, timeout=None):
        self.request_count += 1
        page = (params or {}).get("page", 1)
        return _FakeResponse(self._responder(page))


def _install_fake(monkeypatch, responder):
    client = _FakeClient(responder)
    monkeypatch.setattr(census_tools.httpx, "AsyncClient", lambda *a, **k: client)
    return client


def _heavy_field():
    # Serializes to well over CENSUS_HEAVY_FIELD_BYTES (1 KB).
    return [{"column": "x" * 60, "to": "y" * 60} for _ in range(40)]


def test_single_page_slims_heavy_fields(monkeypatch):
    rec = {"id": 1, "name": "sync-a", "status": "active", "mappings": _heavy_field()}
    client = _install_fake(
        monkeypatch,
        lambda page: {"data": [rec], "pagination": {"total_records": 1, "next_page": None}},
    )
    result = asyncio.run(census_request_all_pages("/api/v1/syncs"))

    assert client.request_count == 1
    assert result["returned_records"] == 1
    item = result["data"][0]
    assert item == {"id": 1, "name": "sync-a", "status": "active"}  # scalars kept
    assert "mappings" not in item                                   # heavy field dropped
    assert result["fields_omitted_for_size"] == ["mappings"]
    assert "note" in result


def test_multi_page_aggregates_and_terminates(monkeypatch):
    # Small scalar records; next_page advances 1->2->3 then None. Nothing trimmed.
    pages = {
        1: {"data": [{"id": i} for i in range(100)], "pagination": {"total_records": 230, "next_page": 2}},
        2: {"data": [{"id": i} for i in range(100)], "pagination": {"total_records": 230, "next_page": 3}},
        3: {"data": [{"id": i} for i in range(30)], "pagination": {"total_records": 230, "next_page": None}},
    }
    client = _install_fake(monkeypatch, lambda page: pages[page])
    result = asyncio.run(census_request_all_pages("/api/v1/syncs"))

    assert client.request_count == 3
    assert result["returned_records"] == 230
    assert result["total_records"] == 230
    assert "truncated" not in result
    assert "fields_omitted_for_size" not in result


def test_non_advancing_next_page_does_not_loop_forever(monkeypatch):
    # Pathological API: every response says "next_page: 2" — the original bug.
    client = _install_fake(
        monkeypatch,
        lambda page: {"data": [{"id": 1}], "pagination": {"next_page": 2}},
    )
    result = asyncio.run(census_request_all_pages("/api/v1/syncs"))

    # Fetches page 1, then page 2, then sees page 2 again and stops — never spins.
    assert client.request_count == 2
    assert result["status"] == "success"


def test_runaway_pagination_is_capped(monkeypatch):
    # API that NEVER returns next_page=None — would page forever without the cap.
    client = _install_fake(
        monkeypatch,
        lambda page: {"data": [{"id": page}], "pagination": {"next_page": page + 1}},
    )
    result = asyncio.run(census_request_all_pages("/api/v1/sync_runs"))

    assert client.request_count == CENSUS_MAX_PAGES
    assert result["truncated"] is True


def test_byte_budget_stops_within_a_page(monkeypatch):
    monkeypatch.setattr(census_tools, "CENSUS_MAX_RESPONSE_BYTES", 500)
    # 100 small records on page 1; the budget should stop us partway through it.
    client = _install_fake(
        monkeypatch,
        lambda page: {
            "data": [{"id": i, "name": "x" * 50} for i in range(100)],
            "pagination": {"next_page": page + 1},
        },
    )
    result = asyncio.run(census_request_all_pages("/api/v1/syncs"))

    assert client.request_count == 1                 # stopped before fetching page 2
    assert result["truncated"] is True
    assert 0 < result["returned_records"] < 100


def test_total_records_reported_from_pagination(monkeypatch):
    # One page returned, but the API says there are more -> truncated + true total.
    client = _install_fake(
        monkeypatch,
        lambda page: {"data": [{"id": 1}, {"id": 2}], "pagination": {"total_records": 81, "next_page": None}},
    )
    result = asyncio.run(census_request_all_pages("/api/v1/syncs"))

    assert result["returned_records"] == 2
    assert result["total_records"] == 81
    assert result["truncated"] is True


def test_non_list_payload_returned_unchanged(monkeypatch):
    client = _install_fake(
        monkeypatch,
        lambda page: {"error": "something", "data": {"not": "a list"}},
    )
    result = asyncio.run(census_request_all_pages("/api/v1/syncs"))

    assert client.request_count == 1
    assert result == {"error": "something", "data": {"not": "a list"}}
