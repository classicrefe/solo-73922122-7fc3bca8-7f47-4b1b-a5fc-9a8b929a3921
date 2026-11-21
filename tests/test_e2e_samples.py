"""测试样本 API 端到端测试：需要真实 PostgreSQL（环境变量 DATABASE_URL）。"""
import os

import pytest
from fastapi.testclient import TestClient

from app.main import app

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL 未设置，跳过端到端测试"
)

client = TestClient(app)

LEFT_JOIN = (
    "SELECT s.id, r.value FROM stations s LEFT JOIN readings r "
    "ON s.id = r.station_id"
)


def test_e2e_covered_pair_verified_in_postgres():
    resp = client.post(
        "/samples",
        json={"query": LEFT_JOIN + " WHERE r.value >= 10 AND s.threshold IS NOT NULL"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [c["status"] for c in body["conditions"]] == ["COVERED"]
    pair = body["conditions"][0]["pair"]
    # 两份可装载 SQL，仅差一条读数的 value
    assert "INSERT INTO readings" in pair["setup_sql_a"]
    assert "INSERT INTO readings" in pair["setup_sql_b"]
    assert pair["changed_reading"]["value_a"] != pair["changed_reading"]["value_b"]
    # 目标条件三值改变、WHERE 仅一侧 TRUE、结果多重集不同（均经 PG 复核）
    assert pair["target_truth_a"] != pair["target_truth_b"]
    assert sorted([pair["where_truth_a"], pair["where_truth_b"]]) == sorted(
        ["TRUE", "FALSE"]
    ) or sorted([pair["where_truth_a"], pair["where_truth_b"]]) == sorted(
        ["TRUE", "UNKNOWN"]
    )
    assert sorted(map(tuple, pair["query_result_a"])) != sorted(
        map(tuple, pair["query_result_b"])
    )


def test_e2e_mixed_coverable_and_uncoverable():
    resp = client.post(
        "/samples", json={"query": LEFT_JOIN + " WHERE r.value > 5 OR r.value IS NULL"}
    )
    assert resp.status_code == 200
    conds = resp.json()["conditions"]
    assert [c["status"] for c in conds] == ["COVERED", "UNCOVERABLE"]
    pair = conds[0]["pair"]
    assert pair["changed_reading"]["value_a"] is not None
    assert pair["changed_reading"]["value_b"] is not None


def test_e2e_null_flip_sample():
    resp = client.post(
        "/samples", json={"query": LEFT_JOIN + " WHERE r.value IS NULL"}
    )
    assert resp.status_code == 200
    conds = resp.json()["conditions"]
    assert [c["status"] for c in conds] == ["COVERED"]
    pair = conds[0]["pair"]
    # 一侧 value 为 NULL（判空条件三值翻转）
    vals = [pair["changed_reading"]["value_a"], pair["changed_reading"]["value_b"]]
    assert (vals[0] is None) != (vals[1] is None)
    assert sorted([pair["target_truth_a"], pair["target_truth_b"]]) == [
        "FALSE",
        "TRUE",
    ]


def test_e2e_setup_sql_loadable_and_results_differ():
    """返回的两份装载 SQL 在真实库中执行后，查询结果确实不同。"""
    import psycopg

    resp = client.post(
        "/samples", json={"query": LEFT_JOIN + " WHERE r.value >= 10"}
    )
    assert resp.status_code == 200
    pair = resp.json()["conditions"][0]["pair"]
    query = LEFT_JOIN + " WHERE r.value >= 10"
    dsn = os.environ["DATABASE_URL"]
    results = []
    with psycopg.connect(dsn) as conn:
        for setup in (pair["setup_sql_a"], pair["setup_sql_b"]):
            with conn.cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS readings")
                cur.execute("DROP TABLE IF EXISTS stations")
                cur.execute(setup)
                cur.execute(query)
                results.append(sorted(map(tuple, cur.fetchall())))
        conn.rollback()
    assert results[0] != results[1]
    # 与 API 复核时记录的结果一致
    assert results[0] == sorted(map(tuple, pair["query_result_a"]))
    assert results[1] == sorted(map(tuple, pair["query_result_b"]))
