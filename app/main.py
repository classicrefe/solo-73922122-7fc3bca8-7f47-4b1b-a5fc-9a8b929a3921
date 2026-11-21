"""FastAPI 入口：SQL 改写有界等价核对 + 读数条件测试样本 API。"""
from __future__ import annotations

import os

from fastapi import Body, FastAPI, HTTPException

from .conditions import (
    diagnostic_sql,
    is_target_condition,
    iter_atoms,
    render_condition,
)
from .ir import MAX_ROWS, ColRef, QueryIR
from .models import (
    ChangedReading,
    CheckRequest,
    CheckResponse,
    ConditionCoverage,
    Counterexample,
    SamplesRequest,
    SamplesResponse,
    SnapshotPair,
)
from .sql_parser import SqlRejected, parse_query
from .verifier import Verifier, setup_sql
from .z3check import check_bounded_equivalence
from .z3sample import find_coverage_sample

DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5432/postgres"

app = FastAPI(
    title="SQL 改写有界等价核对 API",
    description=(
        "面向数仓质控 SQL 改写的等价性核对：提交原始 SELECT 与改写 SELECT，"
        "在固定模式 stations(id 主键, threshold) / readings(station_id 外键, value)"
        "（全列 integer，非主键可 NULL）下，枚举每表 0~3 行的所有合法取值，"
        "按列位置比较结果多重集（忽略顺序、保留重复）。"
        "差异会在隔离 PostgreSQL 中实际复现后才报 NOT_EQUIVALENT；"
        "界内无反例报 EQUIVALENT；求解或复现失败报 UNKNOWN。"
        "另提供 /samples：针对单条 stations LEFT JOIN readings 的质控 SELECT，"
        "为 WHERE 中每个 readings.value 条件寻找仅改变该条件三值结果的"
        "快照对（测试样本），并在隔离 PostgreSQL 复核后返回可装载 SQL。"
    ),
    version="1.1.0",
)

_QUERY_EXAMPLES = {
    "equivalent-integer-rewrite": {
        "summary": "界内等价：整数域上 > 0 与 >= 1 同义",
        "value": {
            "query_a": "SELECT id FROM stations WHERE threshold > 0",
            "query_b": "SELECT id FROM stations WHERE threshold >= 1",
        },
    },
    "not-equivalent-null-semantics": {
        "summary": "不等价：OR IS NULL 改变了 NULL 行的去留",
        "value": {
            "query_a": "SELECT id FROM stations WHERE threshold = 5",
            "query_b": "SELECT id FROM stations WHERE threshold = 5 OR threshold IS NULL",
        },
    },
    "not-equivalent-left-vs-inner-join": {
        "summary": "不等价：LEFT JOIN 与 INNER JOIN（无读数站点）",
        "value": {
            "query_a": (
                "SELECT s.id FROM stations s "
                "LEFT JOIN readings r ON s.id = r.station_id"
            ),
            "query_b": (
                "SELECT s.id FROM stations s "
                "INNER JOIN readings r ON s.id = r.station_id"
            ),
        },
    },
    "equivalent-left-join-counting": {
        "summary": "界内等价：连接后过滤的两种写法",
        "value": {
            "query_a": (
                "SELECT s.id, r.value FROM stations s "
                "INNER JOIN readings r ON s.id = r.station_id "
                "WHERE r.value >= 10 AND s.threshold IS NOT NULL"
            ),
            "query_b": (
                "SELECT s.id, r.value FROM stations s "
                "INNER JOIN readings r ON s.id = r.station_id "
                "WHERE NOT (r.value < 10) AND NOT (s.threshold IS NULL)"
            ),
        },
    },
}


