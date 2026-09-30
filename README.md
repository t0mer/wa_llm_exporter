# wa_llm Prometheus Exporter

A Prometheus exporter for [wa_llm](https://github.com/ilanbenb/wa_llm), the AI-powered WhatsApp
group bot. It reads the wa_llm PostgreSQL database and the WhatsApp HTTP API that wa_llm runs on
([go-whatsapp-web-multidevice](https://github.com/aldinokemal/go-whatsapp-web-multidevice)), and
exposes metrics about WhatsApp connectivity, messages, groups, senders, reactions, opt-outs,
knowledge-base topics and database query latency. A ready-made Grafana dashboard is included.

It is meant for people who self-host wa_llm (for example the fork at
[t0mer/wa_llm](https://github.com/t0mer/wa_llm)) and want to watch the bot in Prometheus and Grafana.

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

- **WhatsApp connectivity**: number of devices, connection status, device name and JID, and API
  latency, read from the go-whatsapp-web-multidevice API.
- **Message statistics**: total, today (UTC), last 24 hours, last hour, direct vs. group, and
  messages with media.
- **Per-group and per-sender counts**: message count for the top 50 groups and the top 10 senders.
- **Group statistics**: total, managed, spam notification enabled, and groups with community keys.
- **Sender statistics**: total senders and active senders in the last 24 hours.
- **Reactions, opt-outs and knowledge-base topics** counts.
- **Database health**: connection status, per-query latency histograms and row counts per table.
- **Exporter self-metrics**: last scrape timestamp, scrape duration and error counters.
- **Health and readiness endpoints** for Docker and Kubernetes probes.
- **Grafana dashboard** (`dashboard.json`) with 15 panels.
- Multi-arch Docker image (`linux/amd64`, `linux/arm64`) on Docker Hub.

## How it works

The exporter is a small [Starlette](https://www.starlette.io/) app served by Uvicorn on port
`9100`. There is no background polling and no cache: every request to `/metrics` runs a full
collection, and the WhatsApp API and the database are queried in parallel.

```mermaid
flowchart LR
    P[Prometheus] -- "GET /metrics" --> E[wa_llm exporter :9100]
    E -- "GET /app/devices<br/>GET /user/my/groups<br/>(HTTP Basic Auth)" --> W[go-whatsapp-web-multidevice API]
    E -- "SELECT COUNT(*) ...<br/>(SQLAlchemy + asyncpg)" --> D[(wa_llm PostgreSQL)]
    G[Grafana] -- PromQL --> P
```

**WhatsApp API** (`WHATSAPP_HOST`):

- `GET /app/devices`: sets the device count and connection status, and reports the first device's
  name and JID.
- `GET /user/my/groups` (with the `X-Device-Id` header): called when a device was found. Only its
  latency is recorded; the group list itself is not exported.

**Database** (`DB_URI`): read-only `SELECT COUNT(*)` queries against these wa_llm tables:

| Table | Columns used |
|-------|--------------|
| `message` | `message_id`, `timestamp`, `group_jid`, `sender_jid`, `media_url` |
| `"group"` | `group_jid`, `group_name`, `managed`, `notify_on_spam`, `community_keys` |
| `sender` | `jid`, `push_name` |
| `reaction` | row count only |
| `optout` | row count only (**known issue:** the wa_llm table is named `opt_out`, see below) |
| `kbtopic` | row count only |

If the `reaction`, `optout` or `kbtopic` query fails, the exporter catches the error and sets that
metric to `0`. The error is not logged or counted.

> **Known issue: `optout` vs. `opt_out`.** The exporter queries a table named `optout`, but the
> wa_llm schema names it `opt_out`. The query therefore fails, and in PostgreSQL a failed statement
> aborts the session's transaction. Every later query in the same collection also fails silently:
> the `kbtopic` count and all five `whatsapp_db_table_rows` queries. Against current wa_llm this
> means `whatsapp_optouts_total` and `whatsapp_kb_topics_total` are always `0`, and
> `whatsapp_db_table_rows` has no samples. The transaction-abort chain is inferred from standard
> PostgreSQL behaviour and was not tested against a database.

## Requirements

- A running wa_llm deployment:
  - its PostgreSQL database, reachable from the exporter;
  - its go-whatsapp-web-multidevice API, reachable from the exporter (optional; without it only the
    WhatsApp metrics are missing).
- Docker, **or** Python 3.12 (the version used by the Docker image) to run from source.
- Prometheus to scrape the exporter, and optionally Grafana for the dashboard.

## Installation

### Docker Compose

The included `docker-compose.yml` runs the published image `techblog/wa-llm-exporter` (tag
`latest`) as the container `wa_llm_exporter` on port `9100`, with `restart: unless-stopped` and a
health check on `/health`.

1. Edit `DB_URI` and `WHATSAPP_HOST` in `docker-compose.yml`. The defaults point at
   `host.docker.internal` with placeholder credentials.
2. Set the WhatsApp API credentials in your shell or in a `.env` file next to the compose file:

   ```bash
   WHATSAPP_BASIC_AUTH_USER=<whatsapp-api-user>
   WHATSAPP_BASIC_AUTH_PASSWORD=<whatsapp-api-password>
   ```

3. Start it:

   ```bash
   docker compose up -d
   curl -s http://localhost:9100/metrics | head
   ```

On Linux, `host.docker.internal` does not resolve by default. Add
`extra_hosts: ["host.docker.internal:host-gateway"]` to the service, or use the real host name or
IP address of the database and the WhatsApp API.

If wa_llm itself runs in Docker Compose, you can also add the exporter as a service to that stack
and point it at the service names (for example `postgres` and `http://whatsapp:3000`).

### Docker

```bash
docker run -d \
  --name wa_llm_exporter \
  -p 9100:9100 \
  -e DB_URI="postgresql+asyncpg://<db-user>:<db-password>@<db-host>:5432/<db-name>" \
  -e WHATSAPP_HOST="http://<whatsapp-host>:3000" \
  -e WHATSAPP_BASIC_AUTH_USER="<whatsapp-api-user>" \
  -e WHATSAPP_BASIC_AUTH_PASSWORD="<whatsapp-api-password>" \
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
export WHATSAPP_HOST="http://localhost:3000"
export WHATSAPP_BASIC_AUTH_USER="<whatsapp-api-user>"
export WHATSAPP_BASIC_AUTH_PASSWORD="<whatsapp-api-password>"

python app/app.py
```

## Configuration

All configuration is through environment variables. There are no command-line flags or config
files.

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_URI` | `postgresql+asyncpg://user:password@localhost:5432/postgres` (placeholder) | SQLAlchemy URL of the wa_llm PostgreSQL database. Must use the `postgresql+asyncpg://` driver. Always set it. |
| `WHATSAPP_HOST` | `http://localhost:3000` | Base URL of the go-whatsapp-web-multidevice API. |
| `WHATSAPP_BASIC_AUTH_USER` | `admin` | Basic Auth user for the WhatsApp API. |
| `WHATSAPP_BASIC_AUTH_PASSWORD` | `admin` | Basic Auth password for the WhatsApp API. |
| `LOG_LEVEL` | `INFO` | Python logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |

Notes:

- Basic Auth is sent only when **both** the user and the password are non-empty. If a variable is
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
| `whatsapp_devices_total` | Gauge | none | Number of devices returned by `/app/devices`. |
| `whatsapp_connection_status` | Gauge | none | `1` when `/app/devices` returns at least one device, `0` otherwise or on error. |
| `whatsapp_device_info` | Info | `name`, `device` | Name and JID of the first device (value is always `1`). |
| `whatsapp_api_latency_seconds` | Histogram | `endpoint` (`/app/devices`, `/user/my/groups`) | WhatsApp API response time. Buckets: 0.01 to 10 s. |

### Messages

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `whatsapp_messages_total` | Gauge | none | All rows in `message`. |
| `whatsapp_messages_today` | Gauge | none | Messages since midnight **UTC**. |
| `whatsapp_messages_last_24h` | Gauge | none | Messages in the last 24 hours. |
| `whatsapp_messages_last_hour` | Gauge | none | Messages in the last hour. |
| `whatsapp_messages_direct_total` | Gauge | none | Messages with no `group_jid` (direct/private). |
| `whatsapp_messages_group_total` | Gauge | none | Messages with a `group_jid`. |
| `whatsapp_messages_with_media_total` | Gauge | none | Messages with a `media_url`. |
| `whatsapp_messages_per_group` | Gauge | `group_jid`, `group_name` | All-time message count for the top 50 groups. The group name has quotes removed and is cut to 50 characters. |
| `whatsapp_messages_by_type` | Gauge | `message_type` | Declared but never set, so it has no samples. |

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
| `whatsapp_senders_total` | Gauge | none | All rows in `sender`. |
| `whatsapp_senders_active_24h` | Gauge | none | Distinct senders with a message in the last 24 hours. |
| `whatsapp_messages_per_sender` | Gauge | `sender_jid`, `sender_name` | All-time message count for the top 10 senders. `sender_name` is the WhatsApp push name, or the JID when there is none (quotes removed, cut to 50 characters). Reset on every scrape. |

### Reactions, opt-outs and knowledge base

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `whatsapp_reactions_total` | Gauge | none | Rows in `reaction` (`0` if the query fails). |
| `whatsapp_optouts_total` | Gauge | none | Rows in `optout` (`0` if the query fails). **Always `0` against current wa_llm**, whose table is `opt_out` (see [known issue](#how-it-works)). |
| `whatsapp_kb_topics_total` | Gauge | none | Rows in `kbtopic` (`0` if the query fails). **Always `0` against current wa_llm**, because the earlier `optout` query aborts the transaction (see [known issue](#how-it-works)). |

### Database

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `whatsapp_db_connection_status` | Gauge | none | `1` when the collection queries succeeded, `0` when collection failed. |
| `whatsapp_db_query_latency_seconds` | Histogram | `query_type` | Latency per query. Buckets: 0.001 to 1 s. |
| `whatsapp_db_table_rows` | Gauge | `table_name` (`message`, `sender`, `group`, `reaction`, `optout`) | Exact row count (`COUNT(*)`) per table. **No samples against current wa_llm**, because the earlier `optout` query aborts the transaction (see [known issue](#how-it-works)). |

`query_type` values: `connection_test`, `messages_total`, `messages_today`, `messages_last_24h`,
`messages_last_hour`, `messages_direct`, `messages_group`, `messages_with_media`,
`messages_per_group`, `groups_total`, `groups_managed`, `groups_spam_notify`, `groups_community`,
`senders_total`, `senders_active_24h`, `messages_per_sender`, `reactions_total`, `optouts_total`,
`kb_topics_total`.

### Exporter

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `whatsapp_exporter_last_scrape_timestamp` | Gauge | none | Unix time of the last collection. |
| `whatsapp_exporter_scrape_duration_seconds` | Histogram | none | Duration of a collection. Buckets: 0.1 to 30 s. |
| `whatsapp_exporter_scrape_errors_total` | Counter | `error_type` | Collection errors. Values: `whatsapp_api_error` (non-200 from `/app/devices`), `whatsapp_connection_error`, `database_error`, `general_error` (practically unreachable: `asyncio.gather(..., return_exceptions=True)` swallows collector exceptions). |

Histograms also expose `_bucket`, `_sum`, `_count` and `_created` series, and the counter exposes
`_created`. The default `prometheus_client` metrics (`process_*`, `python_gc_*`, `python_info`)
are exported as well.

## Prometheus configuration

Each scrape runs 24 database queries, several of them over the whole `message` table, so
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
| Media Messages | Stat | `whatsapp_messages_with_media_total` |
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
- **`whatsapp_connection_status` is `0`**: look for `Failed to connect to WhatsApp API` (network or
  URL problem, `error_type="whatsapp_connection_error"`) or `WhatsApp API returned status <code>`
  (for example `401` from wrong Basic Auth credentials, `error_type="whatsapp_api_error"`). It is
  also `0` when the API is reachable but no device is logged in.
- **`whatsapp_optouts_total` and `whatsapp_kb_topics_total` are always `0`, and
  `whatsapp_db_table_rows` is missing**: this is the `optout` vs. `opt_out` table-name
  [known issue](#how-it-works). No error is logged, because the failures are caught silently.
- **`host.docker.internal` does not resolve** (Linux): see the note in
  [Docker Compose](#docker-compose).
- **Scrapes time out**: each scrape queries the database live. Raise `scrape_timeout`, lower the
  scrape frequency, or check `whatsapp_db_query_latency_seconds` for slow queries. A WhatsApp API
  request can wait up to 30 seconds before it times out.
- **A group that dropped out of the top 50 still appears**: `whatsapp_messages_per_group` series are
  not cleared between scrapes, so old groups keep their last value until the exporter restarts.
- **Setting `PORT` has no effect**: the port is fixed at 9100; remap it on the Docker side.

## Security and privacy

- **Use a read-only database user.** The exporter only runs `SELECT` statements. Create a dedicated
  role with `SELECT` on the wa_llm tables it reads, and nothing else:

  ```sql
  CREATE ROLE wa_llm_exporter LOGIN PASSWORD '<password>';
  GRANT SELECT ON message, "group", sender, reaction, opt_out, kbtopic TO wa_llm_exporter;
  ```

  `opt_out` is the wa_llm table name. The exporter currently queries `optout` instead (see the
  [known issue](#how-it-works)), so that query fails until the code is fixed.
- **Do not expose `/metrics` publicly.** The exporter has no authentication or TLS. Keep port 9100
  on a private network, or put it behind a reverse proxy with authentication.
- **The metrics contain personal data.** `whatsapp_messages_per_group` exposes group JIDs and
  names, `whatsapp_messages_per_sender` exposes the phone-number-based JIDs and WhatsApp display
  names of the most active senders, and `whatsapp_device_info` exposes the bot's own JID. Anyone
  who can read Prometheus or the Grafana dashboard can see them. Restrict access accordingly and
  consider your retention settings and local privacy rules.
- **`/ready` returns the raw database error text** on failure, which can reveal host names. Don't
  expose it outside your network.
- **Set the WhatsApp API credentials explicitly.** If they are not set, the code falls back to
  `admin` / `admin`. Pass secrets through environment variables or a `.env` file, and never
  commit real values to `docker-compose.yml`.
- The Docker image runs as `root` (the Dockerfile sets no `USER`).

## Development

Project layout:

```text
app/app.py                          # the exporter (Starlette app, metrics and collectors)
dashboard.json                      # Grafana dashboard
requirements.txt                    # starlette, uvicorn, prometheus-client, sqlalchemy[asyncio], asyncpg, httpx
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

The repository has no tests or linter configuration.

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
