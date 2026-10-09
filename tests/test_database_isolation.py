from prometheus_client import REGISTRY

import app as exporter


def sample(name, labels=None):
    return REGISTRY.get_sample_value(name, labels or {})


def errors(kind):
    return sample("whatsapp_exporter_scrape_errors_total", {"error_type": kind}) or 0


async def seed_basic(db):
    await db("INSERT INTO opt_out VALUES ('a@s.whatsapp.net'), ('b@s.whatsapp.net')")
    await db("INSERT INTO kbtopic VALUES ('t1'), ('t2'), ('t3')")
    await db("INSERT INTO reaction VALUES ('r1', 'x')")
    await db(
        "INSERT INTO message (message_id, timestamp, sender_jid) "
        "VALUES ('m1', now(), 'a@s.whatsapp.net')"
    )


async def test_opt_out_and_kbtopic_are_counted(db):
    await seed_basic(db)
    await exporter.collect_database_metrics()
    assert sample("whatsapp_optouts_total") == 2
    assert sample("whatsapp_kb_topics_total") == 3
    assert sample("whatsapp_reactions_total") == 1


async def test_failing_query_does_not_poison_the_next(db):
    await seed_basic(db)
    await db("DROP TABLE reaction")  # reactions query runs before opt_out/kbtopic
    before = errors("query:reactions_total")
    await exporter.collect_database_metrics()
    assert errors("query:reactions_total") == before + 1
    assert sample("whatsapp_optouts_total") == 2
    assert sample("whatsapp_kb_topics_total") == 3
    assert sample("whatsapp_db_table_rows", {"table_name": "message"}) == 1
    assert sample("whatsapp_db_connection_status") == 1


async def test_missing_opt_out_table_still_counts_kb_topics(db):
    await seed_basic(db)
    await db("DROP TABLE opt_out")
    before = errors("query:optouts_total")
    await exporter.collect_database_metrics()
    assert errors("query:optouts_total") == before + 1
    assert sample("whatsapp_kb_topics_total") == 3
    assert sample("whatsapp_db_table_rows", {"table_name": "kbtopic"}) == 3


async def test_db_table_rows_lists_real_table_names(db):
    await seed_basic(db)
    await exporter.collect_database_metrics()
    assert sample("whatsapp_db_table_rows", {"table_name": "opt_out"}) == 2
    assert sample("whatsapp_db_table_rows", {"table_name": "kbtopic"}) == 3
    assert sample("whatsapp_db_table_rows", {"table_name": "group"}) == 0
