from prometheus_client import REGISTRY

import app as exporter


def sample(name, labels=None):
    return REGISTRY.get_sample_value(name, labels or {})


async def test_media_count_and_type_breakdown(db):
    rows = [
        ("m1", "[[Attached Image]] a caption", None),
        ("m2", "[[Attached Image]]", None),
        ("m3", "[[Attached Video]] clip", None),
        ("m4", "[[Attached Document]] file.pdf", None),
        ("m5", "plain text", None),
        ("m6", None, None),
        ("m7", "legacy row", "/media/old.jpg"),  # gowa-era media_url, no prefix
    ]
    for mid, txt, url in rows:
        await db(
            "INSERT INTO message (message_id, timestamp, text, media_url) "
            "VALUES (:i, now(), :t, :u)",
            i=mid, t=txt, u=url,
        )
    await exporter.collect_database_metrics()
    assert sample("whatsapp_messages_with_media_total") == 5
    assert sample("whatsapp_messages_by_type", {"message_type": "image"}) == 2
    assert sample("whatsapp_messages_by_type", {"message_type": "video"}) == 1
    assert sample("whatsapp_messages_by_type", {"message_type": "document"}) == 1
    assert sample("whatsapp_messages_by_type", {"message_type": "text"}) == 3


async def test_by_type_is_cleared_each_scrape(db):
    await db(
        "INSERT INTO message (message_id, timestamp, text) "
        "VALUES ('m1', now(), '[[Attached Sticker]]')"
    )
    await exporter.collect_database_metrics()
    assert sample("whatsapp_messages_by_type", {"message_type": "sticker"}) == 1
    await db("DELETE FROM message")
    await exporter.collect_database_metrics()
    assert sample("whatsapp_messages_by_type", {"message_type": "sticker"}) is None