_SAMPLE_EXAMPLES = {
    "covered-comparison-and-null-check": {
        "summary": "比较可单独覆盖，判空条件界内无法单独覆盖",
        "value": {
            "query": (
                "SELECT s.id, r.value FROM stations s "
                "LEFT JOIN readings r ON s.id = r.station_id "
                "WHERE r.value > 5 OR r.value IS NULL"
            )
        },
    },
    "mixed-station-and-reading-conditions": {
        "summary": "站点与读数混合条件：读数条件单独翻转",
        "value": {
            "query": (
                "SELECT s.id FROM stations s "
                "LEFT JOIN readings r ON s.id = r.station_id "
                "WHERE r.value >= 10 AND s.threshold IS NOT NULL"
            )
        },
    },
    "mutually-complementary-uncoverable": {
        "summary": "互为补的两个读数条件：均无法单独覆盖",
        "value": {
            "query": (
                "SELECT s.id FROM stations s "
                "LEFT JOIN readings r ON s.id = r.station_id "
                "WHERE r.value > 5 AND NOT (r.value <= 5)"
            )
        },
    },
}


def _dsn() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DSN)


def _solver_timeout_ms(override: int | None) -> int:
    if override is not None:
        return override
    return int(os.environ.get("SOLVER_TIMEOUT_MS", "10000"))


@app.get("/", summary="服务信息")
def root() -> dict:
    return {
        "service": "sql-equivalence-checker",
        "docs": "/docs",
        "check_endpoint": "POST /check",
        "samples_endpoint": "POST /samples",
        "bound_rows_per_table": MAX_ROWS,
    }


@app.get("/health", summary="健康检查（含数据库连通性）")
def health() -> dict:
    db_up = Verifier(_dsn()).ping()
    return {"status": "ok", "database": "up" if db_up else "down"}


@app.post(
    "/check",
    response_model=CheckResponse,
    summary="核对两条 SELECT 在界内是否等价",
)
def check_equivalence(
    req: CheckRequest = Body(openapi_examples=_QUERY_EXAMPLES),
) -> CheckResponse:
    # 1. 解析与白名单校验
    try:
        ir_a = parse_query(req.query_a)
        ir_b = parse_query(req.query_b)
    except SqlRejected as exc:
        raise HTTPException(status_code=400, detail=f"SQL 超出受支持子集: {exc}")
    if len(ir_a.projections) != len(ir_b.projections):
        raise HTTPException(
            status_code=400,
            detail="两条查询的投影列数不一致，无法按列位置比较",
        )

    # 2. z3 有界等价判定
    outcome = check_bounded_equivalence(ir_a, ir_b, _solver_timeout_ms(req.solver_timeout_ms))
    if outcome.status == "equivalent":
        return CheckResponse(
            verdict="EQUIVALENT",
            message=(
                f"在每表 0~{MAX_ROWS} 行的所有合法取值下，两侧结果多重集相同"
                "（有界等价，不构成更大表上的证明）。"
            ),
        )
    if outcome.status == "unknown":
        return CheckResponse(
            verdict="UNKNOWN", message=f"求解失败：{outcome.detail}"
        )

    # 3. 候选反例必须在隔离 PostgreSQL 中实际复现
    assert outcome.witness is not None
    repro = Verifier(_dsn()).reproduce(outcome.witness, req.query_a, req.query_b)
    if not repro.ok:
        return CheckResponse(verdict="UNKNOWN", message=repro.detail)
    if not repro.differs:
        return CheckResponse(
            verdict="UNKNOWN",
            message="内部不一致：z3 反例在 PostgreSQL 中未复现差异，按未知处理。",
        )
    return CheckResponse(
        verdict="NOT_EQUIVALENT",
        message="两侧结果存在差异，已在隔离 PostgreSQL 中实际复现。",
        counterexample=Counterexample(
            setup_sql=setup_sql(outcome.witness),
            query_a_result=repro.query_a_result or [],
            query_b_result=repro.query_b_result or [],
        ),
    )


def _is_left_join_shape(ir: QueryIR) -> bool:
    """是否恰为 stations LEFT JOIN readings ON stations.id = readings.station_id。"""
    return (
        ir.tables == ("stations", "readings")
        and ir.join is not None
        and ir.join.kind == "left"
        and ir.join.left_col == ColRef("stations", "id")
        and ir.join.right_col == ColRef("readings", "station_id")
    )


