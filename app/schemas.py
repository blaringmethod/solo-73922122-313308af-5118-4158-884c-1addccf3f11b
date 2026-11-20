"""Pydantic 请求/响应模型。"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator

_METER_NO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-/]{0,63}$")


def _validate_meter_no(v: str) -> str:
    v = v.strip()
    if not _METER_NO_RE.match(v):
        raise ValueError(
            "meter_no 须为 1-64 位字母/数字及 _.-/ 且以字母或数字开头"
        )
    return v


def _to_utc(v: datetime) -> datetime:
    """统一转 UTC 存储; 不接受无时区时间, 避免时刻歧义。"""
    if v.tzinfo is None:
        raise ValueError("read_at 必须带时区偏移 (建议直接用 UTC, 如 2026-01-01T00:00:00Z)")
    return v.astimezone(timezone.utc)


class ReadingCreate(BaseModel):
    meter_no: str = Field(..., examples=["M-001"])
    read_at: datetime = Field(..., examples=["2026-01-01T00:00:00Z"])
    reading: int = Field(..., ge=0, le=999999, description="累计读数, 整数 Wh, 0~999999")
    replaced: bool = Field(False, description="换表标记: 该读数为新表首读")

    _v_meter = field_validator("meter_no")(_validate_meter_no)
    _v_time = field_validator("read_at")(_to_utc)


class ReadingUpdate(BaseModel):
    meter_no: str = Field(..., examples=["M-001"])
    read_at: datetime = Field(..., examples=["2026-01-01T00:00:00Z"])
    reading: int = Field(..., ge=0, le=999999)
    replaced: bool = False

    _v_meter = field_validator("meter_no")(_validate_meter_no)
    _v_time = field_validator("read_at")(_to_utc)


class ReadingOut(BaseModel):
    id: int
    meter_no: str
    read_at: datetime
    reading: int
    replaced: bool


class IntervalOut(BaseModel):
    meter_no: str
    start_read_at: datetime
    end_read_at: datetime
    start_reading: int | None
    end_reading: int
    energy_wh: int | None = Field(None, description="区间电量 Wh; null=换表缺测")


class WorkOrderCreate(BaseModel):
    meter_no: str = Field(..., examples=["M-001"])
    replaced_at: datetime = Field(
        ..., examples=["2026-01-01T01:00:00Z"], description="UTC 换表时刻"
    )
    removal_reading: int = Field(
        ..., ge=0, le=999999, description="拆除读数 (旧表末读), 整数 Wh, 0~999999"
    )
    install_reading: int = Field(
        ..., ge=0, le=999999, description="装机读数 (新表首读), 整数 Wh, 0~999999"
    )

    _v_meter = field_validator("meter_no")(_validate_meter_no)
    _v_time = field_validator("replaced_at")(_to_utc)


class WorkOrderOut(BaseModel):
    id: int
    meter_no: str
    replaced_at: datetime
    removal_reading: int
    install_reading: int
    created_at: datetime
    updated_at: datetime


class WorkOrderUpsertOut(WorkOrderOut):
    created: bool = Field(
        ..., description="true=新登记工单; false=同表同刻重复提交, 已更正"
    )


class BackfillSegmentOut(BaseModel):
    """换表缺测段的持久化补算子段。

    端点来自原采样读数时, 对应的 work_order_at 为 null;
    来自工单拆除/装机读数时, 为该工单的换表时刻。
    """

    meter_no: str
    gap_end_read_at: datetime = Field(..., description="所属原始缺测段的右端时刻")
    segment_index: int
    start_read_at: datetime
    end_read_at: datetime
    start_reading: int
    end_reading: int
    energy_wh: int
    start_work_order_at: datetime | None = Field(
        None, description="左端工单时刻; null=左端取自原采样读数"
    )
    end_work_order_at: datetime | None = Field(
        None, description="右端工单时刻; null=右端取自原采样读数"
    )


class SupplyResponse(BaseModel):
    """同一完整提交快照下的读数、原始缺测与补算子段供数结果。"""

    meter_no: str
    snapshot_time: datetime = Field(..., description="本次供数使用的事务快照时间 (UTC)")
    readings: list[ReadingOut]
    intervals: list[IntervalOut] = Field(
        ..., description="全部区间; 其中 energy_wh=null 即原始换表缺测段"
    )
    missing_intervals: list[IntervalOut] = Field(
        ..., description="原始缺测值 (energy_wh=null 的换表缺测段), 与补算子段分开返回"
    )
    backfill_segments: list[BackfillSegmentOut] = Field(
        ..., description="持久化补算子段; 无适用工单的缺测段在此不出现"
    )
    work_orders: list[WorkOrderOut]
