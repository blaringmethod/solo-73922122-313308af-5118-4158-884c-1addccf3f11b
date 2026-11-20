"""换表工单 + 缺测段补算子段的 SQL 层测试。

覆盖:
  * 工单登记/同表同刻更正/删除;
  * 严格边界判定 (工单时刻等于缺测段端点不参与);
  * 多工单按换表时刻逐次切开, 拆除结束上段/装机开启下段, 最外两端用采样读数;
  * 单次回绕; 无工单保留缺测;
  * 补算子段持久化;
  * 读数补录/删除/更正使工单换段或脱离缺测段时, 旧补算同事务撤下并按新段重建;
  * 回滚原子性; 改电表号级联;
  * backfill_consistency_report 巡检始终为 0 行。
"""
from __future__ import annotations

import psycopg
import pytest

from app.db import db


def _segs(conn, meter):
    return conn.execute(
        "SELECT gap_end_read_at, segment_index, start_read_at, end_read_at, "
        "start_reading, end_reading, energy_wh, "
        "start_work_order_at, end_work_order_at "
        "FROM backfill_segments WHERE meter_no=%s "
        "ORDER BY gap_end_read_at, segment_index",
        (meter,),
    ).fetchall()


def _problems(conn):
    return conn.execute("SELECT * FROM backfill_consistency_report()").fetchall()


