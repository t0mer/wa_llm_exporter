from prometheus_client import REGISTRY

import app as exporter

BOT_PHONE = "111@s.whatsapp.net"
BOT_LID = "999@lid"


def sample(name, labels=None):
    return REGISTRY.get_sample_value(name, labels or {})


async def add_msgs(db, sender, n, group=None, prefix=None):
    for i in range(n):
        await db(
            "INSERT INTO message (message_id, timestamp, sender_jid, group_jid) "
            "VALUES (:i, now(), :s, :g)",
            i=f"{prefix or sender}-{i}", s=sender, g=group,
        )


async def seed(db):
    for jid, name in [
        ("222@s.whatsapp.net", "Alice"),
        ("333@lid", "Bob"),
        (BOT_PHONE, "Bot"),
        (BOT_LID, None),
    ]:
        await db("INSERT INTO sender VALUES (:j, :n)", j=jid, n=name)
    await add_msgs(db, "222@s.whatsapp.net", 3)
    await add_msgs(db, "333@lid", 2)
    await add_msgs(db, BOT_PHONE, 10)
    await add_msgs(db, BOT_LID, 7)


async def test_bot_jids_excluded_and_kind_labelled(db, monkeypatch):
    monkeypatch.setattr(exporter, "BOT_JIDS", [BOT_PHONE, BOT_LID])
    await seed(db)
    await exporter.collect_database_metrics()

    assert sample("whatsapp_senders_total") == 2
    assert sample("whatsapp_senders_active_24h") == 2
    assert sample("whatsapp_senders_by_kind", {"kind": "phone"}) == 1
    assert sample("whatsapp_senders_by_kind", {"kind": "lid"}) == 1
    top = {
        "222@s.whatsapp.net": ("Alice", "phone", 3),
        "333@lid": ("Bob", "lid", 2),
    }
    for jid, (name, kind, n) in top.items():
        labels = {"sender_jid": jid, "sender_name": name, "jid_kind": kind}
        assert sample("whatsapp_messages_per_sender", labels) == n
    for bot in (BOT_PHONE, BOT_LID):
        assert not [
            s for m in REGISTRY.collect() if m.name == "whatsapp_messages_per_sender"
            for s in m.samples if s.labels["sender_jid"] == bot
        ]
    # total message count is untouched by the exclusion
    assert sample("whatsapp_messages_total") == 22


async def test_no_bot_jids_keeps_everyone(db, monkeypatch):
    monkeypatch.setattr(exporter, "BOT_JIDS", [])
    await seed(db)
    await exporter.collect_database_metrics()
    assert sample("whatsapp_senders_total") == 4
    assert sample("whatsapp_senders_by_kind", {"kind": "lid"}) == 2


def test_bot_jids_env_parsing():
    assert exporter.parse_jid_list(" a@lid, b@s.whatsapp.net ,,") == ["a@lid", "b@s.whatsapp.net"]
    assert exporter.parse_jid_list("") == []


async def test_group_label_prefers_display_name_and_stale_series_cleared(db):
    await db("""INSERT INTO "group" (group_jid, group_name, display_name) VALUES
        ('g1@g.us', 'Raw name', 'Admin name'),
        ('g2@g.us', 'Only raw', ''),
        ('g3@g.us', NULL, NULL)""")
    await add_msgs(db, "x", 3, group="g1@g.us", prefix="a")
    await add_msgs(db, "x", 2, group="g2@g.us", prefix="b")
    await add_msgs(db, "x", 1, group="g3@g.us", prefix="c")
    await exporter.collect_database_metrics()
    assert sample("whatsapp_messages_per_group", {"group_jid": "g1@g.us", "group_name": "Admin name"}) == 3
    assert sample("whatsapp_messages_per_group", {"group_jid": "g2@g.us", "group_name": "Only raw"}) == 2
    assert sample("whatsapp_messages_per_group", {"group_jid": "g3@g.us", "group_name": "g3@g.us"}) == 1

    await db("""UPDATE "group" SET display_name = 'Renamed' WHERE group_jid = 'g1@g.us'""")
    await exporter.collect_database_metrics()
    assert sample("whatsapp_messages_per_group", {"group_jid": "g1@g.us", "group_name": "Admin name"}) is None
    assert sample("whatsapp_messages_per_group", {"group_jid": "g1@g.us", "group_name": "Renamed"}) == 3
