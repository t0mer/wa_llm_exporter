#!/usr/bin/env python3
"""
Prometheus Exporter for WhatsApp LLM Platform

Exports comprehensive metrics about:
- WhatsApp connectivity and device status
- Messages (total, by type, by group)
- Groups (total, managed, community)
- Senders/Contacts
- Database performance
- API health
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from prometheus_client import Counter, Gauge, Histogram, Info, generate_latest, CONTENT_TYPE_LATEST
from sqlalchemy import func, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

# Configure logging
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Configuration from environment
DB_URI = os.getenv("DB_URI", "postgresql+asyncpg://user:password@localhost:5432/postgres")


def choose_backend(backend: str, api_key: str) -> str:
    """Return "openwa" or "gowa" (explicit setting wins; auto = openwa iff an API key is set)."""
    value = (backend or "auto").strip().lower()
    if value in ("openwa", "gowa"):
        return value
    return "openwa" if api_key else "gowa"


def default_whatsapp_host(backend: str, api_key: str, host: str) -> str:
    """WHATSAPP_HOST if set, else the default port of the resolved backend."""
    if host:
        return host
    return "http://localhost:2785" if choose_backend(backend, api_key) == "openwa" else "http://localhost:3000"


WHATSAPP_BACKEND = os.getenv("WHATSAPP_BACKEND", "auto")  # auto | openwa | gowa
OPENWA_API_KEY = os.getenv("OPENWA_API_KEY", "")
OPENWA_SESSION_ID = os.getenv("OPENWA_SESSION_ID", "")
WHATSAPP_HOST = default_whatsapp_host(WHATSAPP_BACKEND, OPENWA_API_KEY, os.getenv("WHATSAPP_HOST", ""))
WHATSAPP_BASIC_AUTH_USER = os.getenv("WHATSAPP_BASIC_AUTH_USER", "admin")
WHATSAPP_BASIC_AUTH_PASSWORD = os.getenv("WHATSAPP_BASIC_AUTH_PASSWORD", "admin")


def parse_jid_list(raw: str) -> list[str]:
    """Parse a comma separated JID list, dropping blanks."""
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


# JIDs (phone and/or lid form) of the bot itself; excluded from sender metrics.
BOT_JIDS = parse_jid_list(os.getenv("BOT_JIDS", ""))
PORT = 9100

# ============================================================================
# Prometheus Metrics Definitions
# ============================================================================

# WhatsApp Device/Connection Metrics
whatsapp_devices_total = Gauge(
    "whatsapp_devices_total",
    "Total number of WhatsApp devices connected"
)
whatsapp_device_info = Info(
    "whatsapp_device",
    "WhatsApp device information"
)
whatsapp_connection_status = Gauge(
    "whatsapp_connection_status",
    "WhatsApp connection status (1=connected, 0=disconnected)"
)
whatsapp_session_status = Gauge(
    "whatsapp_session_status",
    "OpenWA session status (1 for the current status label)",
    ["status"]
)
whatsapp_api_groups = Gauge(
    "whatsapp_api_groups",
    "Number of groups reported live by the WhatsApp API"
)
whatsapp_api_latency_seconds = Histogram(
    "whatsapp_api_latency_seconds",
    "WhatsApp API response latency in seconds",
    ["endpoint"],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]
)

# Message Metrics
messages_total = Gauge(
    "whatsapp_messages_total",
    "Total number of messages in database"
)
messages_by_type = Gauge(
    "whatsapp_messages_by_type",
    "Number of messages by type",
    ["message_type"]
)
messages_today = Gauge(
    "whatsapp_messages_today",
    "Number of messages received today"
)
messages_last_24h = Gauge(
    "whatsapp_messages_last_24h",
    "Number of messages in the last 24 hours"
)
messages_last_hour = Gauge(
    "whatsapp_messages_last_hour",
    "Number of messages in the last hour"
)
messages_per_group = Gauge(
    "whatsapp_messages_per_group",
    "Number of messages per group",
    ["group_jid", "group_name"]
)
messages_direct_total = Gauge(
    "whatsapp_messages_direct_total",
    "Total number of direct/private messages"
)
messages_group_total = Gauge(
    "whatsapp_messages_group_total",
    "Total number of group messages"
)
messages_with_media = Gauge(
    "whatsapp_messages_with_media_total",
    "Total messages containing media"
)

# Group Metrics
groups_total = Gauge(
    "whatsapp_groups_total",
    "Total number of WhatsApp groups"
)
groups_managed = Gauge(
    "whatsapp_groups_managed",
    "Number of managed groups"
)
groups_with_spam_notification = Gauge(
    "whatsapp_groups_with_spam_notification",
    "Number of groups with spam notification enabled"
)
groups_with_community = Gauge(
    "whatsapp_groups_with_community",
    "Number of groups with community keys"
)

# Sender/Contact Metrics
senders_total = Gauge(
    "whatsapp_senders_total",
    "Total number of unique senders/contacts"
)
senders_active_24h = Gauge(
    "whatsapp_senders_active_24h",
    "Number of active senders in last 24 hours"
)
messages_per_sender = Gauge(
    "whatsapp_messages_per_sender",
    "Number of messages per sender (top 10)",
    ["sender_jid", "sender_name", "jid_kind"]
)
senders_by_kind = Gauge(
    "whatsapp_senders_by_kind",
    "Number of known senders by JID kind (phone or lid), excluding the bot",
    ["kind"]
)

# Reaction Metrics
reactions_total = Gauge(
    "whatsapp_reactions_total",
    "Total number of message reactions"
)

# Opt-out Metrics
optouts_total = Gauge(
    "whatsapp_optouts_total",
    "Total number of opt-outs"
)

# Knowledge Base Metrics
kb_topics_total = Gauge(
    "whatsapp_kb_topics_total",
    "Total number of knowledge base topics"
)

# Database Performance Metrics
db_connection_status = Gauge(
    "whatsapp_db_connection_status",
    "Database connection status (1=connected, 0=disconnected)"
)
db_query_latency_seconds = Histogram(
    "whatsapp_db_query_latency_seconds",
    "Database query latency in seconds",
    ["query_type"],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0]
)
db_table_rows = Gauge(
    "whatsapp_db_table_rows",
    "Approximate row count per table",
    ["table_name"]
)

# Scrape Metrics
last_scrape_timestamp = Gauge(
    "whatsapp_exporter_last_scrape_timestamp",
    "Timestamp of last successful metrics scrape"
)
scrape_duration_seconds = Histogram(
    "whatsapp_exporter_scrape_duration_seconds",
    "Duration of metrics collection in seconds",
    buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0]
)
scrape_errors_total = Counter(
    "whatsapp_exporter_scrape_errors_total",
    "Total number of scrape errors",
    ["error_type"]
)

# ============================================================================
# Database Connection
# ============================================================================

engine = None
async_session_factory = None


async def init_db():
    """Initialize database connection."""
    global engine, async_session_factory
    engine = create_async_engine(DB_URI, pool_pre_ping=True, pool_size=5, max_overflow=10)
    async_session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    logger.info("Database engine initialized")


async def get_db_session() -> AsyncSession:
    """Get a database session."""
    async with async_session_factory() as session:
        yield session


# ============================================================================
# Metrics Collection Functions
# ============================================================================

# Known "[[Attached X]]" types; anything else (user-controlled text) becomes "other".
KNOWN_MESSAGE_TYPES = frozenset({
    "text", "image", "video", "audio", "voice", "document", "sticker", "gif",
    "contact", "location", "poll", "list", "order",
})

# Test hook: an httpx transport (e.g. httpx.MockTransport) used for API calls.
HTTP_TRANSPORT: httpx.AsyncBaseTransport | None = None

OPENWA_GROUPS_PAGE_SIZE = 100
OPENWA_GROUPS_MAX_PAGES = 50


def resolve_backend() -> str:
    """Return "openwa" or "gowa" from WHATSAPP_BACKEND / OPENWA_API_KEY."""
    return choose_backend(WHATSAPP_BACKEND, OPENWA_API_KEY)


def log_backend_choice():
    """Log which WhatsApp backend is used (never the API key)."""
    logger.info(f"WhatsApp backend: {resolve_backend()} (host {WHATSAPP_HOST})")


def _redact(text_: str) -> str:
    if OPENWA_API_KEY:
        return text_.replace(OPENWA_API_KEY, "***")
    return text_


def _reset_openwa_state(clear_status: bool = True):
    """Drop stale OpenWA-derived series after a non-success scrape."""
    if clear_status:
        whatsapp_session_status.clear()
        whatsapp_device_info.info({"name": "", "device": ""})
    whatsapp_api_groups.set(float("nan"))


def _unwrap(payload: Any) -> Any:
    """OpenWA may wrap responses as {"data": {...}}; return the inner object."""
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload


def _extract_list(payload: Any) -> list:
    """Find a list in a bare list or under data|groups|items|results."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "groups", "items", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = _extract_list(value)
                if nested:
                    return nested
    return []


