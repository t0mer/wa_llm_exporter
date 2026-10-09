from prometheus_client import REGISTRY

import app as exporter


def sample(name, labels=None):
    return REGISTRY.get_sample_value(name, labels or {})


def series(name):
    return [s for m in REGISTRY.collect() if m.name == name for s in m.samples]


async def test_spoofed_attach_types_bucket_to_other(db):
    for i in range(50):
        await db(
            "INSERT INTO message (message_id, timestamp, text) VALUES (:i, now(), :t)",
            i=f"s{i}", t=f"[[Attached a{i}]] hi",
        )
    await db("INSERT INTO message (message_id, timestamp, text) VALUES ('r', now(), '[[Attached IMAGE]]')")
    await db("INSERT INTO message (message_id, timestamp, text) VALUES ('t', now(), 'hello')")
    await exporter.collect_database_metrics()
    types = {s.labels["message_type"]: s.value for s in series("whatsapp_messages_by_type")}
    assert types == {"other": 50, "image": 1, "text": 1}


async def test_group_query_falls_back_without_display_name(db):
    await db('INSERT INTO "group" (group_jid, group_name) VALUES (\'g1@g.us\', \'Old name\')')
    await db("INSERT INTO message (message_id, timestamp, group_jid) VALUES ('m', now(), 'g1@g.us')")
    await db('ALTER TABLE "group" DROP COLUMN display_name')
    await exporter.collect_database_metrics()
    assert sample("whatsapp_messages_per_group", {"group_jid": "g1@g.us", "group_name": "Old name"}) == 1
    assert sample("whatsapp_groups_total") == 1


async def test_empty_group_name_falls_back_to_jid(db):
    await db("""INSERT INTO "group" (group_jid, group_name, display_name) VALUES ('g1@g.us', '', NULL)""")
    await exporter.collect_database_metrics()
    assert sample("whatsapp_messages_per_group", {"group_jid": "g1@g.us", "group_name": "g1@g.us"}) == 0


async def test_null_sender_consistent_with_and_without_bot_jids(db, monkeypatch):
    await db("INSERT INTO message (message_id, timestamp, sender_jid) VALUES ('n', now(), NULL)")
    for bots in ([], ["bot@lid"]):
        monkeypatch.setattr(exporter, "BOT_JIDS", bots)
        await exporter.collect_database_metrics()
        assert sample(
            "whatsapp_messages_per_sender",
            {"sender_jid": "unknown", "sender_name": "unknown", "jid_kind": "phone"},
        ) == 1
