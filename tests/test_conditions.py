"""原子条件枚举（书写顺序）、目标识别与 SQL 渲染测试。"""
import pytest

from app.conditions import (
    diagnostic_sql,
    is_target_condition,
    iter_atoms,
    render_condition,
)
from app.sql_parser import parse_query


def atoms_of(sql: str):
    return iter_atoms(parse_query(sql).where)


def test_written_order_mixed_connectives():
    atoms = atoms_of(
        "SELECT s.id FROM stations s LEFT JOIN readings r ON s.id = r.station_id "
        "WHERE s.threshold > 0 AND r.value > 5 OR r.value IS NULL AND NOT (r.value <= 3)"
    )
    assert [render_condition(a) for a in atoms] == [
        "(stations.threshold > 0)",
        "(readings.value > 5)",
        "(readings.value IS NULL)",
        "(readings.value <= 3)",
    ]


def test_written_order_with_parentheses_and_not():
    atoms = atoms_of(
        "SELECT id FROM stations "
        "WHERE NOT (threshold < 0 OR threshold IS NULL) AND id != 7"
    )
    assert [render_condition(a) for a in atoms] == [
        "(stations.threshold < 0)",
        "(stations.threshold IS NULL)",
        "(stations.id != 7)",
    ]


def test_duplicate_conditions_get_distinct_positions():
    atoms = atoms_of(
        "SELECT s.id FROM stations s LEFT JOIN readings r ON s.id = r.station_id "
        "WHERE r.value > 5 AND r.value > 5"
    )
    assert len(atoms) == 2
    assert atoms[0] == atoms[1]  # 语义相同但位置不同


def test_no_where_yields_no_atoms():
    assert iter_atoms(parse_query("SELECT id FROM stations").where) == []


def test_target_condition_only_on_readings_value():
    atoms = atoms_of(
        "SELECT s.id FROM stations s LEFT JOIN readings r ON s.id = r.station_id "
        "WHERE r.value >= 10 AND r.station_id IS NOT NULL AND s.threshold = 1 "
        "AND r.value IS NULL"
    )
    assert [is_target_condition(a) for a in atoms] == [True, False, False, True]


def test_render_condition_variants():
    atoms = atoms_of(
        "SELECT s.id FROM stations s LEFT JOIN readings r ON s.id = r.station_id "
        "WHERE r.value != -3 AND r.value IS NOT NULL"
    )
    assert [render_condition(a) for a in atoms] == [
        "(readings.value != -3)",
        "(readings.value IS NOT NULL)",
    ]


def test_diagnostic_sql_shape():
    ir = parse_query(
        "SELECT s.id FROM stations s LEFT JOIN readings r ON s.id = r.station_id "
        "WHERE r.value > 5 AND s.threshold IS NOT NULL"
    )
    atoms = iter_atoms(ir.where)
    sql = diagnostic_sql(atoms, ir.where)
    assert sql.startswith(
        'SELECT "s"."id", "r"."station_id", "r"."value", '
        '("r"."value" > 5), ("s"."threshold" IS NOT NULL), '
    )
    assert sql.endswith(
        "FROM stations s LEFT JOIN readings r ON s.id = r.station_id"
    )
    # 诊断列数 = 3 个标识列 + 原子条件数 + 整体 WHERE
    assert sql.count("(") >= 3