async def collect_whatsapp_metrics():
    """Collect WhatsApp API metrics. Never raises; failures are counted."""
    try:
        if resolve_backend() == "openwa":
            await _collect_openwa()
        else:
            await _collect_gowa()
    except Exception as e:  # defensive: a scrape must never fail on the API side
        whatsapp_connection_status.set(0)
        scrape_errors_total.labels(error_type="whatsapp_connection_error").inc()
        logger.error(f"WhatsApp metrics collection failed: {type(e).__name__}")


def _fail_openwa(error_type: str):
    whatsapp_connection_status.set(0)
    whatsapp_devices_total.set(0)
    _reset_openwa_state()
    scrape_errors_total.labels(error_type=error_type).inc()


async def _list_openwa_groups(client: httpx.AsyncClient, session_path: str) -> int:
    """Count groups by paging, deduplicating ids; tolerant of servers that clamp the limit."""
    seen: set[str] = set()
    offset = 0
    for _ in range(OPENWA_GROUPS_MAX_PAGES):
        start = time.time()
        resp = await client.get(
            f"{session_path}/groups",
            params={"limit": OPENWA_GROUPS_PAGE_SIZE, "offset": offset},
        )
        whatsapp_api_latency_seconds.labels(
            endpoint="/api/sessions/{id}/groups"
        ).observe(time.time() - start)
        if resp.status_code != 200:
            raise RuntimeError(f"groups status {resp.status_code}")
        items = _extract_list(resp.json())
        added = 0
        for item in items:
            key = None
            if isinstance(item, dict):
                for field in ("id", "groupId", "jid", "chatId"):
                    if item.get(field):
                        key = str(item[field])
                        break
            key = key or repr(item)
            if key not in seen:
                seen.add(key)
                added += 1
        if not items or added == 0:
            break
        offset += len(items)
    else:
        logger.warning(f"OpenWA group listing hit the {OPENWA_GROUPS_MAX_PAGES} page cap")
    return len(seen)


