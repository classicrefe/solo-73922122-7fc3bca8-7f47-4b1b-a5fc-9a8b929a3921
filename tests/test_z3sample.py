"""z3 快照对求解测试（不依赖数据库）。"""
import pytest

from app.conditions import is_target_condition, iter_atoms
from app.ir import MAX_ROWS
from app.sql_parser import parse_query
from app.z3sample import find_coverage_sample

BASE = (
    "SELECT s.id FROM stations s LEFT JOIN readings r "
    "ON s.id = r.station_id WHERE "
)


def solve(sql: str):
    ir = parse_query(sql)
    atoms = iter_atoms(ir.where)
    targets = [i for i, a in enumerate(atoms) if is_target_condition(a)]
    return [
        (t, find_coverage_sample(ir, atoms, t, timeout_ms=20000)) for t in targets
    ]


def assert_witness_legal(w):
    """快照对必须满足行数界、主键唯一、外键引用完整，且仅一条读数的 value 不同。"""
    for state in (w.state_a, w.state_b):
        assert len(state.stations) <= MAX_ROWS
        assert len(state.readings) <= MAX_ROWS
        ids = [s[0] for s in state.stations]
        assert len(ids) == len(set(ids))
        for sid, _ in state.readings:
            assert sid is None or sid in ids
    # 两快照仅一条已匹配读数的 value 不同
    assert w.state_a.stations == w.state_b.stations
    ra, rb = w.state_a.readings, w.state_b.readings
    assert len(ra) == len(rb)
    assert [r[0] for r in ra] == [r[0] for r in rb]
    diffs = [i for i in range(len(ra)) if ra[i] != rb[i]]
    assert len(diffs) == 1
    k = diffs[0]
    assert ra[k][0] == w.station_id == rb[k][0]
    assert ra[k][1] == w.value_a and rb[k][1] == w.value_b
    assert w.value_a != w.value_b
    # 被修改读数已匹配（station_id 非空且引用存在的站点）
    assert w.station_id in [s[0] for s in w.state_a.stations]


# ---------- 界内可覆盖 ----------

@pytest.mark.parametrize(
    "where",
    [
        "r.value >= 10 AND s.threshold IS NOT NULL",
        "r.value IS NULL",
        "r.value IS NOT NULL",
        "NOT (r.value > 5)",
        "r.value = 5 OR s.threshold = 0",
        "s.id != 7 AND r.value < 0",
    ],
)
def test_coverable_conditions(where):
    results = solve(BASE + where)
    assert results, "应存在目标条件"
    for _, outcome in results:
        assert outcome.status == "found", outcome.detail
        assert_witness_legal(outcome.witness)


def test_value_change_can_cross_null():
    """IS NULL 条件的样本对应在 NULL 与非 NULL 之间翻转。"""
    [(t, outcome)] = solve(BASE + "r.value IS NULL")
    w = outcome.witness
    assert (w.value_a is None) != (w.value_b is None)


def test_only_target_changes_others_fixed():
    """其余原子条件不变：求解器给出的快照对只动目标读数的 value。"""
    results = solve(BASE + "r.value > 5 OR r.value IS NULL")
    assert [o.status for _, o in results] == ["found", "uncoverable"]
    assert_witness_legal(results[0][1].witness)


# ---------- 界内无法单独覆盖 ----------

@pytest.mark.parametrize(
    "where",
    [
        # 互为补：任一变化必然带动另一个
        "r.value > 5 AND NOT (r.value <= 5)",
        # 完全重复的条件
        "r.value > 5 AND r.value > 5",
        # 永真组合：判空翻转必然带动另一个判空
        "r.value IS NULL OR r.value IS NOT NULL",
        # 判空恒假使 WHERE 两侧都不过；判空翻转又必然带动比较
        "r.value IS NULL AND r.value > 5",
    ],
)
def test_uncoverable_conditions(where):
    results = solve(BASE + where)
    assert results
    for _, outcome in results:
        assert outcome.status == "uncoverable", outcome.detail


def test_second_condition_uncoverable_first_covered():
    results = solve(BASE + "r.value > 5 OR r.value IS NULL")
    assert [t for t, _ in results] == [0, 1]
    assert results[0][1].status == "found"
    assert results[1][1].status == "uncoverable"
