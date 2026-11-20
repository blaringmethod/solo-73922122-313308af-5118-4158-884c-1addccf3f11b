"""工单登记 API 与 /supply 分开返回原始缺测/补算子段的 API 层测试。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _reading(meter, ts, reading, replaced=False):
    return client.post(
        "/readings",
        json={
            "meter_no": meter,
            "read_at": ts,
            "reading": reading,
            "replaced": replaced,
        },
    )


def _gap(meter="MA"):
    _reading(meter, "2026-01-01T00:00:00Z", 1000)
    _reading(meter, "2026-01-01T01:00:00Z", 999900)
    _reading(meter, "2026-01-01T02:00:00Z", 100, replaced=True)
    _reading(meter, "2026-01-01T03:00:00Z", 500)


def _work_order(meter, ts, removal, install):
    return client.post(
        "/work-orders",
        json={
            "meter_no": meter,
            "replaced_at": ts,
            "removal_reading": removal,
            "install_reading": install,
        },
    )


def test_work_order_register_and_correct_status_codes(clean_db):
    _gap()
    r1 = _work_order("MA", "2026-01-01T01:30:00Z", 999950, 20)
    assert r1.status_code == 201, r1.text
    body1 = r1.json()
    assert body1["created"] is True
    assert body1["removal_reading"] == 999950 and body1["install_reading"] == 20

    # 同表同刻再次提交 => 更正, 200 + created=false, 仍是同一 id
    r2 = _work_order("MA", "2026-01-01T01:30:00Z", 999980, 60)
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["created"] is False
    assert body2["id"] == body1["id"]
    assert body2["removal_reading"] == 999980

    listed = client.get("/work-orders", params={"meter_no": "MA"}).json()
    assert len(listed) == 1 and listed[0]["id"] == body1["id"]


def test_work_order_validation_errors(clean_db):
    _gap()
    # 读数超界
    r = _work_order("MA", "2026-01-01T01:30:00Z", 1_000_000, 20)
    assert r.status_code == 422
    r = _work_order("MA", "2026-01-01T01:30:00Z", 100, -1)
    assert r.status_code == 422
    # 无时区时刻
    r = client.post(
        "/work-orders",
        json={
            "meter_no": "MA",
            "replaced_at": "2026-01-01T01:30:00",
            "removal_reading": 100,
            "install_reading": 20,
        },
    )
    assert r.status_code == 422


def test_supply_splits_missing_and_backfill(clean_db):
    _gap()
    # 无工单: 原始缺测存在, 补算子段为空
    s0 = client.get("/supply", params={"meter_no": "MA"}).json()
    assert len(s0["missing_intervals"]) == 1
    assert s0["missing_intervals"][0]["energy_wh"] is None
    assert s0["backfill_segments"] == []
    assert s0["work_orders"] == []

    _work_order("MA", "2026-01-01T01:30:00Z", 999950, 20)
    s = client.get("/supply", params={"meter_no": "MA"}).json()

    # 原始缺测值仍保留 (区间 energy_wh=null), 与补算分开返回
    assert len(s["missing_intervals"]) == 1
    assert s["missing_intervals"][0]["energy_wh"] is None
    null_intervals = [i for i in s["intervals"] if i["energy_wh"] is None]
    assert len(null_intervals) == 1

    # 补算子段: 两段, 含起止/Wh/所用工单时刻
    segs = s["backfill_segments"]
    assert len(segs) == 2
    assert [x["energy_wh"] for x in segs] == [50, 80]
    assert segs[0]["start_work_order_at"] is None
    assert segs[0]["end_work_order_at"] is not None
    assert segs[1]["start_work_order_at"] is not None
    assert segs[1]["end_work_order_at"] is None
    for x in segs:
        assert x["start_read_at"] and x["end_read_at"] and x["gap_end_read_at"]

    assert len(s["work_orders"]) == 1
    assert s["work_orders"][0]["removal_reading"] == 999950
    # 同一快照字段齐全
    assert s["snapshot_time"] and s["readings"]


def test_supply_time_window_filters_backfill(clean_db):
    _gap()
    _work_order("MA", "2026-01-01T01:30:00Z", 999950, 20)
    # 窗口在缺测段之前: 补算子段 (右端 01:30/02:00) 全部被排除
    s = client.get(
        "/supply",
        params={"meter_no": "MA", "start": "2026-01-01T02:30:00Z"},
    ).json()
    assert s["backfill_segments"] == []
    assert s["missing_intervals"] == []


def test_backfill_updates_when_reading_changes_via_api(clean_db):
    _gap()
    _work_order("MA", "2026-01-01T01:30:00Z", 999950, 20)
    # 补录 00:30 普通读数: 缺测段身份不变 (仍为 (01,02]), 补算不受影响
    _reading("MA", "2026-01-01T00:30:00Z", 500)
    s = client.get("/supply", params={"meter_no": "MA"}).json()
    assert [x["energy_wh"] for x in s["backfill_segments"]] == [50, 80]

    # 更正 02:00 读数去掉换表标记: 缺测段变正常, 补算同事务撤下
    target = next(
        r for r in client.get("/readings", params={"meter_no": "MA"}).json()
        if r["read_at"].startswith("2026-01-01T02:00:00")
    )
    r = client.put(
        f"/readings/{target['id']}",
        json={
            "meter_no": "MA",
            "read_at": "2026-01-01T02:00:00Z",
            "reading": 100,
            "replaced": False,
        },
    )
    assert r.status_code == 200, r.text
    s = client.get("/supply", params={"meter_no": "MA"}).json()
    assert s["missing_intervals"] == []
    assert s["backfill_segments"] == []
    # 工单仍登记在案 (只是脱离了缺测段)
    assert len(s["work_orders"]) == 1


def test_delete_work_order_via_api_restores_missing(clean_db):
    _gap()
    _work_order("MA", "2026-01-01T01:30:00Z", 999950, 20)
    wid = client.get("/work-orders", params={"meter_no": "MA"}).json()[0]["id"]
    r = client.delete(f"/work-orders/{wid}")
    assert r.status_code == 200, r.text
    s = client.get("/supply", params={"meter_no": "MA"}).json()
    assert s["backfill_segments"] == []
    assert len(s["missing_intervals"]) == 1

    assert client.delete("/work-orders/999999").status_code == 404


def test_openapi_documents_new_endpoints(clean_db):
    spec = client.get("/openapi.json").json()
    assert "/work-orders" in spec["paths"]
    supply_schema = spec["components"]["schemas"]["SupplyResponse"]["properties"]
    assert "missing_intervals" in supply_schema
    assert "backfill_segments" in supply_schema
    assert "work_orders" in supply_schema
