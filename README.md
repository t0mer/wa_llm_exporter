# wa_llm Prometheus Exporter

A Prometheus exporter for [wa_llm](https://github.com/ilanbenb/wa_llm), the AI-powered WhatsApp
group bot, and its OpenWA-based fork [t0mer/openwa_llm](https://github.com/t0mer/openwa_llm). It
reads the bot's PostgreSQL database and the WhatsApp HTTP API the bot runs on, and exposes metrics
about WhatsApp connectivity, messages, groups, senders, reactions, opt-outs, knowledge-base topics
and database query latency. A ready-made Grafana dashboard is included.

Two WhatsApp backends are supported: **OpenWA** (the default for `openwa_llm`) and the legacy
[go-whatsapp-web-multidevice](https://github.com/aldinokemal/go-whatsapp-web-multidevice) ("gowa")
used by the original wa_llm. See [WhatsApp backends](#whatsapp-backends).

[![Docker Pulls](https://img.shields.io/docker/pulls/techblog/wa-llm-exporter)](https://hub.docker.com/r/techblog/wa-llm-exporter)
[![License](https://img.shields.io/github/license/t0mer/wa_llm_exporter)](LICENSE)

## Table of contents

- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [HTTP endpoints](#http-endpoints)
- [Metrics](#metrics)
- [Prometheus configuration](#prometheus-configuration)
- [Grafana dashboard](#grafana-dashboard)
- [Troubleshooting](#troubleshooting)
- [Security and privacy](#security-and-privacy)
- [Development](#development)
- [Contributing](#contributing)
- [License](#license)

## Features

- **WhatsApp connectivity**: connection status, OpenWA session status (`ready`, `qr`, ...), live
  group count from the API, device name and JID, and API latency. Works with OpenWA and gowa.
- **Message statistics**: total, today (UTC), last 24 hours, last hour, direct vs. group, and
  messages with media, and a per-type breakdown.
- **Per-group and per-sender counts**: message count for the top 50 groups and the top 10 senders.
- **Group statistics**: total, managed, spam notification enabled, and groups with community keys.
- **Sender statistics**: total senders, active senders in the last 24 hours, and a phone-vs-`@lid`
  split. The bot's own JIDs (`BOT_JIDS`) are excluded.
- **Reactions, opt-outs and knowledge-base topics** counts.
- **Database health**: connection status, per-query latency histograms and row counts per table.
- **Exporter self-metrics**: last scrape timestamp, scrape duration and error counters.
- **Health and readiness endpoints** for Docker and Kubernetes probes.
- **Grafana dashboard** (`dashboard.json`) with 17 panels.
- Multi-arch Docker image (`linux/amd64`, `linux/arm64`) on Docker Hub.

## How it works

The exporter is a small [Starlette](https://www.starlette.io/) app served by Uvicorn on port
`9100`. There is no background polling and no cache: every request to `/metrics` runs a full
collection, and the WhatsApp API and the database are queried in parallel.

```mermaid
flowchart LR
    P[Prometheus] -- "GET /metrics" --> E[wa_llm exporter :9100]
    E -- "OpenWA: GET /api/sessions/{id}[/groups]<br/>(X-API-Key)<br/>gowa: GET /app/devices (Basic Auth)" --> W[OpenWA / gowa API]
    E -- "SELECT COUNT(*) ...<br/>(SQLAlchemy + asyncpg)" --> D[(wa_llm PostgreSQL)]
    G[Grafana] -- PromQL --> P
```

**WhatsApp API** (`WHATSAPP_HOST`), one of two backends (see [WhatsApp backends](#whatsapp-backends)):

- OpenWA: `GET /api/sessions/{OPENWA_SESSION_ID}` (header `X-API-Key`) gives the session status;
  when the session is `ready`, `GET /api/sessions/{id}/groups?limit=100&offset=N` is paged to count
  the live groups.
- gowa: `GET /app/devices` sets the device count and connection status; `GET /user/my/groups`
  (with `X-Device-Id`) gives the live group count.

**Database** (`DB_URI`): read-only `SELECT COUNT(*)` style queries against these wa_llm tables:

| Table | Columns used |
|-------|--------------|
| `message` | `message_id`, `timestamp`, `group_jid`, `sender_jid`, `media_url`, `text` |
| `"group"` | `group_jid`, `group_name`, `display_name`, `managed`, `notify_on_spam`, `community_keys` |
| `sender` | `jid`, `push_name` |
| `reaction`, `opt_out`, `kbtopic`, `kb_topic_message` | row count only |

The `display_name` column (absent on original wa_llm schemas) is detected once via `information_schema` and re-checked after a failure of the group query. Every query runs in its own savepoint. If one fails (for example a table is missing), only that
metric is skipped (a gauge keeps its previous value; a missing table simply has no
`whatsapp_db_table_rows` sample), the failure is logged and counted as
`whatsapp_exporter_scrape_errors_total{error_type="query:<name>"}`, and all later queries still run.

## WhatsApp backends

`WHATSAPP_BACKEND` selects how connectivity is checked:

| Value | Behaviour |
|-------|-----------|
| `auto` (default) | `openwa` when `OPENWA_API_KEY` is set, otherwise `gowa`. |
| `openwa` | Query the OpenWA REST API (`X-API-Key`). Requires `OPENWA_API_KEY` and `OPENWA_SESSION_ID`. |
| `gowa` | Query go-whatsapp-web-multidevice with HTTP Basic Auth (the previous behaviour). |

With OpenWA, `whatsapp_connection_status` is `1` only when the session status is `ready`;
`whatsapp_session_status{status="..."}` shows the exact status (for example `qr` or `disconnected`).
Group listing is paged (100 per page, at most 200 pages) and de-duplicated by id, so servers that clamp the page size or ignore the offset still terminate. Responses are accepted both bare and wrapped as `{"data": {...}}`, and group lists under `data`,
`groups`, `items`, `results` or as a bare list. `OPENWA_SESSION_ID` and `OPENWA_API_KEY` must match
the values in the bot's own `.env`. The API key is sent only as a header and is never logged.

## Requirements

- A running wa_llm deployment:
  - its PostgreSQL database, reachable from the exporter;
  - its WhatsApp API (OpenWA, or gowa for the original wa_llm), reachable from the exporter
    (optional; without it only the WhatsApp metrics are missing).
- Docker, **or** Python 3.12 (the version used by the Docker image) to run from source.
- Prometheus to scrape the exporter, and optionally Grafana for the dashboard.

## Installation

### Docker Compose

The included `docker-compose.yml` runs the published image `techblog/wa-llm-exporter` (tag
`latest`) as the container `wa_llm_exporter` on port `9100`, with `restart: unless-stopped` and a
health check on `/health`.

1. Edit `DB_URI` and `WHATSAPP_HOST` in `docker-compose.yml`. The defaults point at
   `host.docker.internal` (OpenWA on port `2785`) with placeholder credentials.
2. Set the WhatsApp API credentials in your shell or in a `.env` file next to the compose file.
   For OpenWA (use the same values as the bot's `.env`):

   ```bash
   OPENWA_API_KEY=<openwa-api-key>
   OPENWA_SESSION_ID=<openwa-session-id>
   BOT_JIDS=<bot-phone>@s.whatsapp.net,<bot-lid>@lid   # optional
   ```

   For gowa, set `WHATSAPP_BACKEND=gowa`, `WHATSAPP_HOST` (port `3000`) and
   `WHATSAPP_BASIC_AUTH_USER` / `WHATSAPP_BASIC_AUTH_PASSWORD` instead.

3. Start it:

   ```bash
   docker compose up -d
   curl -s http://localhost:9100/metrics | head
   ```

The compose file maps `host.docker.internal` to the host gateway (`extra_hosts`), so it resolves on
Linux too. Outside compose (plain `docker run`), add `--add-host host.docker.internal:host-gateway`
or use the real host name or IP address of the database and the WhatsApp API.

If the bot runs in Docker Compose, you can instead join the exporter to the bot's compose network
and use the service names: attach the service to the external network
`<bot-project>_default` (for example `openwa_llm_default`) with
`networks: {default: {external: true, name: <bot-project>_default}}`, then set
`DB_URI=postgresql+asyncpg://<db-user>:<db-password>@postgres:5432/postgres` and
`WHATSAPP_HOST=http://whatsapp:2785`. Note that the bot's database name is `postgres`
(its `DB_URI` ends in `/postgres`), not the `POSTGRES_DB` value of the postgres service.

### Docker

```bash
docker run -d \
  --name wa_llm_exporter \
  -p 9100:9100 \
  -e DB_URI="postgresql+asyncpg://<db-user>:<db-password>@<db-host>:5432/<db-name>" \
  -e WHATSAPP_HOST="http://<whatsapp-host>:2785" \
  -e OPENWA_API_KEY="<openwa-api-key>" \
  -e OPENWA_SESSION_ID="<openwa-session-id>" \
  techblog/wa-llm-exporter:latest
```

Published tags:

| Tag | Platforms |
|-----|-----------|
| `latest` | `linux/amd64`, `linux/arm64` |
| `e5d19ec` | `linux/amd64`, `linux/arm64` |

To build the image yourself:

```bash
docker build -t wa-llm-exporter .
```

### From source

```bash
git clone https://github.com/t0mer/wa_llm_exporter.git
cd wa_llm_exporter
pip install -r requirements.txt

export DB_URI="postgresql+asyncpg://<db-user>:<db-password>@localhost:5432/<db-name>"
export WHATSAPP_HOST="http://localhost:2785"
export OPENWA_API_KEY="<openwa-api-key>"
export OPENWA_SESSION_ID="<openwa-session-id>"

python app/app.py
```

## Configuration

All configuration is through environment variables. There are no command-line flags or config
files.

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_URI` | `postgresql+asyncpg://user:password@localhost:5432/postgres` (placeholder) | SQLAlchemy URL of the wa_llm PostgreSQL database. Must use the `postgresql+asyncpg://` driver. Always set it. |
| `WHATSAPP_BACKEND` | `auto` | `auto`, `openwa` or `gowa` (see [WhatsApp backends](#whatsapp-backends)). Unknown values behave like `auto`. |
| `WHATSAPP_HOST` | `http://localhost:2785` for OpenWA, `http://localhost:3000` otherwise | Base URL of the WhatsApp API. The default is chosen when `OPENWA_API_KEY` is set or `WHATSAPP_BACKEND=openwa`. |
| `OPENWA_API_KEY` | empty | OpenWA API key, sent as `X-API-Key`. Setting it selects the OpenWA backend in `auto` mode. |
| `OPENWA_SESSION_ID` | empty | OpenWA session id to monitor. Required for the OpenWA backend (a missing id is counted as `error_type="whatsapp_config_error"`). |
| `BOT_JIDS` | empty | Comma-separated JIDs of the bot itself (phone `...@s.whatsapp.net` and/or `...@lid`). Excluded from the sender metrics. |
| `WHATSAPP_BASIC_AUTH_USER` | `admin` | Basic Auth user for the gowa API (gowa backend only). |
| `WHATSAPP_BASIC_AUTH_PASSWORD` | `admin` | Basic Auth password for the gowa API (gowa backend only). |
| `LOG_LEVEL` | `INFO` | Python logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |

Notes:

- Precedence: there are no flags or config files, so the environment is the only source. Backend
  choice: `WHATSAPP_BACKEND=openwa|gowa` wins; otherwise `OPENWA_API_KEY` set means OpenWA; otherwise
  gowa.
- Basic Auth (gowa only) is sent only when **both** the user and the password are non-empty. If a variable is
  not set at all, the code falls back to `admin`. The compose file sets them to an empty string
  when they are missing from your environment, which disables Basic Auth.
- The listen port is fixed at **9100** in the code. The `PORT` variable in the Dockerfile has no
  effect. To use another port, change the host side of the port mapping (for example
  `-p 9200:9100`).
- The database pool uses `pool_size=5`, `max_overflow=10` and `pool_pre_ping=True`. WhatsApp API
  requests time out after 30 seconds.

## HTTP endpoints

| Endpoint | Description |
|----------|-------------|
| `/metrics` | Collects all metrics, then returns them in the Prometheus text format. |
| `/health`, `/healthz` | Liveness check. Always returns `{"status": "ok"}`. |
| `/ready`, `/readyz` | Readiness check. Runs `SELECT 1` against the database. Returns `{"status": "ready"}`, or HTTP 503 with `{"status": "not ready", "error": "..."}`. |

## Metrics

All metrics use the `whatsapp_` prefix.

### WhatsApp connection

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `whatsapp_devices_total` | Gauge | none | OpenWA: `1` when the session endpoint answers, else `0`. gowa: devices returned by `/app/devices`. |
| `whatsapp_connection_status` | Gauge | none | OpenWA: `1` when the session status is `ready`. gowa: `1` when `/app/devices` returns a device. `0` otherwise or on error. |
| `whatsapp_session_status` | Gauge | `status` | OpenWA only. `1` for the current session status (`ready`, `qr`, ...); other labels are removed each scrape. |
| `whatsapp_api_groups` | Gauge | none | Groups reported live by the WhatsApp API. `NaN` when the session is not ready or the listing failed. Compare with `whatsapp_groups_total` to spot drift. |
| `whatsapp_device_info` | Info | `name`, `device` | OpenWA: `pushName` (if the API returns one) and the session phone as a JID. gowa: name and JID of the first device. |
| `whatsapp_api_latency_seconds` | Histogram | `endpoint` | WhatsApp API response time. OpenWA endpoints: `/api/sessions/{id}`, `/api/sessions/{id}/groups`. gowa: `/app/devices`, `/user/my/groups`. Buckets: 0.01 to 10 s. |

### Messages

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `whatsapp_messages_total` | Gauge | none | All rows in `message`. |
| `whatsapp_messages_today` | Gauge | none | Messages since midnight **UTC**. |
| `whatsapp_messages_last_24h` | Gauge | none | Messages in the last 24 hours. |
| `whatsapp_messages_last_hour` | Gauge | none | Messages in the last hour. |
| `whatsapp_messages_direct_total` | Gauge | none | Messages with no `group_jid` (direct/private). |
| `whatsapp_messages_group_total` | Gauge | none | Messages with a `group_jid`. |
| `whatsapp_messages_with_media_total` | Gauge | none | Messages with a `media_url` **or** a text starting with `[[Attached `. OpenWA messages no longer store `media_url`, and uncaptioned media has no text, so this is a lower bound for new messages. |
| `whatsapp_messages_per_group` | Gauge | `group_jid`, `group_name` | All-time message count for the top 50 groups. `group_name` is the admin `display_name` (if the column exists), else a non-empty `group_name`, else the JID (quotes removed, cut to 50 characters). Reset every scrape. |
| `whatsapp_messages_by_type` | Gauge | `message_type` | Messages grouped by the `[[Attached X]]` text prefix (`image`, `video`, `audio`, `voice`, `document`, `sticker`, `gif`, `contact`, `location`, `poll`, `list`, `order`); no prefix is `text`; unknown prefixes are bucketed as `other` (the text is user-controlled). Reset every scrape. |

### Groups

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `whatsapp_groups_total` | Gauge | none | All rows in `"group"`. |
| `whatsapp_groups_managed` | Gauge | none | Groups with `managed = true`. |
| `whatsapp_groups_with_spam_notification` | Gauge | none | Groups with `notify_on_spam = true`. |
| `whatsapp_groups_with_community` | Gauge | none | Groups with non-null `community_keys`. |

### Senders

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `whatsapp_senders_total` | Gauge | none | Rows in `sender`, excluding `BOT_JIDS`. |
| `whatsapp_senders_active_24h` | Gauge | none | Distinct senders with a message in the last 24 hours, excluding `BOT_JIDS`. |
| `whatsapp_senders_by_kind` | Gauge | `kind` (`phone`, `lid`) | Rows in `sender` by JID kind (`@lid` or not), excluding `BOT_JIDS`. A high `lid` share means LID-to-phone resolution is not working, and one person may appear twice. |
| `whatsapp_messages_per_sender` | Gauge | `sender_jid`, `sender_name`, `jid_kind` | All-time message count for the top 10 senders, excluding `BOT_JIDS`. `jid_kind` is `phone` or `lid`. `sender_name` is the push name, or the JID when there is none (quotes removed, cut to 50 characters). Reset every scrape. |

### Reactions, opt-outs and knowledge base

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `whatsapp_reactions_total` | Gauge | none | Rows in `reaction`. |
| `whatsapp_optouts_total` | Gauge | none | Rows in `opt_out`. |
| `whatsapp_kb_topics_total` | Gauge | none | Rows in `kbtopic`. |

### Database

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `whatsapp_db_connection_status` | Gauge | none | `1` when the database answers `SELECT 1`, `0` when collection failed. Individual query failures are in `scrape_errors_total{error_type="query:..."}`. |
| `whatsapp_db_query_latency_seconds` | Histogram | `query_type` | Latency per query. Buckets: 0.001 to 1 s. |
| `whatsapp_db_table_rows` | Gauge | `table_name` (`message`, `sender`, `group`, `reaction`, `opt_out`, `kbtopic`, `kb_topic_message`) | Exact row count (`COUNT(*)`) per table. |

`query_type` values: `connection_test`, `messages_total`, `messages_today`, `messages_last_24h`,
`messages_last_hour`, `messages_direct`, `messages_group`, `messages_with_media`,
`messages_per_group`, `groups_total`, `groups_managed`, `groups_spam_notify`, `groups_community`,
`senders_total`, `senders_active_24h`, `messages_per_sender`, `senders_by_kind`, `messages_by_type`,
`reactions_total`, `optouts_total`, `kb_topics_total`, and `table_rows_<table>` for each table above.

### Exporter

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `whatsapp_exporter_last_scrape_timestamp` | Gauge | none | Unix time of the last collection. |
| `whatsapp_exporter_scrape_duration_seconds` | Histogram | none | Duration of a collection. Buckets: 0.1 to 30 s. |
| `whatsapp_exporter_scrape_errors_total` | Counter | `error_type` | Collection errors. Values: `whatsapp_api_error` (non-200 or unparseable session/devices response), `whatsapp_connection_error`, `whatsapp_groups_error` (OpenWA group listing failed), `whatsapp_config_error` (OpenWA backend without `OPENWA_SESSION_ID`), `query:<name>` (one database query failed), `database_error` (connection failed), `general_error` (practically unreachable: `asyncio.gather(..., return_exceptions=True)` swallows collector exceptions). |

Histograms also expose `_bucket`, `_sum`, `_count` and `_created` series, and the counter exposes
`_created`. The default `prometheus_client` metrics (`process_*`, `python_gc_*`, `python_info`)
are exported as well.

## Prometheus configuration

Each scrape runs about 30 database queries, several of them over the whole `message` table, so
keep the interval moderate:

```yaml
scrape_configs:
  - job_name: wa_llm
    scrape_interval: 30s
    scrape_timeout: 20s
    static_configs:
      - targets: ["<exporter-host>:9100"]
```

Example queries:

```promql
# Messages received in the last hour
whatsapp_messages_last_hour

# Top 10 groups by message count
topk(10, whatsapp_messages_per_group)

# WhatsApp API latency, 95th percentile
histogram_quantile(0.95, sum by (le, endpoint) (rate(whatsapp_api_latency_seconds_bucket[5m])))

# Slowest database queries, 95th percentile
topk(5, histogram_quantile(0.95, sum by (le, query_type) (rate(whatsapp_db_query_latency_seconds_bucket[5m]))))

# Both WhatsApp and the database are up
whatsapp_connection_status == 1 and whatsapp_db_connection_status == 1

# Collection errors in the last 15 minutes
increase(whatsapp_exporter_scrape_errors_total[15m])
```

## Grafana dashboard

`dashboard.json` is the **WhatsApp LLM Dashboard** (uid `whatsapp-llm-dashboard`, tags `whatsapp`,
`prometheus`, default time range of the last 6 hours). It uses a `datasource` template variable of
type Prometheus, so it works with any Prometheus data source.

Panels:

| Panel | Type | Query |
|-------|------|-------|
| Total Messages | Stat | `whatsapp_messages_total` |
| Total Groups | Stat | `whatsapp_groups_total` |
| Media Messages (captioned + legacy) | Stat | `whatsapp_messages_with_media_total` |
| WhatsApp Connected | Stat | `whatsapp_connection_status` |
| Top 10 Senders by Messages | Bar chart | `topk(10, whatsapp_messages_per_sender)` |
| Top 5 Groups by Messages | Bar chart | `topk(5, whatsapp_messages_per_group)` |
| Messages Distribution | Pie chart | `whatsapp_messages_direct_total`, `whatsapp_messages_group_total` |
| Messages Today | Stat | `whatsapp_messages_today` |
| Messages Last 24h | Stat | `whatsapp_messages_last_24h` |
| Total Senders | Stat | `whatsapp_senders_total` |
| Active Senders (24h) | Stat | `whatsapp_senders_active_24h` |
| Managed Groups | Stat | `whatsapp_groups_managed` |
| Reactions | Stat | `whatsapp_reactions_total` |
| Opt-outs | Stat | `whatsapp_optouts_total` |
| Scrape Duration (P95) | Stat | `histogram_quantile(0.95, rate(whatsapp_exporter_scrape_duration_seconds_bucket[5m]))` |
| WhatsApp Session Status | Stat | `whatsapp_session_status == 1` |
| Senders by ID Type (phone vs lid) | Pie chart | `whatsapp_senders_by_kind` |

The dashboard uses schema version 39, so it needs a recent Grafana release.
<!-- TODO: verify the minimum Grafana version (schemaVersion 39 suggests Grafana 10.4 or later) -->

<!-- TODO: screenshot of the Grafana dashboard -->

To import it:

1. In Grafana, open **Dashboards → New → Import**.
2. Upload `dashboard.json` or paste its contents, then click **Import**.
3. Pick your Prometheus data source in the **Datasource** drop-down at the top of the dashboard.

## Troubleshooting

- **All database metrics are stale or `whatsapp_db_connection_status` is `0`**: check the
  `Database metrics collection failed` log line and `/ready`. `DB_URI` must start with
  `postgresql+asyncpg://`. When a collection fails, the gauges keep their last values, so watch
  `whatsapp_db_connection_status` and `whatsapp_exporter_scrape_errors_total` instead of the
  counts alone.
- **`whatsapp_connection_status` is `0`**: check `whatsapp_session_status` (OpenWA: anything but
  `ready`, such as `qr`, means the session needs attention) and the logs. A connection failure is
  `error_type="whatsapp_connection_error"`; a non-200 answer (for example `401`/`403` from a wrong
  `OPENWA_API_KEY`, or `404` from a wrong `OPENWA_SESSION_ID`) is `error_type="whatsapp_api_error"`.
  Pointing a gowa-style setup at OpenWA (or vice versa) also fails: check `WHATSAPP_BACKEND`.
- **A metric is missing or stale and `scrape_errors_total{error_type="query:<name>"}` grows**: that
  one query failed (see the `Query <name> failed` log line); the other metrics are unaffected.
- **`host.docker.internal` does not resolve** (plain `docker run` on Linux): see the note in
  [Docker Compose](#docker-compose).
- **Scrapes time out**: each scrape queries the database live. Raise `scrape_timeout`, lower the
  scrape frequency, or check `whatsapp_db_query_latency_seconds` for slow queries. A WhatsApp API
  request can wait up to 30 seconds before it times out.
- **Setting `PORT` has no effect**: the port is fixed at 9100; remap it on the Docker side.

## Security and privacy

- **Use a read-only database user.** The exporter only runs `SELECT` statements. Create a dedicated
  role with `SELECT` on the wa_llm tables it reads, and nothing else:

  ```sql
  CREATE ROLE wa_llm_exporter LOGIN PASSWORD '<password>';
  GRANT SELECT ON message, "group", sender, reaction, opt_out, kbtopic, kb_topic_message TO wa_llm_exporter;
  ```
- **Do not expose `/metrics` publicly.** The exporter has no authentication or TLS. Keep port 9100
  on a private network, or put it behind a reverse proxy with authentication.
- **The metrics contain personal data.** `whatsapp_messages_per_group` exposes group JIDs and
  names, `whatsapp_messages_per_sender` exposes the phone-number-based JIDs and WhatsApp display
  names of the most active senders, and `whatsapp_device_info` exposes the bot's own JID. Anyone
  who can read Prometheus or the Grafana dashboard can see them. Restrict access accordingly and
  consider your retention settings and local privacy rules.
- **`/ready` returns the raw database error text** on failure, which can reveal host names. Don't
  expose it outside your network.
- **Set the WhatsApp API credentials explicitly.** `OPENWA_API_KEY` is sent only as a header and is
  never logged. For gowa, if the Basic Auth variables are not set, the code falls back to
  `admin` / `admin`. Pass secrets through environment variables or a `.env` file, and never
  commit real values to `docker-compose.yml`.
- The Docker image runs as `root` (the Dockerfile sets no `USER`).

## Development

Project layout:

```text
app/app.py                          # the exporter (Starlette app, metrics and collectors)
dashboard.json                      # Grafana dashboard
requirements.txt                    # starlette, uvicorn, prometheus-client, sqlalchemy[asyncio], asyncpg, httpx
requirements-dev.txt                # pytest, pytest-asyncio, httpx (pinned)
tests/                              # pytest suite
Dockerfile                          # python:3.12-slim image; runs `python app.py`
docker-compose.yml                  # standalone deployment of the published image
.github/workflows/docker-image.yml  # manual Docker Hub build
```

Run locally with auto-reload:

```bash
pip install -r requirements.txt
export DB_URI="postgresql+asyncpg://<db-user>:<db-password>@localhost:5432/<db-name>"
uvicorn app:app --app-dir app --host 0.0.0.0 --port 9100 --reload
```

Tests (pytest): `pip install -r requirements.txt -r requirements-dev.txt && pytest`. The WhatsApp
tests use `httpx.MockTransport`. The database tests need a throwaway PostgreSQL (they drop and
recreate the exporter's tables in it):

```bash
docker run -d --name exporter-test-pg -e POSTGRES_USER=user -e POSTGRES_PASSWORD=password \
  -e POSTGRES_DB=exp_test -p 55437:5432 postgres:17
# override with EXPORTER_TEST_DB_URI if you use another database
docker rm -fv exporter-test-pg   # when done
```

There is no linter configuration.

**Releases**: the `Docker Build` workflow runs only on manual dispatch (`workflow_dispatch`). It
builds `linux/amd64` and `linux/arm64` images and pushes `techblog/wa-llm-exporter:latest` plus the
short commit SHA. The workflow tries `git describe --tags` first, but its shallow checkout fetches
no tags, so it always falls back to the SHA. The repository has no git tags or GitHub releases.

## Contributing

Issues and pull requests are welcome. Keep the metric names stable, because the Grafana dashboard
and existing alerts depend on them, and update this README when you add or change a metric or
setting.

## License

Licensed under the [Apache License 2.0](LICENSE).
