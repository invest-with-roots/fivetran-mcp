"""Tests for census_request_all_pages — the bounded auto-pagination.

Regression coverage for the production 502: the original loop was unbounded in
both page count and total time, and its multi-page path was never exercised
(the only manual check hit a single-page, <100-item result). These tests drive
the actual page-advancement logic with mocked HTTP responses.
"""

import asyncio
import os

# census_tools reads CENSUS_API_TOKEN into a module global at import time.
os.environ.setdefault("CENSUS_API_TOKEN", "test_token")

import census_tools
from census_tools import (
    census_request_all_pages,
    CENSUS_MAX_PAGES,
    CENSUS_PER_PAGE,
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
    """Patch census_tools.httpx.AsyncClient and return the live FakeClient."""
    client = _FakeClient(responder)
    monkeypatch.setattr(census_tools.httpx, "AsyncClient", lambda *a, **k: client)
    return client


def test_single_page_returns_all_items_in_one_request(monkeypatch):
    client = _install_fake(
        monkeypatch,
        lambda page: {"data": list(range(40)), "pagination": {"next_page": None}},
    )
    result = asyncio.run(census_request_all_pages("/api/v1/syncs"))

    assert client.request_count == 1
    assert result["total_records"] == 40
    assert "truncated" not in result


def test_multi_page_aggregates_and_terminates(monkeypatch):
    # 3 real pages: 100, 100, 30 — next_page advances 1->2->3 then None.
    pages = {
        1: {"data": list(range(100)), "pagination": {"next_page": 2}},
        2: {"data": list(range(100)), "pagination": {"next_page": 3}},
        3: {"data": list(range(30)), "pagination": {"next_page": None}},
    }
    client = _install_fake(monkeypatch, lambda page: pages[page])
    result = asyncio.run(census_request_all_pages("/api/v1/syncs"))

    assert client.request_count == 3
    assert result["total_records"] == 230
    assert "truncated" not in result


def test_non_advancing_next_page_does_not_loop_forever(monkeypatch):
    # Pathological API: every response says "next_page: 2" — the original bug.
    client = _install_fake(
        monkeypatch,
        lambda page: {"data": [1, 2, 3], "pagination": {"next_page": 2}},
    )
    result = asyncio.run(census_request_all_pages("/api/v1/syncs"))

    # Must terminate via the seen-pages guard (fetches page 1, then page 2,
    # then sees page 2 again and stops) — never spins.
    assert client.request_count == 2
    assert result["status"] == "success"


def test_runaway_pagination_is_capped_and_flagged(monkeypatch):
    # API that NEVER returns next_page=None — would page forever without the cap.
    client = _install_fake(
        monkeypatch,
        lambda page: {"data": list(range(CENSUS_PER_PAGE)), "pagination": {"next_page": page + 1}},
    )
    result = asyncio.run(census_request_all_pages("/api/v1/sync_runs"))

    assert client.request_count == CENSUS_MAX_PAGES
    assert result["truncated"] is True
    assert result["total_records"] == CENSUS_MAX_PAGES * CENSUS_PER_PAGE
    assert "note" in result


def test_non_list_payload_returned_unchanged(monkeypatch):
    client = _install_fake(
        monkeypatch,
        lambda page: {"error": "something", "data": {"not": "a list"}},
    )
    result = asyncio.run(census_request_all_pages("/api/v1/syncs"))

    assert client.request_count == 1
    assert result == {"error": "something", "data": {"not": "a list"}}
