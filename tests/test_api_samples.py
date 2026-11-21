"""测试样本 API 行为测试：形态校验、空目标、COVERED / UNCOVERABLE / UNKNOWN。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

LEFT_JOIN = (
    "SELECT s.id FROM stations s LEFT JOIN readings r ON s.id = r.station_id"
)


# ---------- 请求校验 ----------

@pytest.mark.parametrize(
    "query",
    [
        # 单表
        "SELECT id FROM stations WHERE threshold = 1",
        # INNER JOIN
        "SELECT s.id FROM stations s INNER JOIN readings r "
        "ON s.id = r.station_id WHERE r.value > 1",
        # 连接方向颠倒（readings 驱动）
        "SELECT r.station_id FROM readings r LEFT JOIN stations s "
        "ON s.id = r.station_id WHERE r.value > 1",
        # ON 列不符
        "SELECT s.id FROM stations s LEFT JOIN readings r "
        "ON s.threshold = r.value WHERE r.value > 1",
    ],
)
def test_non_left_join_shape_rejected(query):
    resp = client.post("/samples", json={"query": query})
    assert resp.status_code == 400
    assert "LEFT JOIN" in resp.json()["detail"]


def test_unsupported_sql_rejected():
    resp = client.post("/samples", json={"query": "SELECT * FROM stations"})
    assert resp.status_code == 400
    assert "受支持子集" in resp.json()["detail"]


def test_no_value_condition_returns_empty():
    for suffix in ("", " WHERE s.threshold = 1 AND s.id IS NOT NULL"):
        resp = client.post("/samples", json={"query": LEFT_JOIN + suffix})
        assert resp.status_code == 200
        body = resp.json()
        assert body["conditions"] == []
        assert body["bound_rows"] == 3


# ---------- UNCOVERABLE（纯 z3，无需数据库） ----------

def test_uncoverable_complementary_conditions():
    resp = client.post(
        "/samples",
        json={"query": LEFT_JOIN + " WHERE r.value > 5 AND NOT (r.value <= 5)"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [c["status"] for c in body["conditions"]] == [
        "UNCOVERABLE",
        "UNCOVERABLE",
    ]
    assert [c["index"] for c in body["conditions"]] == [0, 1]
    assert body["conditions"][0]["condition"] == "(readings.value > 5)"
    assert body["conditions"][1]["condition"] == "(readings.value <= 5)"
    assert all(c["pair"] is None for c in body["conditions"])
    assert "无法单独覆盖" in body["message"]


def test_written_order_of_conditions():
    resp = client.post(
        "/samples",
        json={
            "query": LEFT_JOIN
            + " WHERE s.threshold > 0 AND r.value >= 10 OR r.value IS NULL"
        },
    )
    assert resp.status_code == 200
    conds = resp.json()["conditions"]
    # 只列出 readings.value 条件，且按书写顺序
    assert [c["index"] for c in conds] == [1, 2]
    assert [c["condition"] for c in conds] == [
        "(readings.value >= 10)",
        "(readings.value IS NULL)",
    ]


# ---------- COVERED / UNKNOWN（打桩复核器） ----------

class _OkVerifier:
    def __init__(self, dsn):
        pass

    def verify_sample_pair(self, witness, query_sql, diag_sql, atom_count, target_idx):
        from app.verifier import SampleCheckOutcome

        return SampleCheckOutcome(
            ok=True,
            confirmed=True,
            query_result_a=[[1]],
            query_result_b=[],
            target_truth_a="FALSE",
            target_truth_b="TRUE",
            where_truth_a="FALSE",
            where_truth_b="TRUE",
        )


class _DownVerifier:
    def __init__(self, dsn):
        pass

    def verify_sample_pair(self, witness, query_sql, diag_sql, atom_count, target_idx):
        from app.verifier import SampleCheckOutcome

        return SampleCheckOutcome(ok=False, detail="PostgreSQL 复核失败: 连接被拒")


class _UnconfirmedVerifier:
    def __init__(self, dsn):
        pass

    def verify_sample_pair(self, witness, query_sql, diag_sql, atom_count, target_idx):
        from app.verifier import SampleCheckOutcome

        return SampleCheckOutcome(ok=True, confirmed=False, detail="两侧结果相同")


def test_covered_with_stubbed_verifier(monkeypatch):
    from app import main

    monkeypatch.setattr(main, "Verifier", _OkVerifier)
    resp = client.post(
        "/samples", json={"query": LEFT_JOIN + " WHERE r.value >= 10"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [c["status"] for c in body["conditions"]] == ["COVERED"]
    pair = body["conditions"][0]["pair"]
    assert "CREATE TABLE stations" in pair["setup_sql_a"]
    assert "CREATE TABLE stations" in pair["setup_sql_b"]
    assert pair["changed_reading"]["station_id"] is not None
    assert pair["target_truth_a"] == "FALSE"
    assert pair["target_truth_b"] == "TRUE"
    assert pair["where_truth_a"] != pair["where_truth_b"]
    assert pair["query_result_a"] == [[1]]
    assert pair["query_result_b"] == []


def test_unknown_when_db_unavailable(monkeypatch):
    from app import main

    monkeypatch.setattr(main, "Verifier", _DownVerifier)
    resp = client.post(
        "/samples", json={"query": LEFT_JOIN + " WHERE r.value >= 10"}
    )
    assert resp.status_code == 200
    conds = resp.json()["conditions"]
    assert [c["status"] for c in conds] == ["UNKNOWN"]
    assert "复核失败" in conds[0]["message"]


def test_unknown_when_not_confirmed(monkeypatch):
    from app import main

    monkeypatch.setattr(main, "Verifier", _UnconfirmedVerifier)
    resp = client.post(
        "/samples", json={"query": LEFT_JOIN + " WHERE r.value >= 10"}
    )
    assert resp.status_code == 200
    conds = resp.json()["conditions"]
    assert [c["status"] for c in conds] == ["UNKNOWN"]
    assert "复核未确认" in conds[0]["message"]


def test_openapi_includes_samples():
    assert client.get("/docs").status_code == 200
    spec = client.get("/openapi.json").json()
    assert "/samples" in spec["paths"]