async def _collect_openwa():
    if not OPENWA_SESSION_ID:
        _fail_openwa("whatsapp_config_error")
        logger.warning("OPENWA_SESSION_ID is not set; cannot query OpenWA")
        return

    session_path = f"/api/sessions/{OPENWA_SESSION_ID}"
    headers = {"X-API-Key": OPENWA_API_KEY}
    async with httpx.AsyncClient(
        base_url=WHATSAPP_HOST, headers=headers, timeout=30.0, transport=HTTP_TRANSPORT
    ) as client:
        try:
            start = time.time()
            response = await client.get(session_path)
            whatsapp_api_latency_seconds.labels(endpoint="/api/sessions/{id}").observe(time.time() - start)
        except Exception as e:
            _fail_openwa("whatsapp_connection_error")
            logger.error(f"Failed to connect to OpenWA: {type(e).__name__}")
            return

        if response.status_code != 200:
            _fail_openwa("whatsapp_api_error")
            logger.warning(f"OpenWA session endpoint returned status {response.status_code}")
            return

        try:
            body = _unwrap(response.json())
            if not isinstance(body, dict):
                raise ValueError("unexpected session payload")
        except ValueError:
            _fail_openwa("whatsapp_api_error")
            logger.warning("OpenWA session endpoint returned an unparseable body")
            return

        status = str(body.get("status") or "unknown")
        ready = status == "ready"
        whatsapp_devices_total.set(1)
        whatsapp_connection_status.set(1 if ready else 0)
        whatsapp_session_status.clear()
        whatsapp_session_status.labels(status=status).set(1)

        phone_raw = str(body.get("phone") or "").split("@")[0].split(":")[0]
        phone = "".join(ch for ch in phone_raw if ch.isdigit())
        whatsapp_device_info.info({
            "name": str(body.get("pushName") or ""),
            "device": f"{phone}@s.whatsapp.net" if phone else "",
        })

        if not ready:
            _reset_openwa_state(clear_status=False)
            return

        try:
            whatsapp_api_groups.set(await _list_openwa_groups(client, session_path))
        except Exception as e:
            _reset_openwa_state(clear_status=False)
            scrape_errors_total.labels(error_type="whatsapp_groups_error").inc()
            logger.warning(f"Failed to list groups from OpenWA: {type(e).__name__}: {_redact(str(e))}")