@app.post(
    "/samples",
    response_model=SamplesResponse,
    summary="为 WHERE 中每个 readings.value 条件生成测试样本对",
)
def coverage_samples(
    req: SamplesRequest = Body(openapi_examples=_SAMPLE_EXAMPLES),
) -> SamplesResponse:
    # 1. 解析与白名单校验，并限制为固定的左连接形态
    try:
        ir = parse_query(req.query)
    except SqlRejected as exc:
        raise HTTPException(status_code=400, detail=f"SQL 超出受支持子集: {exc}")
    if not _is_left_join_shape(ir):
        raise HTTPException(
            status_code=400,
            detail=(
                "仅支持 stations LEFT JOIN readings "
                "ON stations.id = readings.station_id（表可加别名，ON 两侧可交换）"
            ),
        )

    # 2. 按书写顺序找出所有检查 readings.value 的原子条件
    atoms = iter_atoms(ir.where)
    targets = [i for i, a in enumerate(atoms) if is_target_condition(a)]
    if not targets:
        return SamplesResponse(
            conditions=[],
            message="WHERE 中没有检查 readings.value 的比较或判空条件。",
        )

    # 3. 逐条件求解快照对，并在隔离 PostgreSQL 中复核
    timeout_ms = _solver_timeout_ms(req.solver_timeout_ms)
    verifier = Verifier(_dsn())
    diag = diagnostic_sql(atoms, ir.where)  # 有目标条件必然有 WHERE
    results: list[ConditionCoverage] = []
    for t in targets:
        text = render_condition(atoms[t])
        outcome = find_coverage_sample(ir, atoms, t, timeout_ms)
        if outcome.status == "uncoverable":
            results.append(
                ConditionCoverage(
                    index=t,
                    condition=text,
                    status="UNCOVERABLE",
                    message=(
                        f"每表 0~{MAX_ROWS} 行界内不存在仅改变该条件、其余原子条件"
                        "不变的快照对，该条件无法单独覆盖。"
                    ),
                )
            )
            continue
        if outcome.status != "found" or outcome.witness is None:
            results.append(
                ConditionCoverage(
                    index=t,
                    condition=text,
                    status="UNKNOWN",
                    message=f"求解失败：{outcome.detail}",
                )
            )
            continue
        w = outcome.witness
        chk = verifier.verify_sample_pair(w, req.query, diag, len(atoms), t)
        if not chk.ok:
            results.append(
                ConditionCoverage(
                    index=t, condition=text, status="UNKNOWN", message=chk.detail
                )
            )
            continue
        if not chk.confirmed:
            results.append(
                ConditionCoverage(
                    index=t,
                    condition=text,
                    status="UNKNOWN",
                    message=f"复核未确认：{chk.detail}",
                )
            )
            continue
        results.append(
            ConditionCoverage(
                index=t,
                condition=text,
                status="COVERED",
                message="已找到仅改变该条件的快照对，并在隔离 PostgreSQL 中复核通过。",
                pair=SnapshotPair(
                    setup_sql_a=setup_sql(w.state_a),
                    setup_sql_b=setup_sql(w.state_b),
                    changed_reading=ChangedReading(
                        station_id=w.station_id,
                        value_a=w.value_a,
                        value_b=w.value_b,
                    ),
                    target_truth_a=chk.target_truth_a,  # type: ignore[arg-type]
                    target_truth_b=chk.target_truth_b,  # type: ignore[arg-type]
                    where_truth_a=chk.where_truth_a,  # type: ignore[arg-type]
                    where_truth_b=chk.where_truth_b,  # type: ignore[arg-type]
                    query_result_a=chk.query_result_a or [],
                    query_result_b=chk.query_result_b or [],
                ),
            )
        )

    covered = sum(1 for r in results if r.status == "COVERED")
    uncoverable = sum(1 for r in results if r.status == "UNCOVERABLE")
    unknown = len(results) - covered - uncoverable
    return SamplesResponse(
        conditions=results,
        message=(
            f"共 {len(results)} 个 readings.value 条件：{covered} 个已覆盖，"
            f"{uncoverable} 个界内无法单独覆盖，{unknown} 个未知。"
        ),
    )
