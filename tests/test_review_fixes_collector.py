import logging
import math

import httpx
import pytest
from prometheus_client import REGISTRY

import app as exporter

KEY = "secret-key-123"


def sample(name, labels=None):
    return REGISTRY.get_sample_value(name, labels or {})


@pytest.fixture(autouse=True)
def openwa_env(monkeypatch):
    monkeypatch.setattr(exporter, "WHATSAPP_BACKEND", "openwa")
    monkeypatch.setattr(exporter, "WHATSAPP_HOST", "http://openwa.test")
    monkeypatch.setattr(exporter, "OPENWA_API_KEY", KEY)
    monkeypatch.setattr(exporter, "OPENWA_SESSION_ID", "sess1")


def serve(monkeypatch, handler):
    monkeypatch.setattr(exporter, "HTTP_TRANSPORT", httpx.MockTransport(handler))


def ids(a, b):
    return [{"id": f"{i}@g.us", "name": f"g{i}"} for i in range(a, b)]


def paged_server(all_groups, clamp=None, ignore_offset=False):
    def handler(request):
        if request.url.path == "/api/sessions/sess1":
            return httpx.Response(200, json={"status": "ready", "phone": "1"})
        limit = int(request.url.params["limit"])
        offset = 0 if ignore_offset else int(request.url.params["offset"])
        if clamp:
            limit = min(limit, clamp)
        return httpx.Response(200, json={"data": all_groups[offset : offset + limit]})

    return handler


# --- 2: stale gauges ---------------------------------------------------------

@pytest.mark.parametrize(
    "failure",
    [
        lambda r: httpx.Response(500),
        lambda r: httpx.Response(401),
        lambda r: httpx.Response(200, content=b"not json"),
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused")),
    ],
    ids=["500", "401", "badbody", "connect"],
)
async def test_failure_after_ready_clears_stale_state(monkeypatch, failure):
    serve(monkeypatch, paged_server(ids(0, 7)))
    await exporter.collect_whatsapp_metrics()
    assert sample("whatsapp_session_status", {"status": "ready"}) == 1
    assert sample("whatsapp_api_groups") == 7

    serve(monkeypatch, failure)
    await exporter.collect_whatsapp_metrics()
    assert sample("whatsapp_connection_status") == 0
    assert sample("whatsapp_devices_total") == 0
    assert sample("whatsapp_session_status", {"status": "ready"}) is None
    assert math.isnan(sample("whatsapp_api_groups"))
    assert sample("whatsapp_device_info", {"name": "", "device": ""}) == 1


async def test_missing_session_id_clears_stale_state(monkeypatch):
    serve(monkeypatch, paged_server(ids(0, 3)))
    await exporter.collect_whatsapp_metrics()
    monkeypatch.setattr(exporter, "OPENWA_SESSION_ID", "")
    await exporter.collect_whatsapp_metrics()
    assert sample("whatsapp_session_status", {"status": "ready"}) is None
    assert math.isnan(sample("whatsapp_api_groups"))


async def test_not_ready_after_ready_resets_groups(monkeypatch):
    serve(monkeypatch, paged_server(ids(0, 3)))
    await exporter.collect_whatsapp_metrics()
    serve(monkeypatch, lambda r: httpx.Response(200, json={"status": "qr"}))
    await exporter.collect_whatsapp_metrics()
    assert sample("whatsapp_session_status", {"status": "qr"}) == 1
    assert sample("whatsapp_session_status", {"status": "ready"}) is None
    assert math.isnan(sample("whatsapp_api_groups"))


# --- 3: paging ---------------------------------------------------------------

async def test_server_ignoring_offset_terminates(monkeypatch):
    seen = []
    inner = paged_server(ids(0, 100), ignore_offset=True)
    serve(monkeypatch, lambda r: (seen.append(r), inner(r))[1])
    await exporter.collect_whatsapp_metrics()
    assert sample("whatsapp_api_groups") == 100
    assert len(seen) <= 3  # session + page 1 + page 2 (no new ids)


async def test_server_clamping_limit_still_counts_all(monkeypatch):
    serve(monkeypatch, paged_server(ids(0, 120), clamp=50))
    await exporter.collect_whatsapp_metrics()
    assert sample("whatsapp_api_groups") == 120


async def test_page_cap(monkeypatch):
    calls = []

    def handler(request):
        if request.url.path == "/api/sessions/sess1":
            return httpx.Response(200, json={"status": "ready"})
        calls.append(1)
        off = int(request.url.params["offset"])
        return httpx.Response(200, json=ids(off, off + 100))  # endless distinct

    serve(monkeypatch, handler)
    await exporter.collect_whatsapp_metrics()
    assert len(calls) == exporter.OPENWA_GROUPS_MAX_PAGES == 50


# --- 5: phone parsing --------------------------------------------------------

async def test_phone_device_suffix_stripped(monkeypatch):
    serve(
        monkeypatch,
        lambda r: httpx.Response(200, json={"status": "ready", "phone": "972501234567:12@c.us"}),
    )
    await exporter.collect_whatsapp_metrics()
    assert sample(
        "whatsapp_device_info", {"name": "", "device": "972501234567@s.whatsapp.net"}
    ) == 1


# --- 7: key never logged -----------------------------------------------------

async def test_key_in_exception_text_is_not_logged_connect(monkeypatch, caplog):
    def boom(request):
        raise httpx.ConnectError(f"failed with X-API-Key: {KEY}")

    serve(monkeypatch, boom)
    with caplog.at_level(logging.DEBUG):
        await exporter.collect_whatsapp_metrics()
    assert KEY not in caplog.text


async def test_key_in_exception_text_is_not_logged_groups(monkeypatch, caplog):
    def handler(request):
        if request.url.path == "/api/sessions/sess1":
            return httpx.Response(200, json={"status": "ready"})
        raise httpx.ReadError(f"boom X-API-Key: {KEY}")

    serve(monkeypatch, handler)
    with caplog.at_level(logging.DEBUG):
        await exporter.collect_whatsapp_metrics()
    assert KEY not in caplog.text
    assert "***" in caplog.text


# --- 6: default host / backend ----------------------------------------------

@pytest.mark.parametrize(
    "backend,key,host,expected",
    [
        ("gowa", "leftover", "", "http://localhost:3000"),
        (" GOWA ", "leftover", "", "http://localhost:3000"),
        ("auto", "k", "", "http://localhost:2785"),
        ("auto", "", "", "http://localhost:3000"),
        ("OpenWA ", "", "", "http://localhost:2785"),
        ("gowa", "", "http://x:1", "http://x:1"),
    ],
)
def test_default_host_uses_resolved_backend(backend, key, host, expected):
    assert exporter.default_whatsapp_host(backend, key, host) == expected


def test_backend_resolution_strips_whitespace():
    assert exporter.choose_backend(" OpenWA ", "") == "openwa"
    assert exporter.choose_backend(" gowa ", "k") == "gowa"


def test_startup_logs_backend_not_key(monkeypatch, caplog):
    with caplog.at_level(logging.INFO):
        exporter.log_backend_choice()
    assert "openwa" in caplog.text
    assert KEY not in caplog.text
