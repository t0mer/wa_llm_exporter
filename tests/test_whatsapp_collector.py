import logging

import httpx
import pytest
from prometheus_client import REGISTRY

import app as exporter


def sample(name, labels=None):
    return REGISTRY.get_sample_value(name, labels or {})


def errors(kind):
    return sample("whatsapp_exporter_scrape_errors_total", {"error_type": kind}) or 0


@pytest.fixture(autouse=True)
def openwa_env(monkeypatch):
    monkeypatch.setattr(exporter, "WHATSAPP_BACKEND", "openwa")
    monkeypatch.setattr(exporter, "WHATSAPP_HOST", "http://openwa.test")
    monkeypatch.setattr(exporter, "OPENWA_API_KEY", "secret-key-123")
    monkeypatch.setattr(exporter, "OPENWA_SESSION_ID", "sess1")


def use_transport(monkeypatch, handler):
    seen = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(exporter, "HTTP_TRANSPORT", httpx.MockTransport(wrapped))
    return seen


def groups(n, start=0):
    return [{"id": f"{start + i}@g.us", "name": f"g{start + i}"} for i in range(n)]


def session_handler(session_body, group_pages=None):
    def handler(request):
        if request.url.path == "/api/sessions/sess1":
            return httpx.Response(200, json=session_body)
        if request.url.path == "/api/sessions/sess1/groups":
            offset = int(request.url.params["offset"])
            limit = int(request.url.params["limit"])
            assert limit == 100
            body = (group_pages or (lambda o: []))(offset)
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    return handler


async def test_ready_wrapped_payload_sets_connected(monkeypatch):
    seen = use_transport(
        monkeypatch,
        session_handler({"data": {"status": "ready", "phone": "972501234567", "pushName": "Bot"}}),
    )
    await exporter.collect_whatsapp_metrics()
    assert sample("whatsapp_connection_status") == 1
    assert sample("whatsapp_devices_total") == 1
    assert sample("whatsapp_session_status", {"status": "ready"}) == 1
    assert seen[0].headers["X-API-Key"] == "secret-key-123"
    assert "authorization" not in seen[0].headers
    assert sample(
        "whatsapp_api_latency_seconds_count", {"endpoint": "/api/sessions/{id}"}
    ) >= 1


async def test_non_ready_bare_payload_is_disconnected(monkeypatch):
    use_transport(monkeypatch, session_handler({"status": "qr"}))
    await exporter.collect_whatsapp_metrics()
    assert sample("whatsapp_connection_status") == 0
    assert sample("whatsapp_session_status", {"status": "qr"}) == 1
    assert sample("whatsapp_session_status", {"status": "ready"}) is None


@pytest.mark.parametrize(
    "wrap",
    [
        lambda page: page,
        lambda page: {"data": page},
        lambda page: {"groups": page},
        lambda page: {"items": page},
        lambda page: {"results": page},
    ],
)
async def test_group_paging_tolerates_shapes(monkeypatch, wrap):
    pages = {0: groups(100), 100: groups(3, 100)}
    use_transport(
        monkeypatch,
        session_handler({"status": "ready"}, lambda o: wrap(pages.get(o, []))),
    )
    await exporter.collect_whatsapp_metrics()
    assert sample("whatsapp_api_groups") == 103
    assert sample(
        "whatsapp_api_latency_seconds_count", {"endpoint": "/api/sessions/{id}/groups"}
    ) >= 2


async def test_http_error_counts_and_does_not_raise(monkeypatch):
    use_transport(monkeypatch, lambda r: httpx.Response(500))
    before = errors("whatsapp_api_error")
    await exporter.collect_whatsapp_metrics()
    assert sample("whatsapp_connection_status") == 0
    assert errors("whatsapp_api_error") == before + 1


async def test_connection_failure_counts_and_does_not_raise(monkeypatch, caplog):
    def boom(request):
        raise httpx.ConnectError("refused")

    use_transport(monkeypatch, boom)
    before = errors("whatsapp_connection_error")
    with caplog.at_level(logging.DEBUG):
        await exporter.collect_whatsapp_metrics()
    assert sample("whatsapp_connection_status") == 0
    assert errors("whatsapp_connection_error") == before + 1
    assert "secret-key-123" not in caplog.text


async def test_groups_failure_is_counted_not_raised(monkeypatch):
    def handler(request):
        if request.url.path.endswith("/groups"):
            return httpx.Response(502)
        return httpx.Response(200, json={"status": "ready"})

    use_transport(monkeypatch, handler)
    before = errors("whatsapp_groups_error")
    await exporter.collect_whatsapp_metrics()
    assert sample("whatsapp_connection_status") == 1
    assert errors("whatsapp_groups_error") == before + 1


async def test_missing_session_id_is_config_error(monkeypatch):
    monkeypatch.setattr(exporter, "OPENWA_SESSION_ID", "")
    use_transport(monkeypatch, lambda r: httpx.Response(200, json={}))
    before = errors("whatsapp_config_error")
    await exporter.collect_whatsapp_metrics()
    assert sample("whatsapp_connection_status") == 0
    assert errors("whatsapp_config_error") == before + 1


@pytest.mark.parametrize(
    "backend,key,expected",
    [
        ("auto", "k", "openwa"),
        ("auto", "", "gowa"),
        ("openwa", "", "openwa"),
        ("gowa", "k", "gowa"),
        ("AUTO", "k", "openwa"),
        ("bogus", "k", "openwa"),
        ("bogus", "", "gowa"),
    ],
)
def test_backend_resolution(monkeypatch, backend, key, expected):
    monkeypatch.setattr(exporter, "WHATSAPP_BACKEND", backend)
    monkeypatch.setattr(exporter, "OPENWA_API_KEY", key)
    assert exporter.resolve_backend() == expected


async def test_gowa_backend_still_works(monkeypatch):
    monkeypatch.setattr(exporter, "WHATSAPP_BACKEND", "gowa")
    monkeypatch.setattr(exporter, "WHATSAPP_BASIC_AUTH_USER", "u")
    monkeypatch.setattr(exporter, "WHATSAPP_BASIC_AUTH_PASSWORD", "p")

    def handler(request):
        if request.url.path == "/app/devices":
            assert request.headers["authorization"].startswith("Basic ")
            return httpx.Response(
                200, json={"results": [{"device": "123@s.whatsapp.net", "name": "d"}]}
            )
        if request.url.path == "/user/my/groups":
            assert request.headers["X-Device-Id"] == "123@s.whatsapp.net"
            return httpx.Response(200, json={"results": {"data": [{}, {}]}})
        return httpx.Response(404)

    seen = use_transport(monkeypatch, handler)
    await exporter.collect_whatsapp_metrics()
    assert sample("whatsapp_connection_status") == 1
    assert sample("whatsapp_devices_total") == 1
    assert [r.url.path for r in seen] == ["/app/devices", "/user/my/groups"]
    assert not any("x-api-key" in r.headers for r in seen)
