"""API 请求 / 响应模型。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .ir import MAX_ROWS


class CheckRequest(BaseModel):
    query_a: str = Field(..., description="原始 SELECT（当前在用的质控 SQL）")
    query_b: str = Field(..., description="改写后的 SELECT")
    solver_timeout_ms: int | None = Field(
        None,
        ge=100,
        le=120000,
        description="z3 求解超时（毫秒）；缺省取环境变量 SOLVER_TIMEOUT_MS 或 10000",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "query_a": (
                        "SELECT s.id, r.value FROM stations s "
                        "INNER JOIN readings r ON s.id = r.station_id "
                        "WHERE r.value > 0"
                    ),
                    "query_b": (
                        "SELECT s.id, r.value FROM stations s "
                        "INNER JOIN readings r ON s.id = r.station_id "
                        "WHERE r.value >= 1"
                    ),
                }
            ]
        }
    }


class Counterexample(BaseModel):
    setup_sql: str = Field(..., description="可装载的建表与数据 SQL 脚本")
    query_a_result: list[list[Any]] = Field(..., description="反例数据下 query_a 的实际结果")
    query_b_result: list[list[Any]] = Field(..., description="反例数据下 query_b 的实际结果")


class CheckResponse(BaseModel):
    verdict: Literal["EQUIVALENT", "NOT_EQUIVALENT", "UNKNOWN"] = Field(
        ...,
        description=(
            "EQUIVALENT=界内无反例；NOT_EQUIVALENT=差异已在隔离 PostgreSQL 复现；"
            "UNKNOWN=求解或复现失败"
        ),
    )
    bound_rows: int = Field(
        MAX_ROWS, description="等价结论成立的每表行数上界（含 0 行）"
    )
    message: str = Field(..., description="判定说明")
    counterexample: Counterexample | None = Field(
        None, description="verdict=NOT_EQUIVALENT 时给出可复现的反例"
    )


# ---------- 测试样本 API ----------

TruthValue = Literal["TRUE", "FALSE", "UNKNOWN"]


class SamplesRequest(BaseModel):
    query: str = Field(
        ...,
        description=(
            "待分析的质控 SELECT，限 stations LEFT JOIN readings "
            "ON stations.id = readings.station_id（表可加别名）"
        ),
    )
    solver_timeout_ms: int | None = Field(
        None,
        ge=100,
        le=120000,
        description=(
            "单个条件的 z3 求解超时（毫秒）；缺省取环境变量 SOLVER_TIMEOUT_MS "
            "或 10000。每个目标条件独立求解一次"
        ),
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "query": (
                        "SELECT s.id, r.value FROM stations s "
                        "LEFT JOIN readings r ON s.id = r.station_id "
                        "WHERE r.value >= 10 AND s.threshold IS NOT NULL"
                    )
                }
            ]
        }
    }


class ChangedReading(BaseModel):
    station_id: int = Field(..., description="被修改读数所属站点（已匹配）")
    value_a: int | None = Field(..., description="快照 A 中该读数的 value")
    value_b: int | None = Field(..., description="快照 B 中该读数的 value")


class SnapshotPair(BaseModel):
    setup_sql_a: str = Field(..., description="快照 A 的可装载建表与数据 SQL")
    setup_sql_b: str = Field(..., description="快照 B 的可装载建表与数据 SQL")
    changed_reading: ChangedReading = Field(
        ..., description="两份快照唯一的差异：该已匹配读数的 value"
    )
    target_truth_a: TruthValue = Field(..., description="快照 A 连接行上目标条件的三值结果")
    target_truth_b: TruthValue = Field(..., description="快照 B 连接行上目标条件的三值结果")
    where_truth_a: TruthValue = Field(..., description="快照 A 连接行上整体 WHERE 的三值结果")
    where_truth_b: TruthValue = Field(..., description="快照 B 连接行上整体 WHERE 的三值结果")
    query_result_a: list[list[Any]] = Field(..., description="快照 A 上被测查询的实际结果")
    query_result_b: list[list[Any]] = Field(..., description="快照 B 上被测查询的实际结果")


class ConditionCoverage(BaseModel):
    index: int = Field(..., description="原子条件在 WHERE 中的书写顺序号（从 0 起）")
    condition: str = Field(..., description="目标条件的 SQL 片段（表名限定）")
    status: Literal["COVERED", "UNCOVERABLE", "UNKNOWN"] = Field(
        ...,
        description=(
            "COVERED=已找到快照对并在隔离 PostgreSQL 复核通过；"
            "UNCOVERABLE=界内不存在仅改变该条件的快照对（无法单独覆盖）；"
            "UNKNOWN=求解或复核无法确认"
        ),
    )
    message: str = Field(..., description="结论说明")
    pair: SnapshotPair | None = Field(
        None, description="status=COVERED 时给出复核通过的快照对"
    )


class SamplesResponse(BaseModel):
    bound_rows: int = Field(
        MAX_ROWS, description="样本搜索的每表行数上界（含 0 行）"
    )
    message: str = Field(..., description="总体结论说明")
    conditions: list[ConditionCoverage] = Field(
        ..., description="按书写顺序排列的各 readings.value 条件的覆盖结论"
    )
