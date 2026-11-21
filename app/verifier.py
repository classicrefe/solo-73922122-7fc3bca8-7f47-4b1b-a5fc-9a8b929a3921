"""在隔离的 PostgreSQL 模式中实际复现反例。

z3 给出的候选反例必须在真实 PostgreSQL 中重放并确认两侧结果多重集确实不同，
才允许上报"不等价"；复现失败或未复现差异时一律按"未知"处理（由调用方决定）。
隔离方式：每次复现使用独立的临时 SCHEMA，整个流程在单事务内完成并回滚，
不在数据库中留下任何对象。
"""
from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any

import psycopg

from .z3check import CounterexampleData
from .z3sample import SampleWitness

_CREATE_STATIONS = "CREATE TABLE stations (id INTEGER PRIMARY KEY, threshold INTEGER)"
_CREATE_READINGS = (
    "CREATE TABLE readings (station_id INTEGER REFERENCES stations(id), value INTEGER)"
)


@dataclass
class ReproOutcome:
    ok: bool                     # 复现流程是否成功执行
    differs: bool = False        # 两侧结果多重集是否确实不同
    detail: str = ""
    query_a_result: list[list[Any]] | None = None
    query_b_result: list[list[Any]] | None = None


@dataclass
class SampleCheckOutcome:
    """样本对在隔离 PostgreSQL 中的复核结果。"""

    ok: bool                            # 复核流程是否成功执行
    confirmed: bool = False             # 各项条件与结果差异是否都得到确认
    detail: str = ""
    query_result_a: list[list[Any]] | None = None
    query_result_b: list[list[Any]] | None = None
    target_truth_a: str | None = None   # 'TRUE' | 'FALSE' | 'UNKNOWN'
    target_truth_b: str | None = None
    where_truth_a: str | None = None
    where_truth_b: str | None = None


def setup_sql(witness: CounterexampleData) -> str:
    """生成可装载的 SQL 脚本（重建表结构与反例数据）。"""
    lines = [
        "DROP TABLE IF EXISTS readings;",
        "DROP TABLE IF EXISTS stations;",
        _CREATE_STATIONS + ";",
        _CREATE_READINGS + ";",
    ]
    if witness.stations:
        vals = ", ".join(f"({sid}, {_lit(th)})" for sid, th in witness.stations)
        lines.append(f"INSERT INTO stations (id, threshold) VALUES {vals};")
    if witness.readings:
        vals = ", ".join(f"({_lit(sid)}, {_lit(v)})" for sid, v in witness.readings)
        lines.append(f"INSERT INTO readings (station_id, value) VALUES {vals};")
    return "\n".join(lines) + "\n"


def _lit(v: Any) -> str:
    return "NULL" if v is None else str(int(v))


def _canon(rows: list[list[Any]]) -> list[tuple]:
    """规范化结果多重集：NULL 可比较、忽略行序、保留重复。"""
    return sorted(
        tuple((0, 0) if v is None else (1, v) for v in row) for row in rows
    )


_TRUTH = {True: "TRUE", False: "FALSE", None: "UNKNOWN"}


def _open_scratch(cur, schema: str, timeout_ms: int) -> None:
    """在事务内建立隔离用临时模式并建好源表结构。"""
    cur.execute(f'CREATE SCHEMA "{schema}"')
    cur.execute(f'SET search_path TO "{schema}"')
    cur.execute(f"SET LOCAL statement_timeout TO {timeout_ms}")
    cur.execute(_CREATE_STATIONS)
    cur.execute(_CREATE_READINGS)


def _load_state(cur, state: CounterexampleData) -> None:
    if state.stations:
        cur.executemany(
            "INSERT INTO stations (id, threshold) VALUES (%s, %s)", state.stations
        )
    if state.readings:
        cur.executemany(
            "INSERT INTO readings (station_id, value) VALUES (%s, %s)",
            state.readings,
        )


def _confirm_sample(
    diag_a: list[tuple],
    diag_b: list[tuple],
    res_a: list[list[Any]],
    res_b: list[list[Any]],
    atom_count: int,
    target_idx: int,
) -> SampleCheckOutcome:
    """对照两侧诊断行与查询结果，确认样本对满足全部条件。

    诊断行布局：(s.id, r.station_id, r.value, atom_0..atom_{n-1}, where)。
    """
    count_a, count_b = Counter(diag_a), Counter(diag_b)
    pair: tuple[tuple, tuple] | None = None
    for row_a in count_a:
        for row_b in count_b:
            if row_a == row_b:
                continue
            # 两侧诊断行仅允许相差这一对（被修改读数对应的连接行）
            if count_a - Counter([row_a]) != count_b - Counter([row_b]):
                continue
            if _row_pair_ok(row_a, row_b, atom_count, target_idx):
                pair = (row_a, row_b)
                break
        if pair is not None:
            break
    if pair is None:
        return SampleCheckOutcome(
            ok=True,
            confirmed=False,
            detail="PostgreSQL 复核未确认：两侧连接行上的条件真值不满足样本对要求",
        )
    if _canon(res_a) == _canon(res_b):
        return SampleCheckOutcome(
            ok=True,
            confirmed=False,
            detail="PostgreSQL 复核未确认：两侧查询结果多重集相同",
        )
    row_a, row_b = pair
    return SampleCheckOutcome(
        ok=True,
        confirmed=True,
        query_result_a=res_a,
        query_result_b=res_b,
        target_truth_a=_TRUTH[row_a[3 + target_idx]],
        target_truth_b=_TRUTH[row_b[3 + target_idx]],
        where_truth_a=_TRUTH[row_a[3 + atom_count]],
        where_truth_b=_TRUTH[row_b[3 + atom_count]],
    )


