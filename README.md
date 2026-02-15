#  wa_llm Prometheus Exporter

A Prometheus exporter for WhatsApp LLM platform metrics. Exports comprehensive metrics about WhatsApp connectivity, messages, groups, senders, and database performance.

## Metrics Exported

### WhatsApp Connection Metrics
| Metric | Type | Description |
|--------|------|-------------|
| `whatsapp_devices_total` | Gauge | Total number of WhatsApp devices connected |
| `whatsapp_connection_status` | Gauge | Connection status (1=connected, 0=disconnected) |
| `whatsapp_device` | Info | Device information (name, device JID) |
| `whatsapp_api_latency_seconds` | Histogram | API response latency by endpoint |

### Message Metrics
| Metric | Type | Description |
|--------|------|-------------|
| `whatsapp_messages_total` | Gauge | Total number of messages in database |
| `whatsapp_messages_today` | Gauge | Messages received today |
| `whatsapp_messages_last_24h` | Gauge | Messages in the last 24 hours |
| `whatsapp_messages_last_hour` | Gauge | Messages in the last hour |
| `whatsapp_messages_direct_total` | Gauge | Total direct/private messages |
| `whatsapp_messages_group_total` | Gauge | Total group messages |
| `whatsapp_messages_with_media_total` | Gauge | Messages containing media |
| `whatsapp_messages_per_group` | Gauge | Message count per group (labeled by group_jid, group_name) |

### Group Metrics
| Metric | Type | Description |
|--------|------|-------------|
| `whatsapp_groups_total` | Gauge | Total number of WhatsApp groups |
| `whatsapp_groups_managed` | Gauge | Number of managed groups |
| `whatsapp_groups_with_spam_notification` | Gauge | Groups with spam notification enabled |
| `whatsapp_groups_with_community` | Gauge | Groups with community keys |

### Sender Metrics
| Metric | Type | Description |
|--------|------|-------------|
| `whatsapp_senders_total` | Gauge | Total unique senders/contacts |
| `whatsapp_senders_active_24h` | Gauge | Active senders in last 24 hours |
| `whatsapp_messages_per_sender` | Gauge | Messages per sender (top 10, labeled by sender_jid, sender_name) |

### Other Metrics
| Metric | Type | Description |
|--------|------|-------------|
| `whatsapp_reactions_total` | Gauge | Total message reactions |
| `whatsapp_optouts_total` | Gauge | Total opt-outs |
| `whatsapp_kb_topics_total` | Gauge | Knowledge base topics |

### Database Performance Metrics
| Metric | Type | Description |
|--------|------|-------------|
| `whatsapp_db_connection_status` | Gauge | Database connection status |
| `whatsapp_db_query_latency_seconds` | Histogram | Query latency by query type |
| `whatsapp_db_table_rows` | Gauge | Row count per table |

### Exporter Metrics
| Metric | Type | Description |
|--------|------|-------------|
| `whatsapp_exporter_last_scrape_timestamp` | Gauge | Last successful scrape timestamp |
| `whatsapp_exporter_scrape_duration_seconds` | Histogram | Scrape duration |
| `whatsapp_exporter_scrape_errors_total` | Counter | Scrape errors by type |

## Configuration

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `9100` | Port to expose metrics |
| `DB_URI` | - | PostgreSQL async connection string |
| `WHATSAPP_HOST` | `http://localhost:3000` | WhatsApp API base URL |
| `WHATSAPP_BASIC_AUTH_USER` | - | WhatsApp API basic auth username |
| `WHATSAPP_BASIC_AUTH_PASSWORD` | - | WhatsApp API basic auth password |
| `LOG_LEVEL` | `INFO` | Logging level |

**Note:** Metrics are collected on-demand when `/metrics` endpoint is called (no background scraping).

## Running

### With Docker Compose (Integrated)

The exporter is included in the main `docker-compose.prod.yml`:

```bash
docker compose -f docker-compose.prod.yml up -d prometheus-exporter
```

### With Docker Compose (Standalone)

```bash
cd prometheus_exporter
docker compose up -d
```

### With Docker

```bash
cd prometheus_exporter
docker build -t wa-prometheus-exporter .
docker run -d \
  -p 9100:9100 \
  -e DB_URI="postgresql+asyncpg://user:password@host:5432/postgres" \
  -e WHATSAPP_HOST="http://host:3000" \
  -e WHATSAPP_BASIC_AUTH_USER="user" \
  -e WHATSAPP_BASIC_AUTH_PASSWORD="password" \
  wa-prometheus-exporter
```

### Without Docker

```bash
cd prometheus_exporter
pip install -r requirements.txt
export DB_URI="postgresql+asyncpg://user:password@localhost:5432/postgres"
export WHATSAPP_HOST="http://localhost:3000"
python exporter.py
```

## Endpoints

| Endpoint | Description |
|----------|-------------|
| `/metrics` | Prometheus metrics |
| `/health` | Health check |
| `/healthz` | Health check (K8s style) |
| `/ready` | Readiness check (verifies DB connection) |
| `/readyz` | Readiness check (K8s style) |

## Prometheus Configuration

Add to your `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: 'whatsapp'
    static_configs:
      - targets: ['localhost:9100']
    scrape_interval: 30s
```

## Grafana Dashboard

A pre-built Grafana dashboard is included in `grafana-dashboard.json`. 

### Dashboard Panels:
- **Total Messages** - Overall message count
- **Total Groups** - Number of WhatsApp groups
- **Media Messages** - Messages with media attachments
- **WhatsApp Connected** - Connection status indicator
- **Top 10 Senders by Messages** - Horizontal bar chart
- **Top 5 Groups by Messages** - Horizontal bar chart
- **Messages Distribution** - Pie chart (Direct vs Group)
- Plus additional stats for messages today/24h, senders, reactions, opt-outs

### Import Dashboard:
1. Go to Grafana → Dashboards → Import
2. Upload `grafana-dashboard.json` or paste its contents
3. Select your Prometheus datasource
4. Click Import

Example queries for custom panels:

```promql
# Message rate (last 5 minutes)
rate(whatsapp_messages_last_hour[5m])

# Messages by group
topk(10, whatsapp_messages_per_group)

# API latency P95
histogram_quantile(0.95, whatsapp_api_latency_seconds_bucket)

# Active senders trend
whatsapp_senders_active_24h

# Connection health
whatsapp_connection_status == 1 and whatsapp_db_connection_status == 1
```

## License

Same as the main wa_llm project.

