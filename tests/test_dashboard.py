import json
from pathlib import Path

DASHBOARD = json.loads((Path(__file__).parent.parent / "dashboard.json").read_text())


def test_panel_ids_unique_and_uid_kept():
    ids = [p["id"] for p in DASHBOARD["panels"]]
    assert len(ids) == len(set(ids))
    assert set(range(1, 16)) <= set(ids)
    assert DASHBOARD["uid"] == "whatsapp-llm-dashboard"


def test_all_targets_use_datasource_variable():
    for panel in DASHBOARD["panels"]:
        assert panel["datasource"]["uid"] == "${datasource}"
        for target in panel["targets"]:
            assert target["datasource"]["uid"] == "${datasource}"


def test_new_panels_present():
    exprs = {t["expr"] for p in DASHBOARD["panels"] for t in p["targets"]}
    assert "whatsapp_session_status == 1" in exprs
    assert "whatsapp_senders_by_kind" in exprs