async def _collect_gowa():
    auth = None
    if WHATSAPP_BASIC_AUTH_USER and WHATSAPP_BASIC_AUTH_PASSWORD:
        auth = httpx.BasicAuth(WHATSAPP_BASIC_AUTH_USER, WHATSAPP_BASIC_AUTH_PASSWORD)

    async with httpx.AsyncClient(
        base_url=WHATSAPP_HOST, auth=auth, timeout=30.0, transport=HTTP_TRANSPORT
    ) as client:
        device_id = None

        # Get devices
        try:
            start = time.time()
            response = await client.get("/app/devices")
            latency = time.time() - start
            whatsapp_api_latency_seconds.labels(endpoint="/app/devices").observe(latency)

            if response.status_code == 200:
                data = response.json()
                results = data.get("results", [])
                whatsapp_devices_total.set(len(results))
                whatsapp_connection_status.set(1 if len(results) > 0 else 0)

                if results:
                    device = results[0]
                    device_id = device.get("device", "")
                    whatsapp_device_info.info({
                        "name": str(device.get("name", "")),
                        "device": str(device_id)
                    })
            else:
                whatsapp_connection_status.set(0)
                scrape_errors_total.labels(error_type="whatsapp_api_error").inc()
                logger.warning(f"WhatsApp API returned status {response.status_code}")

        except Exception as e:
            whatsapp_connection_status.set(0)
            scrape_errors_total.labels(error_type="whatsapp_connection_error").inc()
            logger.error(f"Failed to connect to WhatsApp API: {e}")

        # Get groups from WhatsApp API
        if device_id:
            try:
                start = time.time()
                headers = {"X-Device-Id": device_id}
                response = await client.get("/user/my/groups", headers=headers)
                latency = time.time() - start
                whatsapp_api_latency_seconds.labels(endpoint="/user/my/groups").observe(latency)

                if response.status_code == 200:
                    data = response.json()
                    if "results" in data and "data" in data["results"]:
                        groups_from_api = data["results"]["data"]
                        whatsapp_api_groups.set(len(groups_from_api))
                        logger.info(f"Retrieved {len(groups_from_api)} groups from WhatsApp API")

            except Exception as e:
                logger.warning(f"Failed to get groups from WhatsApp API: {e}")


async def _query(session: AsyncSession, name: str, sql: str, params: dict | None = None):
    """Run one query inside a savepoint so a failure cannot poison later queries.

    Returns the rows, or None on failure (counted as scrape_errors_total{error_type="query:<name>"}).
    """
    start = time.time()
    try:
        async with session.begin_nested():
            result = await session.execute(text(sql), params or {})
            rows = result.all()
        return rows
    except Exception as e:
        scrape_errors_total.labels(error_type=f"query:{name}").inc()
        logger.warning(f"Query {name} failed: {type(e).__name__}: {e}")
        return None
    finally:
        db_query_latency_seconds.labels(query_type=name).observe(time.time() - start)


async def _scalar_into(session: AsyncSession, name: str, gauge: Gauge, sql: str, params: dict | None = None):
    rows = await _query(session, name, sql, params)
    if rows is not None:
        gauge.set((rows[0][0] if rows else 0) or 0)


