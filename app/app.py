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
WHATSAPP_HOST = os.getenv("WHATSAPP_HOST", "http://localhost:3000")
WHATSAPP_BASIC_AUTH_USER = os.getenv("WHATSAPP_BASIC_AUTH_USER", "admin")
WHATSAPP_BASIC_AUTH_PASSWORD = os.getenv("WHATSAPP_BASIC_AUTH_PASSWORD", "admin")
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
    ["sender_jid", "sender_name"]
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

async def collect_whatsapp_metrics():
    """Collect WhatsApp API metrics."""
    auth = None
    if WHATSAPP_BASIC_AUTH_USER and WHATSAPP_BASIC_AUTH_PASSWORD:
        auth = httpx.BasicAuth(WHATSAPP_BASIC_AUTH_USER, WHATSAPP_BASIC_AUTH_PASSWORD)

    async with httpx.AsyncClient(base_url=WHATSAPP_HOST, auth=auth, timeout=30.0) as client:
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
                        logger.info(f"Retrieved {len(groups_from_api)} groups from WhatsApp API")

            except Exception as e:
                logger.warning(f"Failed to get groups from WhatsApp API: {e}")


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

            # Messages total
            start = time.time()
            result = await session.execute(text("SELECT COUNT(*) FROM message"))
            total_messages = result.scalar() or 0
            db_query_latency_seconds.labels(query_type="messages_total").observe(time.time() - start)
            messages_total.set(total_messages)

            # Messages today
            start = time.time()
            result = await session.execute(
                text("SELECT COUNT(*) FROM message WHERE timestamp >= :ts"),
                {"ts": today_start}
            )
            messages_today.set(result.scalar() or 0)
            db_query_latency_seconds.labels(query_type="messages_today").observe(time.time() - start)

            # Messages last 24h
            start = time.time()
            result = await session.execute(
                text("SELECT COUNT(*) FROM message WHERE timestamp >= :ts"),
                {"ts": last_24h}
            )
            messages_last_24h.set(result.scalar() or 0)
            db_query_latency_seconds.labels(query_type="messages_last_24h").observe(time.time() - start)

            # Messages last hour
            start = time.time()
            result = await session.execute(
                text("SELECT COUNT(*) FROM message WHERE timestamp >= :ts"),
                {"ts": last_hour}
            )
            messages_last_hour.set(result.scalar() or 0)
            db_query_latency_seconds.labels(query_type="messages_last_hour").observe(time.time() - start)

            # Direct vs Group messages
            start = time.time()
            result = await session.execute(
                text("SELECT COUNT(*) FROM message WHERE group_jid IS NULL")
            )
            messages_direct_total.set(result.scalar() or 0)
            db_query_latency_seconds.labels(query_type="messages_direct").observe(time.time() - start)

            start = time.time()
            result = await session.execute(
                text("SELECT COUNT(*) FROM message WHERE group_jid IS NOT NULL")
            )
            messages_group_total.set(result.scalar() or 0)
            db_query_latency_seconds.labels(query_type="messages_group").observe(time.time() - start)

            # Messages with media
            start = time.time()
            result = await session.execute(
                text("SELECT COUNT(*) FROM message WHERE media_url IS NOT NULL")
            )
            messages_with_media.set(result.scalar() or 0)
            db_query_latency_seconds.labels(query_type="messages_with_media").observe(time.time() - start)

            # Messages per group (top 50)
            start = time.time()
            result = await session.execute(text("""
                SELECT g.group_jid, g.group_name, COUNT(m.message_id) as msg_count
                FROM "group" g
                LEFT JOIN message m ON m.group_jid = g.group_jid
                GROUP BY g.group_jid, g.group_name
                ORDER BY msg_count DESC
                LIMIT 50
            """))
            for row in result:
                group_jid = row[0] or "unknown"
                group_name = row[1] or "unnamed"
                msg_count = row[2] or 0
                # Clean up group name for metric label
                safe_name = group_name.replace('"', '').replace("'", "")[:50]
                messages_per_group.labels(group_jid=group_jid, group_name=safe_name).set(msg_count)
            db_query_latency_seconds.labels(query_type="messages_per_group").observe(time.time() - start)

            # Groups metrics
            start = time.time()
            result = await session.execute(text('SELECT COUNT(*) FROM "group"'))
            groups_total.set(result.scalar() or 0)
            db_query_latency_seconds.labels(query_type="groups_total").observe(time.time() - start)

            start = time.time()
            result = await session.execute(text('SELECT COUNT(*) FROM "group" WHERE managed = true'))
            groups_managed.set(result.scalar() or 0)
            db_query_latency_seconds.labels(query_type="groups_managed").observe(time.time() - start)

            start = time.time()
            result = await session.execute(text('SELECT COUNT(*) FROM "group" WHERE notify_on_spam = true'))
            groups_with_spam_notification.set(result.scalar() or 0)
            db_query_latency_seconds.labels(query_type="groups_spam_notify").observe(time.time() - start)

            start = time.time()
            result = await session.execute(text('SELECT COUNT(*) FROM "group" WHERE community_keys IS NOT NULL'))
            groups_with_community.set(result.scalar() or 0)
            db_query_latency_seconds.labels(query_type="groups_community").observe(time.time() - start)

            # Senders metrics
            start = time.time()
            result = await session.execute(text("SELECT COUNT(*) FROM sender"))
            senders_total.set(result.scalar() or 0)
            db_query_latency_seconds.labels(query_type="senders_total").observe(time.time() - start)

            # Active senders in last 24h
            start = time.time()
            result = await session.execute(text("""
                SELECT COUNT(DISTINCT sender_jid) FROM message WHERE timestamp >= :ts
            """), {"ts": last_24h})
            senders_active_24h.set(result.scalar() or 0)
            db_query_latency_seconds.labels(query_type="senders_active_24h").observe(time.time() - start)

            # Top 10 senders by message count
            start = time.time()
            # Clear previous values
            messages_per_sender._metrics.clear()
            result = await session.execute(text("""
                SELECT m.sender_jid, COALESCE(s.push_name, m.sender_jid) as sender_name, COUNT(*) as msg_count
                FROM message m
                LEFT JOIN sender s ON s.jid = m.sender_jid
                GROUP BY m.sender_jid, s.push_name
                ORDER BY msg_count DESC
                LIMIT 10
            """))
            for row in result:
                sender_jid = row[0] or "unknown"
                sender_name = str(row[1] or "unknown").replace('"', '').replace("'", "")[:50]
                msg_count = row[2] or 0
                messages_per_sender.labels(sender_jid=sender_jid, sender_name=sender_name).set(msg_count)
            db_query_latency_seconds.labels(query_type="messages_per_sender").observe(time.time() - start)

            # Reactions
            start = time.time()
            try:
                result = await session.execute(text("SELECT COUNT(*) FROM reaction"))
                reactions_total.set(result.scalar() or 0)
            except Exception:
                reactions_total.set(0)
            db_query_latency_seconds.labels(query_type="reactions_total").observe(time.time() - start)

            # Opt-outs
            start = time.time()
            try:
                result = await session.execute(text("SELECT COUNT(*) FROM optout"))
                optouts_total.set(result.scalar() or 0)
            except Exception:
                optouts_total.set(0)
            db_query_latency_seconds.labels(query_type="optouts_total").observe(time.time() - start)

            # KB Topics
            start = time.time()
            try:
                result = await session.execute(text("SELECT COUNT(*) FROM kbtopic"))
                kb_topics_total.set(result.scalar() or 0)
            except Exception:
                kb_topics_total.set(0)
            db_query_latency_seconds.labels(query_type="kb_topics_total").observe(time.time() - start)

            # Table row counts (for capacity planning)
            tables = ["message", "sender", '"group"', "reaction", "optout"]
            for table in tables:
                try:
                    result = await session.execute(text(f"SELECT COUNT(*) FROM {table}"))
                    count = result.scalar() or 0
                    db_table_rows.labels(table_name=table.replace('"', '')).set(count)
                except Exception:
                    pass

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
