"""FastAPI 入口: 读数 CRUD + 同快照供数。

所有写操作都在单事务内调用数据库侧的增量维护函数, 任何步骤失败整体回滚。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query
from psycopg.rows import dict_row

from .db import db
from .errors import raise_http_for_db_error
from .schemas import (
    IntervalOut,
    ReadingCreate,
    ReadingOut,
    ReadingUpdate,
    SupplyResponse,
    WorkOrderCreate,
    WorkOrderOut,
    WorkOrderUpsertOut,
)

app = FastAPI(
    title="电表累计读数 -> 区间用电量 API",
    version="1.1.0",
    description=(
        "累计读数持久化与左开右闭区间电量增量维护。\n\n"
        "- 区间 `(前驱时刻, 当前时刻]`, 首条读数无区间\n"
        "- 当前读数 `replaced=true` 时区间电量缺测 (null)\n"
        "- 负差值按 1,000,000 单次回绕\n"
        "- 补录/更正/删除增量修正区间; 改电表号同时修正两只电表\n"
        "- 换表工单 (`/work-orders`) 登记拆除/装机读数, 同表同刻重提即更正\n"
        "- 仅换表时刻严格落在缺测段两端之间的工单参与补算, 按时刻逐次切开\n"
        "- 补算子段持久化, 读数/工单变化只更新受影响段, 旧补算同事务撤下\n"
        "- 同表并发串行、异表并发互不阻塞 (READ COMMITTED + 电表行锁)\n"
        "- `/supply` 在 REPEATABLE READ 单快照内分开返回原始缺测与补算子段"
    ),
)


@app.on_event("startup")
def _startup() -> None:
    db.connect()


@app.on_event("shutdown")
def _shutdown() -> None:
    db.close()


@app.get("/health")
def health() -> dict[str, str]:
    with db.connection() as conn:
        conn.execute("SELECT 1").fetchone()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 写入: 新增 / 更正 / 删除 —— 事务内调用 SQL 函数, 失败整体回滚
# ---------------------------------------------------------------------------
@app.post("/readings", response_model=ReadingOut, status_code=201, tags=["readings"])
def create_reading(payload: ReadingCreate) -> dict[str, Any]:
    try:
        with db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM insert_reading(%s, %s, %s, %s)",
                (payload.meter_no, payload.read_at, payload.reading, payload.replaced),
            ).fetchone()
        return row
    except psycopg.Error as exc:
        raise_http_for_db_error(exc)


@app.put("/readings/{reading_id}", response_model=ReadingOut, tags=["readings"])
def update_reading(reading_id: int, payload: ReadingUpdate) -> dict[str, Any]:
    try:
        with db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM update_reading(%s, %s, %s, %s, %s)",
                (
                    reading_id,
                    payload.meter_no,
                    payload.read_at,
                    payload.reading,
                    payload.replaced,
                ),
            ).fetchone()
        return row
    except psycopg.Error as exc:
        raise_http_for_db_error(exc)


@app.delete("/readings/{reading_id}", response_model=ReadingOut, tags=["readings"])
def delete_reading(reading_id: int) -> dict[str, Any]:
    try:
        with db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM delete_reading(%s)", (reading_id,)
            ).fetchone()
        return row
    except psycopg.Error as exc:
        raise_http_for_db_error(exc)


# ---------------------------------------------------------------------------
# 换表工单: 登记/更正 (同表同刻重提即更正)、查询、删除
# 与读数写入共用同一套电表行锁, 补算在同一事务内增量维护
# ---------------------------------------------------------------------------
@app.post(
    "/work-orders",
    response_model=WorkOrderUpsertOut,
    status_code=201,
    tags=["work-orders"],
)
def upsert_work_order(payload: WorkOrderCreate) -> dict[str, Any]:
    try:
        with db.connection() as conn:
            row = conn.execute(
                "SELECT o_id AS id, o_meter_no AS meter_no, "
                "o_replaced_at AS replaced_at, "
                "o_removal_reading AS removal_reading, "
                "o_install_reading AS install_reading, "
                "o_created_at AS created_at, o_updated_at AS updated_at, "
                "o_created AS created "
                "FROM upsert_work_order(%s, %s, %s::INTEGER, %s::INTEGER)",
                (
                    payload.meter_no,
                    payload.replaced_at,
                    payload.removal_reading,
                    payload.install_reading,
                ),
            ).fetchone()
            # 更正已有工单返回 200, 新登记返回 201 (事务提交后才发响应)
            status_code = 201 if row["created"] else 200
        from fastapi.encoders import jsonable_encoder
        from starlette.responses import JSONResponse

        return JSONResponse(
            content=jsonable_encoder(
                WorkOrderUpsertOut(**row).model_dump(mode="json")
            ),
            status_code=status_code,
        )
    except psycopg.Error as exc:
        raise_http_for_db_error(exc)


@app.get("/work-orders", response_model=list[WorkOrderOut], tags=["work-orders"])
def list_work_orders(
    meter_no: str = Query(..., min_length=1, max_length=64),
    start: datetime | None = Query(None, description="换表时刻起始(含), 须带时区"),
    end: datetime | None = Query(None, description="换表时刻结束(含), 须带时区"),
    limit: int = Query(1000, ge=1, le=10000),
) -> list[dict[str, Any]]:
    sql = "SELECT id, meter_no, replaced_at, removal_reading, install_reading, created_at, updated_at " \
          "FROM meter_work_orders WHERE meter_no = %(meter_no)s"
    params: dict[str, Any] = {"meter_no": meter_no, "limit": limit}
    if start is not None:
        if start.tzinfo is None:
            raise HTTPException(422, "start 必须带时区偏移")
        sql += " AND replaced_at >= %(start)s"
        params["start"] = start.astimezone(timezone.utc)
    if end is not None:
        if end.tzinfo is None:
            raise HTTPException(422, "end 必须带时区偏移")
        sql += " AND replaced_at <= %(end)s"
        params["end"] = end.astimezone(timezone.utc)
    sql += " ORDER BY replaced_at ASC LIMIT %(limit)s"
    with db.snapshot_connection() as conn:
        return conn.execute(sql, params).fetchall()


@app.delete(
    "/work-orders/{work_order_id}",
    response_model=WorkOrderOut,
    tags=["work-orders"],
)
def delete_work_order(work_order_id: int) -> dict[str, Any]:
    try:
        with db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM delete_work_order(%s)", (work_order_id,)
            ).fetchone()
        return row
    except psycopg.Error as exc:
        raise_http_for_db_error(exc)


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
@app.get("/readings", response_model=list[ReadingOut], tags=["readings"])
def list_readings(
    meter_no: str = Query(..., min_length=1, max_length=64),
    start: datetime | None = Query(None, description="起始时刻(含), 须带时区"),
    end: datetime | None = Query(None, description="结束时刻(含), 须带时区"),
    limit: int = Query(1000, ge=1, le=10000),
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM meter_readings WHERE meter_no = %(meter_no)s"
    params: dict[str, Any] = {"meter_no": meter_no, "limit": limit}
    if start is not None:
        if start.tzinfo is None:
            raise HTTPException(422, "start 必须带时区偏移")
        sql += " AND read_at >= %(start)s"
        params["start"] = start.astimezone(timezone.utc)
    if end is not None:
        if end.tzinfo is None:
            raise HTTPException(422, "end 必须带时区偏移")
        sql += " AND read_at <= %(end)s"
        params["end"] = end.astimezone(timezone.utc)
    sql += " ORDER BY read_at ASC LIMIT %(limit)s"
    with db.snapshot_connection() as conn:
        return conn.execute(sql, params).fetchall()


@app.get("/intervals", response_model=list[IntervalOut], tags=["intervals"])
def list_intervals(
    meter_no: str = Query(..., min_length=1, max_length=64),
    start: datetime | None = Query(None, description="按区间右端点过滤, 含"),
    end: datetime | None = Query(None, description="按区间右端点过滤, 含"),
    limit: int = Query(1000, ge=1, le=10000),
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM reading_intervals WHERE meter_no = %(meter_no)s"
    params: dict[str, Any] = {"meter_no": meter_no, "limit": limit}
    if start is not None:
        if start.tzinfo is None:
            raise HTTPException(422, "start 必须带时区偏移")
        sql += " AND end_read_at >= %(start)s"
        params["start"] = start.astimezone(timezone.utc)
    if end is not None:
        if end.tzinfo is None:
            raise HTTPException(422, "end 必须带时区偏移")
        sql += " AND end_read_at <= %(end)s"
        params["end"] = end.astimezone(timezone.utc)
    sql += " ORDER BY end_read_at ASC LIMIT %(limit)s"
    with db.snapshot_connection() as conn:
        return conn.execute(sql, params).fetchall()


@app.get("/supply", response_model=SupplyResponse, tags=["supply"])
def supply(
    meter_no: str = Query(..., min_length=1, max_length=64),
    start: datetime | None = Query(None, description="读数/区间起始时刻(含), 须带时区"),
    end: datetime | None = Query(None, description="读数/区间结束时刻(含), 须带时区"),
    limit: int = Query(1000, ge=1, le=10000),
) -> dict[str, Any]:
    """供数: 原始缺测与补算子段在同一个 REPEATABLE READ 事务快照内分开返回。

    - `intervals`: 全部区间, `energy_wh=null` 即原始换表缺测段;
    - `missing_intervals`: 仅原始缺测段 (缺测值), 与补算子段分开;
    - `backfill_segments`: 持久化补算子段; 无适用工单的缺测段不出现;
    - `work_orders`: 本表登记的换表工单 (补算溯源)。
    四类数据与读数取自同一完整提交快照, 并发或回滚下状态一致。
    """
    if (start is not None and start.tzinfo is None) or (
        end is not None and end.tzinfo is None
    ):
        raise HTTPException(422, "start/end 必须带时区偏移")

    params: dict[str, Any] = {"meter_no": meter_no, "limit": limit}

    r_sql = "SELECT * FROM meter_readings WHERE meter_no = %(meter_no)s"
    i_sql = "SELECT * FROM reading_intervals WHERE meter_no = %(meter_no)s"
    b_sql = (
        "SELECT * FROM backfill_segments WHERE meter_no = %(meter_no)s"
    )
    w_sql = (
        "SELECT id, meter_no, replaced_at, removal_reading, install_reading, "
        "created_at, updated_at FROM meter_work_orders WHERE meter_no = %(meter_no)s"
    )
    if start is not None:
        u = start.astimezone(timezone.utc)
        r_sql += " AND read_at >= %(start)s"
        i_sql += " AND end_read_at >= %(start)s"
        # 补算子段/工单按各自的业务时刻过滤
        b_sql += " AND end_read_at >= %(start)s"
        w_sql += " AND replaced_at >= %(start)s"
        params["start"] = u
    if end is not None:
        u = end.astimezone(timezone.utc)
        r_sql += " AND read_at <= %(end)s"
        i_sql += " AND end_read_at <= %(end)s"
        b_sql += " AND end_read_at <= %(end)s"
        w_sql += " AND replaced_at <= %(end)s"
        params["end"] = u
    r_sql += " ORDER BY read_at ASC LIMIT %(limit)s"
    i_sql += " ORDER BY end_read_at ASC LIMIT %(limit)s"
    b_sql += " ORDER BY gap_end_read_at ASC, segment_index ASC LIMIT %(limit)s"
    w_sql += " ORDER BY replaced_at ASC LIMIT %(limit)s"

    with db.snapshot_connection() as conn:
        # 同一事务、同一快照; 各语句之间其他会话的提交不会影响本视图
        snapshot_time = conn.execute("SELECT now() AS now").fetchone()["now"]
        readings = conn.execute(r_sql, params).fetchall()
        intervals = conn.execute(i_sql, params).fetchall()
        backfill_segments = conn.execute(b_sql, params).fetchall()
        work_orders = conn.execute(w_sql, params).fetchall()

    # 原始缺测值与补算子段分开返回
    missing_intervals = [i for i in intervals if i["energy_wh"] is None]

    return {
        "meter_no": meter_no,
        "snapshot_time": snapshot_time,
        "readings": readings,
        "intervals": intervals,
        "missing_intervals": missing_intervals,
        "backfill_segments": backfill_segments,
        "work_orders": work_orders,
    }


@app.get("/meters", tags=["meters"])
def list_meters() -> list[dict[str, Any]]:
    with db.connection() as conn:
        return conn.execute(
            "SELECT m.meter_no, m.created_at, "
            "(SELECT count(*) FROM meter_readings r WHERE r.meter_no = m.meter_no) AS reading_count "
            "FROM meters m ORDER BY m.meter_no"
        ).fetchall()