def _wo(conn, *args):
    """调用 upsert_work_order 并取回单行。

    池化连接用 dict_row 工厂时 execute 仍返回游标; 直接用独立游标最稳妥。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM upsert_work_order(%s,%s,%s,%s)", args)
        return cur.fetchone()


def _make_gap(conn, meter="M1"):
    """造一条换表缺测段 (01:00,02:00], 旧表末读 999900, 新表首读 100。"""
    conn.execute("SELECT insert_reading(%s,'2026-01-01 00:00Z',1000,false)", (meter,))
    conn.execute("SELECT insert_reading(%s,'2026-01-01 01:00Z',999900,false)", (meter,))
    conn.execute("SELECT insert_reading(%s,'2026-01-01 02:00Z',100,true)", (meter,))
    conn.execute("SELECT insert_reading(%s,'2026-01-01 03:00Z',500,false)", (meter,))


def test_no_work_order_keeps_gap_missing(clean_db):
    with db.connection() as conn:
        _make_gap(conn)
        assert _segs(conn, "M1") == []
        gap = conn.execute(
            "SELECT energy_wh FROM reading_intervals "
            "WHERE meter_no='M1' AND end_read_at='2026-01-01 02:00Z'"
        ).fetchone()
        assert gap["energy_wh"] is None
        assert _problems(conn) == []


def test_single_work_order_splits_gap_into_two_segments(clean_db):
    with db.connection() as conn:
        _make_gap(conn)
        conn.execute(
            "SELECT o_created FROM upsert_work_order('M1','2026-01-01 01:30Z',999950,20)"
        )
        segs = _segs(conn, "M1")
        assert len(segs) == 2
        # 第一段: 采样读数 999900 -> 拆除 999950 = 50, 左端无工单
        s0, s1 = segs
        assert s0["start_read_at"].hour == 1 and s0["end_read_at"].minute == 30
        assert (s0["start_reading"], s0["end_reading"], s0["energy_wh"]) == (
            999900, 999950, 50
        )
        assert s0["start_work_order_at"] is None
        assert s0["end_work_order_at"] is not None
        # 第二段: 装机 20 -> 采样读数 100 = 80, 右端无工单
        assert (s1["start_reading"], s1["end_reading"], s1["energy_wh"]) == (20, 100, 80)
        assert s1["start_work_order_at"] is not None
        assert s1["end_work_order_at"] is None
        assert _problems(conn) == []


def test_segment_uses_single_million_wraparound(clean_db):
    with db.connection() as conn:
        _make_gap(conn)
        # 装机读数大于下一个采样读数: 100 -> 50 回绕 = 999950
        conn.execute(
            "SELECT o_created FROM upsert_work_order('M1','2026-01-01 01:30Z',999950,200)"
        )
        segs = _segs(conn, "M1")
        # 末段 (01:30,02:00]: 100 - 200 + 1_000_000 = 999900
        assert segs[1]["energy_wh"] == 999900
        assert _problems(conn) == []


def test_strict_boundary_work_orders_are_ignored(clean_db):
    with db.connection() as conn:
        _make_gap(conn)
        # 换表时刻恰好等于缺测段左/右端点 => 严格判定, 不参与补算
        conn.execute(
            "SELECT o_created FROM upsert_work_order('M1','2026-01-01 01:00Z',999950,20)"
        )
        conn.execute(
            "SELECT o_created FROM upsert_work_order('M1','2026-01-01 02:00Z',999950,20)"
        )
        assert _segs(conn, "M1") == []
        # 工单本身仍已登记
        n = conn.execute(
            "SELECT count(*) c FROM meter_work_orders WHERE meter_no='M1'"
        ).fetchone()["c"]
        assert n == 2
        assert _problems(conn) == []


def test_multiple_work_orders_split_sequentially(clean_db):
    with db.connection() as conn:
        _make_gap(conn)
        conn.execute(
            "SELECT o_created FROM upsert_work_order('M1','2026-01-01 01:20Z',999920,10)"
        )
        conn.execute(
            "SELECT o_created FROM upsert_work_order('M1','2026-01-01 01:40Z',999960,40)"
        )
        segs = _segs(conn, "M1")
        # 2 个工单 => 3 个子段, 首尾相接
        assert len(segs) == 3
        assert [s["segment_index"] for s in segs] == [0, 1, 2]
        assert segs[0]["end_read_at"] == segs[1]["start_read_at"]
        assert segs[1]["end_read_at"] == segs[2]["start_read_at"]
        # 能量: 20 / 40 / (100-40)=60; 最外两端是采样读数
        assert [s["energy_wh"] for s in segs] == [20, 999950, 60]
        assert segs[0]["start_work_order_at"] is None
        assert segs[2]["end_work_order_at"] is None
        # 中间段两端都来自工单
        assert segs[1]["start_work_order_at"] is not None
        assert segs[1]["end_work_order_at"] is not None
        assert _problems(conn) == []


def test_upsert_same_meter_same_time_is_correction(clean_db):
    with db.connection() as conn:
        _make_gap(conn)
        first = _wo(conn, "M1", "2026-01-01 01:30Z", 999950, 20)
        assert first["o_created"] is True
        second = _wo(conn, "M1", "2026-01-01 01:30Z", 999980, 60)
        assert second["o_created"] is False
        assert second["o_id"] == first["o_id"]  # 同一行被更正
        segs = _segs(conn, "M1")
        # 新值: (999900->999980)=80, (60->100)=40
        assert [s["energy_wh"] for s in segs] == [80, 40]
        n = conn.execute(
            "SELECT count(*) c FROM meter_work_orders WHERE meter_no='M1'"
        ).fetchone()["c"]
        assert n == 1
        assert _problems(conn) == []


def test_delete_work_order_rebuilds_or_restores_gap(clean_db):
    with db.connection() as conn:
        _make_gap(conn)
        wid1 = _wo(conn, "M1", "2026-01-01 01:20Z", 999920, 10)["o_id"]
        wid2 = _wo(conn, "M1", "2026-01-01 01:40Z", 999960, 40)["o_id"]
        assert len(_segs(conn, "M1")) == 3

        conn.execute("SELECT delete_work_order(%s)", (wid1,))
        segs = _segs(conn, "M1")
        assert len(segs) == 2  # 剩 1 工单 => 2 段
        assert _problems(conn) == []

        conn.execute("SELECT delete_work_order(%s)", (wid2,))
        assert _segs(conn, "M1") == []  # 无工单 => 缺测保留
        assert _problems(conn) == []


def test_delete_nonexistent_work_order_raises(clean_db):
    with db.connection() as conn:
        with pytest.raises(psycopg.Error):
            conn.execute("SELECT delete_work_order(999999)")


def test_backfill_persists_across_connections(clean_db):
    with db.connection() as conn:
        _make_gap(conn)
        conn.execute(
            "SELECT o_created FROM upsert_work_order('M1','2026-01-01 01:30Z',999950,20)"
        )
    import psycopg as pg
    from app.config import settings
    with pg.connect(settings.database_url) as fresh:
        rows = fresh.execute(
            "SELECT energy_wh FROM backfill_segments WHERE meter_no='M1' ORDER BY segment_index"
        ).fetchall()
        assert [r[0] for r in rows] == [50, 80]


def test_backfill_moves_when_backfilled_reading_splits_gap(clean_db):
    """补录读数把缺测段拆开: 工单落入新缺测段, 旧补算随旧段同事务撤下。"""
    with db.connection() as conn:
        # 大缺测段 (00:00,04:00], 工单 02:00
        conn.execute("SELECT insert_reading('M2','2026-01-01 00:00Z',1000,false)")
        conn.execute("SELECT insert_reading('M2','2026-01-01 04:00Z',2000,true)")
        conn.execute(
            "SELECT o_created FROM upsert_work_order('M2','2026-01-01 02:00Z',1400,1500)"
        )
        assert len(_segs(conn, "M2")) == 2

        # 补录 01:00 普通读数: (00,01] 正常, (01,04] 仍缺测; 补算迁移并以 01:00 采样为左端
        conn.execute("SELECT insert_reading('M2','2026-01-01 01:00Z',1200,false)")
        segs = _segs(conn, "M2")
        assert len(segs) == 2
        assert all(s["gap_end_read_at"].hour == 4 for s in segs)
        assert segs[0]["start_read_at"].hour == 1
        assert segs[0]["start_reading"] == 1200
        assert _problems(conn) == []

        # 再补录 03:00 换表首读: 缺测段变为 (01,03], 补算再迁移
        conn.execute("SELECT insert_reading('M2','2026-01-01 03:00Z',1800,true)")
        segs = _segs(conn, "M2")
        assert len(segs) == 2
        assert all(s["gap_end_read_at"].hour == 3 for s in segs)
        assert segs[1]["end_reading"] == 1800
        assert _problems(conn) == []
        # 区间表本身依旧一致
        assert conn.execute(
            "SELECT count(*) c FROM interval_consistency_report() WHERE meter_no='M2'"
        ).fetchone()["c"] == 0


def test_backfill_reverts_when_gap_merges_after_reading_delete(clean_db):
    with db.connection() as conn:
        conn.execute("SELECT insert_reading('M2','2026-01-01 00:00Z',1000,false)")
        conn.execute("SELECT insert_reading('M2','2026-01-01 04:00Z',2000,true)")
        conn.execute(
            "SELECT o_created FROM upsert_work_order('M2','2026-01-01 02:00Z',1400,1500)"
        )
        conn.execute("SELECT insert_reading('M2','2026-01-01 03:00Z',1800,true)")
        assert all(
            s["gap_end_read_at"].hour == 3 for s in _segs(conn, "M2")
        )
        rid = conn.execute(
            "SELECT id FROM meter_readings WHERE read_at='2026-01-01 03:00Z'"
        ).fetchone()["id"]
        conn.execute("SELECT delete_reading(%s)", (rid,))
        # 合并回 (00,04] 缺测段, 补算回到该段且最外左端为 00:00 采样
        segs = _segs(conn, "M2")
        assert len(segs) == 2
        assert all(s["gap_end_read_at"].hour == 4 for s in segs)
        assert segs[0]["start_read_at"].hour == 0
        assert _problems(conn) == []


def test_clearing_replaced_flag_removes_backfill_in_same_transaction(clean_db):
    with db.connection() as conn:
        _make_gap(conn)
        conn.execute(
            "SELECT o_created FROM upsert_work_order('M1','2026-01-01 01:30Z',999950,20)"
        )
        assert len(_segs(conn, "M1")) == 2
        rid = conn.execute(
            "SELECT id FROM meter_readings WHERE read_at='2026-01-01 02:00Z'"
        ).fetchone()["id"]
        # replaced true -> false: 缺测段变正常区间, 补算必须撤尽
        conn.execute(
            "SELECT update_reading(%s,'M1','2026-01-01 02:00Z',100,false)", (rid,)
        )
        assert _segs(conn, "M1") == []
        assert _problems(conn) == []


def test_rollback_restores_prior_backfill_state(clean_db):
    with db.connection() as conn:
        _make_gap(conn)
        conn.execute(
            "SELECT o_created FROM upsert_work_order('M1','2026-01-01 01:30Z',999950,20)"
        )
    try:
        with db.connection() as conn:
            conn.execute(
                "SELECT o_created FROM upsert_work_order('M1','2026-01-01 01:30Z',999980,60)"
            )
            assert len(_segs(conn, "M1")) == 2
            raise RuntimeError("force rollback")
    except RuntimeError:
        pass
    with db.connection() as conn:
        # 回到更正前: 50 / 80
        assert [s["energy_wh"] for s in _segs(conn, "M1")] == [50, 80]
        wo = conn.execute(
            "SELECT removal_reading, install_reading FROM meter_work_orders "
            "WHERE meter_no='M1'"
        ).fetchone()
        assert (wo["removal_reading"], wo["install_reading"]) == (999950, 20)
        assert _problems(conn) == []


def test_meter_rename_cascades_to_work_orders_and_backfill(clean_db):
    """SQL 直改电表号 (三表同事务): 工单与补算随级联迁移, 不残留旧表行。"""
    with db.connection() as conn:
        _make_gap(conn, "MOLD")
        conn.execute(
            "SELECT o_created FROM upsert_work_order('MOLD','2026-01-01 01:30Z',999950,20)"
        )
        conn.execute("SELECT lock_meters('MOLD','MNEW')")
        conn.execute("UPDATE meter_work_orders SET meter_no='MNEW' WHERE meter_no='MOLD'")
        conn.execute("UPDATE reading_intervals SET meter_no='MNEW' WHERE meter_no='MOLD'")
        conn.execute("UPDATE meter_readings SET meter_no='MNEW' WHERE meter_no='MOLD'")
    with db.connection() as conn:
        assert _segs(conn, "MOLD") == []
        assert len(_segs(conn, "MNEW")) == 2
        assert conn.execute(
            "SELECT count(*) c FROM meter_work_orders WHERE meter_no='MNEW'"
        ).fetchone()["c"] == 1
        assert _problems(conn) == []


def test_only_affected_gaps_rebuilt_other_gap_untouched(clean_db):
    """一只电表两条独立缺测段: 工单只影响严格包含它的那一段。"""
    with db.connection() as conn:
        # 缺测段 1: (01,02]; 正常段 (02,03]; 缺测段 2: (03,04]
        conn.execute("SELECT insert_reading('M3','2026-01-01 01:00Z',100,false)")
        conn.execute("SELECT insert_reading('M3','2026-01-01 02:00Z',200,true)")
        conn.execute("SELECT insert_reading('M3','2026-01-01 03:00Z',300,false)")
        conn.execute("SELECT insert_reading('M3','2026-01-01 04:00Z',400,true)")
        conn.execute("SELECT insert_reading('M3','2026-01-01 05:00Z',500,false)")
        # 工单只在第一段内
        conn.execute(
            "SELECT o_created FROM upsert_work_order('M3','2026-01-01 01:30Z',150,120)"
        )
        segs = _segs(conn, "M3")
        assert len(segs) == 2
        assert all(s["gap_end_read_at"].hour == 2 for s in segs)
        # 第二段缺测依旧无补算
        assert _problems(conn) == []
