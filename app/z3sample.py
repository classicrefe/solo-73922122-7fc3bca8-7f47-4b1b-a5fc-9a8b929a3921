"""为单个 readings.value 条件求解"仅该条件三值结果改变"的快照对。

快照对定义（两侧均须满足每表 <= MAX_ROWS 行、主键唯一且非空、外键引用
完整、int4 值域等源表约束）：
  - 两个快照仅在某一条"已匹配"读数行（station_id 非 NULL，外键保证其
    出现在左连接结果中）的 value 上不同，其余数据完全相同；
  - 在该读数参与的唯一连接行上：目标条件的三值结果改变，其余原子条件的
    三值结果不变，且 WHERE 整体恰好在一侧为 TRUE（仅一侧通过 WHERE）。

由于除该连接行外的所有行在两侧完全一致，一侧通过 WHERE 即意味着两侧
查询结果多重集相差恰好一行；该结论仍会在隔离 PostgreSQL 中复核。
"""
from __future__ import annotations

from dataclasses import dataclass

from z3 import (
    And as ZAnd,
    Implies,
    Not as ZNot,
    Or as ZOr,
    Solver,
    Xor,
    sat,
    unknown as z3_unknown,
    unsat,
)

from .conditions import Atom
from .ir import MAX_ROWS, SCHEMA, QueryIR
from .z3check import CounterexampleData, SymbolicDB, _encode_bool, _extract


@dataclass
class SampleWitness:
    """z3 模型提取出的快照对与被修改的读数。"""

    state_a: CounterexampleData
    state_b: CounterexampleData
    station_id: int          # 被修改读数所属的站点（已匹配）
    value_a: int | None      # 快照 A 中该读数的 value
    value_b: int | None      # 快照 B 中该读数的 value


@dataclass
class SampleOutcome:
    status: str  # 'found' | 'uncoverable' | 'unknown'
    witness: SampleWitness | None = None
    detail: str = ""


def find_coverage_sample(
    ir: QueryIR, atoms: list[Atom], target_idx: int, timeout_ms: int = 10000
) -> SampleOutcome:
    """求解 atoms[target_idx] 的覆盖样本对；界内无解 / 失败分别回报。"""
    assert ir.where is not None  # 有目标条件必然有 WHERE
    db_a = SymbolicDB(prefix="a_")
    db_b = SymbolicDB(prefix="b_")
    solver = Solver()
    solver.set("timeout", timeout_ms)
    solver.add(*db_a.constraints())
    solver.add(*db_b.constraints())

    # 两快照的行存在性完全一致
    for tname in SCHEMA:
        for i in range(MAX_ROWS):
            solver.add(db_a.present[tname][i] == db_b.present[tname][i])
    # stations 全部单元格一致
    for col in SCHEMA["stations"]:
        for i in range(MAX_ROWS):
            solver.add(
                _cell_identical(
                    db_a.cells["stations"][col][i], db_b.cells["stations"][col][i]
                )
            )
    # readings.station_id 一致（仅允许某一行的 value 不同）
    for i in range(MAX_ROWS):
        solver.add(
            _cell_identical(
                db_a.cells["readings"]["station_id"][i],
                db_b.cells["readings"]["station_id"][i],
            )
        )

    # 选择被修改的读数行 k 及其匹配到的站点行 j
    per_k = []
    for k in range(MAX_ROWS):
        clauses = []
        for i in range(MAX_ROWS):
            if i != k:
                clauses.append(
                    _cell_identical(
                        db_a.cells["readings"]["value"][i],
                        db_b.cells["readings"]["value"][i],
                    )
                )
        # k 行存在且已匹配（station_id 非 NULL；外键约束保证存在对应站点）
        sid_null, sid_val = db_a.cells["readings"]["station_id"][k]
        clauses.append(db_a.present["readings"][k])
        clauses.append(ZNot(sid_null))
        per_j = []
        for j in range(MAX_ROWS):
            rc = [
                db_a.present["stations"][j],
                db_a.cells["stations"]["id"][j][1] == sid_val,
            ]
            cells_a = _joined_cells(db_a, j, k)
            cells_b = _joined_cells(db_b, j, k)
            for idx, atom in enumerate(atoms):
                da, va = _encode_bool(atom, cells_a)
                db_, vb = _encode_bool(atom, cells_b)
                if idx == target_idx:
                    # 三值结果改变：defined 不同，或均有定义但取值不同
                    rc.append(ZOr(da != db_, ZAnd(da, db_, va != vb)))
                else:
                    # 三值结果相同：defined 相同，且均有定义时取值相同
                    rc.append(ZAnd(da == db_, Implies(da, va == vb)))
            dwa, vwa = _encode_bool(ir.where, cells_a)
            dwb, vwb = _encode_bool(ir.where, cells_b)
            # 仅一侧通过 WHERE（TRUE 才通过；FALSE / UNKNOWN 均被过滤）
            rc.append(Xor(ZAnd(dwa, vwa), ZAnd(dwb, vwb)))
            per_j.append(ZAnd(*rc))
        clauses.append(ZOr(*per_j))
        per_k.append(ZAnd(*clauses))
    solver.add(ZOr(*per_k))

    result = solver.check()
    if result == unsat:
        return SampleOutcome(
            "uncoverable",
            detail=f"每表 0~{MAX_ROWS} 行界内不存在满足条件的快照对",
        )
    if result == z3_unknown:
        return SampleOutcome(
            "unknown", detail=f"求解器无法判定（原因: {solver.reason_unknown()}）"
        )
    if result != sat:  # pragma: no cover - 防御
        return SampleOutcome("unknown", detail=f"求解器返回异常状态: {result}")

    model = solver.model()
    return _build_witness(_extract(model, db_a), _extract(model, db_b))


# ---------- 内部实现 ----------

def _cell_identical(c1, c2):
    """两符号单元格完全相同（NULL 标记与取值都一致）。"""
    return ZAnd(c1[0] == c2[0], c1[1] == c2[1])


def _joined_cells(db: SymbolicDB, j: int, k: int):
    """站点行 j 与读数行 k 组成的连接行的符号单元格。"""
    cells = {("stations", c): db.cells["stations"][c][j] for c in SCHEMA["stations"]}
    cells.update(
        {("readings", c): db.cells["readings"][c][k] for c in SCHEMA["readings"]}
    )
    return cells


def _build_witness(
    state_a: CounterexampleData, state_b: CounterexampleData
) -> SampleOutcome:
    """从提取的两侧状态中定位被修改的读数行（防御性校验编码不变量）。"""
    ra, rb = state_a.readings, state_b.readings
    if len(ra) != len(rb):
        return SampleOutcome("unknown", detail="内部不一致：两侧读数行数不同")
    if any(a[0] != b[0] for a, b in zip(ra, rb)):
        return SampleOutcome("unknown", detail="内部不一致：读数 station_id 发生变化")
    diff = [i for i in range(len(ra)) if ra[i] != rb[i]]
    if len(diff) != 1:
        return SampleOutcome("unknown", detail="内部不一致：被修改的读数行不唯一")
    k = diff[0]
    sid, value_a = ra[k]
    _, value_b = rb[k]
    if sid is None:
        return SampleOutcome("unknown", detail="内部不一致：被修改读数未匹配站点")
    return SampleOutcome(
        "found",
        witness=SampleWitness(
            state_a=state_a,
            state_b=state_b,
            station_id=sid,
            value_a=value_a,
            value_b=value_b,
        ),
    )