async def collect_database_metrics():
    """Collect database metrics."""
    try:
        async with async_session_factory() as session:
            # Test connection
            start = time.time()
            await session.execute(text("SELECT 1"))
            latency = time.time() - start
            db_query_latency_seconds.labels(query_type="connection_test").observe(latency)
            db_connection_status.set(1)

            now = datetime.now(timezone.utc)
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            last_24h = now - timedelta(hours=24)
            last_hour = now - timedelta(hours=1)

            bots = {"bots": list(BOT_JIDS)}
            scalars = [
                ("messages_total", messages_total, "SELECT COUNT(*) FROM message", None),
                ("messages_today", messages_today,
                 "SELECT COUNT(*) FROM message WHERE timestamp >= :ts", {"ts": today_start}),
                ("messages_last_24h", messages_last_24h,
                 "SELECT COUNT(*) FROM message WHERE timestamp >= :ts", {"ts": last_24h}),
                ("messages_last_hour", messages_last_hour,
                 "SELECT COUNT(*) FROM message WHERE timestamp >= :ts", {"ts": last_hour}),
                ("messages_direct", messages_direct_total,
                 "SELECT COUNT(*) FROM message WHERE group_jid IS NULL", None),
                ("messages_group", messages_group_total,
                 "SELECT COUNT(*) FROM message WHERE group_jid IS NOT NULL", None),
                ("messages_with_media", messages_with_media,
                 "SELECT COUNT(*) FROM message "
                 "WHERE media_url IS NOT NULL OR text LIKE '[[Attached %'", None),
                ("groups_total", groups_total, 'SELECT COUNT(*) FROM "group"', None),
                ("groups_managed", groups_managed,
                 'SELECT COUNT(*) FROM "group" WHERE managed = true', None),
                ("groups_spam_notify", groups_with_spam_notification,
                 'SELECT COUNT(*) FROM "group" WHERE notify_on_spam = true', None),
                ("groups_community", groups_with_community,
                 'SELECT COUNT(*) FROM "group" WHERE community_keys IS NOT NULL', None),
                ("senders_total", senders_total,
                 "SELECT COUNT(*) FROM sender WHERE jid <> ALL(CAST(:bots AS text[]))", bots),
                ("senders_active_24h", senders_active_24h,
                 "SELECT COUNT(DISTINCT sender_jid) FROM message WHERE timestamp >= :ts "
                 "AND COALESCE(sender_jid, '') <> ALL(CAST(:bots AS text[]))",
                 {"ts": last_24h, **bots}),
                ("reactions_total", reactions_total, "SELECT COUNT(*) FROM reaction", None),
                ("optouts_total", optouts_total, "SELECT COUNT(*) FROM opt_out", None),
                ("kb_topics_total", kb_topics_total, "SELECT COUNT(*) FROM kbtopic", None),
            ]
            for name, gauge, sql, params in scalars:
                await _scalar_into(session, name, gauge, sql, params)

            # Messages per group (top 50). display_name is missing on original wa_llm schemas,
            # so fall back to a query without it.
            group_sql = """
                SELECT g.group_jid,
                       COALESCE({label}) AS label,
                       COUNT(m.message_id) as msg_count
                FROM "group" g
                LEFT JOIN message m ON m.group_jid = g.group_jid
                GROUP BY g.group_jid{extra}, g.group_name
                ORDER BY msg_count DESC
                LIMIT 50
            """
            rows = await _query(
                session, "messages_per_group",
                group_sql.format(
                    label="NULLIF(g.display_name, ''), NULLIF(g.group_name, ''), g.group_jid",
                    extra=", g.display_name",
                ),
            )
            if rows is None:
                rows = await _query(
                    session, "messages_per_group_no_display_name",
                    group_sql.format(label="NULLIF(g.group_name, ''), g.group_jid", extra=""),
                )
            if rows is not None:
                messages_per_group.clear()
                for row in rows:
                    group_jid = row[0] or "unknown"
                    group_name = row[1] or "unnamed"
                    safe_name = group_name.replace('"', '').replace("'", "")[:50]
                    messages_per_group.labels(group_jid=group_jid, group_name=safe_name).set(row[2] or 0)

            # Top 10 senders by message count (bot excluded, JID kind labelled)
            rows = await _query(session, "messages_per_sender", """
                SELECT m.sender_jid, COALESCE(s.push_name, m.sender_jid) as sender_name,
                       CASE WHEN m.sender_jid LIKE '%@lid' THEN 'lid' ELSE 'phone' END AS jid_kind,
                       COUNT(*) as msg_count
                FROM message m
                LEFT JOIN sender s ON s.jid = m.sender_jid
                WHERE COALESCE(m.sender_jid, '') <> ALL(CAST(:bots AS text[]))
                GROUP BY m.sender_jid, s.push_name
                ORDER BY msg_count DESC
                LIMIT 10
            """, bots)
            if rows is not None:
                messages_per_sender.clear()
                for row in rows:
                    sender_jid = row[0] or "unknown"
                    sender_name = str(row[1] or "unknown").replace('"', '').replace("'", "")[:50]
                    messages_per_sender.labels(
                        sender_jid=sender_jid, sender_name=sender_name, jid_kind=row[2]
                    ).set(row[3] or 0)

            # Known senders split by JID kind (lid vs phone), bot excluded
            rows = await _query(session, "senders_by_kind", """
                SELECT CASE WHEN jid LIKE '%@lid' THEN 'lid' ELSE 'phone' END, COUNT(*)
                FROM sender
                WHERE jid <> ALL(CAST(:bots AS text[]))
                GROUP BY 1
            """, bots)
            if rows is not None:
                senders_by_kind.clear()
                found = {row[0]: row[1] for row in rows}
                for kind in ("phone", "lid"):
                    senders_by_kind.labels(kind=kind).set(found.get(kind, 0))

            # Messages by type, from the "[[Attached X]]" text prefix (others are "text")
            rows = await _query(session, "messages_by_type", r"""
                SELECT COALESCE(LOWER(substring(text from '^\[\[Attached (\w+)\]\]')), 'text') AS t,
                       COUNT(*)
                FROM message
                GROUP BY 1
            """)
            if rows is not None:
                messages_by_type.clear()
                buckets: dict[str, int] = {}
                for row in rows:
                    kind = row[0] if row[0] in KNOWN_MESSAGE_TYPES else "other"
                    buckets[kind] = buckets.get(kind, 0) + (row[1] or 0)
                for kind, count in buckets.items():
                    messages_by_type.labels(message_type=kind).set(count)

            # Table row counts (for capacity planning)
            db_table_rows.clear()
            tables = ["message", "sender", '"group"', "reaction", "opt_out", "kbtopic", "kb_topic_message"]
            for table in tables:
                label = table.replace('"', '')
                rows = await _query(session, f"table_rows_{label}", f"SELECT COUNT(*) FROM {table}")
                if rows is not None:
                    db_table_rows.labels(table_name=label).set(rows[0][0] or 0)

    except Exception as e:
        db_connection_status.set(0)
        scrape_errors_total.labels(error_type="database_error").inc()
        logger.error(f"Database metrics collection failed: {e}")
        raise