def _row_pair_ok(
    row_a: tuple, row_b: tuple, atom_count: int, target_idx: int
) -> bool:
    """候选行对是否满足：同一连接行、仅 value 不同、目标条件三值改变、
    其余原子条件三值相同、WHERE 恰好一侧为 TRUE。"""
    if row_a[0] != row_b[0]:            # 同一站点
        return False
    if row_a[1] is None or row_a[1] != row_b[1]:  # 同一条已匹配读数
        return False
    if row_a[2] == row_b[2]:            # value 必须改变
        return False
    for i in range(atom_count):
        ta, tb = row_a[3 + i], row_b[3 + i]
        if i == target_idx:
            if ta == tb:                # 目标条件三值结果必须改变
                return False
        elif ta != tb:                  # 其余原子条件三值结果必须相同
            return False
    wa = row_a[3 + atom_count] is True
    wb = row_b[3 + atom_count] is True
    return wa != wb                     # 仅一侧通过 WHERE


class Verifier:
    def __init__(self, dsn: str, statement_timeout_ms: int = 10000) -> None:
        self._dsn = dsn
        self._timeout_ms = statement_timeout_ms

    def ping(self) -> bool:
        try:
            with psycopg.connect(self._dsn, connect_timeout=3) as conn:
                conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    def reproduce(
        self, witness: CounterexampleData, query_a: str, query_b: str
    ) -> ReproOutcome:
        schema = "cc_" + uuid.uuid4().hex
        try:
            with psycopg.connect(self._dsn, connect_timeout=10) as conn:
                with conn.cursor() as cur:
                    _open_scratch(cur, schema, self._timeout_ms)
                    _load_state(cur, witness)
                    cur.execute(query_a)
                    res_a = [list(r) for r in cur.fetchall()]
                    cur.execute(query_b)
                    res_b = [list(r) for r in cur.fetchall()]
                conn.rollback()  # 隔离：事务内建临时模式，回滚即清理
        except Exception as exc:  # 数据库不可达 / 执行失败 -> 未知
            return ReproOutcome(ok=False, detail=f"PostgreSQL 复现失败: {exc}")
        return ReproOutcome(
            ok=True,
            differs=_canon(res_a) != _canon(res_b),
            query_a_result=res_a,
            query_b_result=res_b,
        )

    def verify_sample_pair(
        self,
        witness: SampleWitness,
        query_sql: str,
        diag_sql: str,
        atom_count: int,
        target_idx: int,
    ) -> SampleCheckOutcome:
        """在隔离模式中装载两侧快照，复核条件真值与查询结果。

        诊断查询对每条连接行输出所有原子条件与整体 WHERE 的三值结果；
        复核要求两侧诊断行仅相差被修改读数对应的一行，且该行上目标条件
        三值改变、其余原子条件不变、WHERE 恰好一侧为 TRUE；同时被测查询
        在两侧的结果多重集必须不同。
        """
        schema = "cs_" + uuid.uuid4().hex
        try:
            with psycopg.connect(self._dsn, connect_timeout=10) as conn:
                with conn.cursor() as cur:
                    _open_scratch(cur, schema, self._timeout_ms)
                    _load_state(cur, witness.state_a)
                    cur.execute(diag_sql)
                    diag_a = [tuple(r) for r in cur.fetchall()]
                    cur.execute(query_sql)
                    res_a = [list(r) for r in cur.fetchall()]
                    cur.execute("DELETE FROM readings")
                    cur.execute("DELETE FROM stations")
                    _load_state(cur, witness.state_b)
                    cur.execute(diag_sql)
                    diag_b = [tuple(r) for r in cur.fetchall()]
                    cur.execute(query_sql)
                    res_b = [list(r) for r in cur.fetchall()]
                conn.rollback()  # 隔离：事务内建临时模式，回滚即清理
        except Exception as exc:  # 数据库不可达 / 执行失败 -> 未知
            return SampleCheckOutcome(ok=False, detail=f"PostgreSQL 复核失败: {exc}")
        return _confirm_sample(diag_a, diag_b, res_a, res_b, atom_count, target_idx)