async def collect_all_metrics():
    """Collect all metrics."""
    start = time.time()

    try:
        # Collect metrics in parallel
        await asyncio.gather(
            collect_whatsapp_metrics(),
            collect_database_metrics(),
            return_exceptions=True
        )

        last_scrape_timestamp.set(time.time())
        duration = time.time() - start
        scrape_duration_seconds.observe(duration)
        logger.info(f"Metrics collection completed in {duration:.2f}s")

    except Exception as e:
        scrape_errors_total.labels(error_type="general_error").inc()
        logger.error(f"Metrics collection failed: {e}")
        raise


# ============================================================================
# HTTP Endpoints
# ============================================================================

async def metrics_endpoint(request: Request) -> Response:
    """Prometheus metrics endpoint - collects metrics on-demand."""
    try:
        await collect_all_metrics()
    except Exception as e:
        logger.error(f"Metrics collection failed during scrape: {e}")
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


async def health_endpoint(request: Request) -> Response:
    """Health check endpoint."""
    return Response('{"status": "ok"}', media_type="application/json")


async def ready_endpoint(request: Request) -> Response:
    """Readiness check endpoint."""
    try:
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
        return Response('{"status": "ready"}', media_type="application/json")
    except Exception as e:
        return Response(f'{{"status": "not ready", "error": "{e}"}}', status_code=503, media_type="application/json")


# ============================================================================
# Application Setup
# ============================================================================

@asynccontextmanager
async def lifespan(app: Starlette):
    """Application lifespan handler."""
    await init_db()
    logger.info(f"WhatsApp Prometheus Exporter started on port {PORT}")
    log_backend_choice()
    logger.info("Metrics are collected on-demand when /metrics is called")
    yield


routes = [
    Route("/metrics", metrics_endpoint),
    Route("/health", health_endpoint),
    Route("/healthz", health_endpoint),
    Route("/ready", ready_endpoint),
    Route("/readyz", ready_endpoint),
]

app = Starlette(debug=False, routes=routes, lifespan=lifespan)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
