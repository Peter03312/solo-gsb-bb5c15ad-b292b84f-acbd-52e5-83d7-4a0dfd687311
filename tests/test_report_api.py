"""修订影响报告 HTTP API 集成测试。

运行：PYTHONPATH=. python -m pytest tests/test_report_api.py -q
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(settings, "db_path", str(db_file))
    monkeypatch.setattr(settings, "db_timeout", 5)
    from app.main import app

    with TestClient(app) as c:
        yield c


def reading(rid, rev, minute, temp, base_hour=0):
    ts = datetime(2026, 10, 1, base_hour, 0, tzinfo=timezone.utc) + timedelta(minutes=minute)
    return {"reading_id": rid, "revision": rev,
            "measured_at": ts.isoformat().replace("+00:00", "Z"),
            "temperature": temp}


def make_window(client, name="w", minutes=60, max_gap=600.0):
    end = (datetime(2026, 10, 1, tzinfo=timezone.utc)
           + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
    r = client.post("/api/v1/windows", json={
        "name": name,
        "start_at": "2026-10-01T00:00:00Z",
        "end_at": end,
        "allowed_temp_min": 18.0, "allowed_temp_max": 22.0,
        "max_continuous_exceed_seconds": 300,
        "max_sample_interval_seconds": max_gap})
    assert r.status_code == 201, r.text
    return r.json()["id"]


REPORT = "/api/v1/certificates/revision-impact-report"


# ---------------------------------------------------------------- 主流程

def test_report_end_to_end(client):
    wid = make_window(client)
    # v1：00:00->00:10 超限 600s -> fail；之后边界未知
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("a", 1, 0, 23.0))
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("b", 1, 10, 20.0))
    c1 = client.post(f"/api/v1/windows/{wid}/certificates/issue",
                     json={"note": "首签"}).json()
    assert c1["verdict"] == "fail"

    # 修订 a 回正常；补齐读数填满整窗（间隔 10min <= 600s）-> pass
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("a", 2, 0, 20.0))
    for m in range(20, 61, 10):
        assert client.post(
            f"/api/v1/windows/{wid}/readings",
            json=reading(f"r{m:02d}", 1, m, 20.0)).status_code == 201
    c2 = client.post(f"/api/v1/windows/{wid}/certificates/issue",
                     json={"note": "整改后重签"}).json()
    assert c2["verdict"] == "pass"

    r = client.post(REPORT, json={"old_certificate_id": c1["id"],
                                  "new_certificate_id": c2["id"]})
    assert r.status_code == 200, r.text
    rep = r.json()

    # 证书/窗口引用
    assert rep["old_certificate"]["id"] == c1["id"]
    assert rep["new_certificate"]["id"] == c2["id"]
    assert rep["window"]["id"] == wid
    assert rep["basis"]["source"] == "certificate_snapshots"

    # 读数差异：a 变更，r20..r60 新增，b 未变
    diff = rep["reading_diff"]
    assert diff["counts"] == {"old_total": 2, "new_total": 7,
                              "added": 5, "changed": 1, "removed": 0, "unchanged": 1}
    assert diff["changed_readings"][0]["reading_id"] == "a"
    assert diff["changed_readings"][0]["after"]["revision"] == 2

    # 状态变化区间：[00:00,00:10) 超限->正常；[00:10,01:00) 边界未知->正常（5 段）
    rows = rep["changed_segments"]
    first = rows[0]
    assert (first["start"], first["end"]) == (
        "2026-10-01T00:00:00Z", "2026-10-01T00:10:00Z")
    assert first["before"]["status"] == "exceeded"
    assert first["after"]["status"] == "normal"
    assert first["before"]["evidence"]["from_reading"]["revision"] == 1
    assert first["after"]["evidence"]["from_reading"]["revision"] == 2
    assert len(rows) == 6

    # 汇总
    s = rep["summary"]
    assert s["verdict_changed"]
    assert (s["verdict_before"], s["verdict_after"]) == ("fail", "pass")
    assert s["max_continuous_exceeded_before_seconds"] == 600.0
    assert s["max_continuous_exceeded_after_seconds"] == 0.0
    assert s["max_continuous_exceeded_delta_seconds"] == -600.0
    assert s["status_transition_counts"] == {
        "exceeded->normal": 1, "unknown->normal": 5}
    assert s["conclusions_zh"]


def test_report_sample_gap_reason(client):
    wid = make_window(client, max_gap=600.0)
    # 00:00 与 00:30：间隔 1800s 超限 -> 采样间隔未知
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("a", 1, 0, 20.0))
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("b", 1, 30, 20.0))
    c1 = client.post(f"/api/v1/windows/{wid}/certificates/issue", json={}).json()

    client.post(f"/api/v1/windows/{wid}/readings", json=reading("m", 1, 10, 20.0))
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("n", 1, 20, 20.0))
    c2 = client.post(f"/api/v1/windows/{wid}/certificates/issue", json={}).json()

    rep = client.post(REPORT, json={"old_certificate_id": c1["id"],
                                    "new_certificate_id": c2["id"]}).json()
    first = rep["changed_segments"][0]
    assert first["before"]["status"] == "unknown"
    assert first["before"]["evidence"]["unknown_reason_code"] == "sample_gap"
    assert first["before"]["evidence"]["from_reading"]["reading_id"] == "a"
    assert first["before"]["evidence"]["to_reading"]["reading_id"] == "b"
    # [00:30,01:00) 两侧同为边界未知 -> 不输出
    assert all(s["start"] != "2026-10-01T00:30:00Z" for s in rep["changed_segments"])


def test_report_no_readings_unknown_reason(client):
    wid = make_window(client)
    c1 = client.post(f"/api/v1/windows/{wid}/certificates/issue", json={}).json()
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("a", 1, 0, 20.0))
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("b", 1, 10, 20.0))
    c2 = client.post(f"/api/v1/windows/{wid}/certificates/issue", json={}).json()
    rep = client.post(REPORT, json={"old_certificate_id": c1["id"],
                                    "new_certificate_id": c2["id"]}).json()
    first = rep["changed_segments"][0]
    assert first["before"]["evidence"]["unknown_reason_code"] == "no_readings"
    assert first["before"]["evidence"]["from_reading"] is None
    # [00:10,01:00) 旧版无读数未知 / 新版边界未知，状态未变 -> 不输出
    assert all(s["start"] != "2026-10-01T00:10:00Z" for s in rep["changed_segments"])


# ---------------------------------------------------------------- 拒绝场景

def test_report_missing_certificate_404(client):
    wid = make_window(client)
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("a", 1, 0, 20.0))
    c1 = client.post(f"/api/v1/windows/{wid}/certificates/issue", json={}).json()

    r = client.post(REPORT, json={"old_certificate_id": 999,
                                  "new_certificate_id": c1["id"]})
    assert r.status_code == 404 and "旧版证书" in r.json()["detail"]

    r = client.post(REPORT, json={"old_certificate_id": c1["id"],
                                  "new_certificate_id": 999})
    assert r.status_code == 404 and "新版证书" in r.json()["detail"]


def test_report_same_certificate_rejected(client):
    wid = make_window(client)
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("a", 1, 0, 20.0))
    c1 = client.post(f"/api/v1/windows/{wid}/certificates/issue", json={}).json()
    r = client.post(REPORT, json={"old_certificate_id": c1["id"],
                                  "new_certificate_id": c1["id"]})
    assert r.status_code == 409 and "同一张证书" in r.json()["detail"]


def test_report_cross_window_rejected(client):
    w1 = make_window(client, name="w1")
    w2 = make_window(client, name="w2")
    for wid in (w1, w2):
        client.post(f"/api/v1/windows/{wid}/readings", json=reading("a", 1, 0, 20.0))
    c1 = client.post(f"/api/v1/windows/{w1}/certificates/issue", json={}).json()
    d2 = client.post(f"/api/v1/windows/{w2}/certificates/issue", json={}).json()
    r = client.post(REPORT, json={"old_certificate_id": c1["id"],
                                  "new_certificate_id": d2["id"]})
    assert r.status_code == 409 and "同一审定窗口" in r.json()["detail"]


def test_report_wrong_order_rejected(client):
    wid = make_window(client)
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("a", 1, 0, 20.0))
    c1 = client.post(f"/api/v1/windows/{wid}/certificates/issue", json={}).json()
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("b", 1, 10, 20.0))
    c2 = client.post(f"/api/v1/windows/{wid}/certificates/issue", json={}).json()
    # 新在前、旧在后 -> 409
    r = client.post(REPORT, json={"old_certificate_id": c2["id"],
                                  "new_certificate_id": c1["id"]})
    assert r.status_code == 409 and "版本顺序" in r.json()["detail"]


def test_report_invalid_body_422(client):
    r = client.post(REPORT, json={"old_certificate_id": 1})
    assert r.status_code == 422
    r = client.post(REPORT, json={"old_certificate_id": 0,
                                  "new_certificate_id": 2})
    assert r.status_code == 422


# ---------------------------------------------------------------- 快照依据

def test_report_reflects_snapshots_not_active_state(client):
    """签发 v2 后继续修订活动读数，v1->v2 的报告内容不得变化。"""
    wid = make_window(client)
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("a", 1, 0, 23.0))
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("b", 1, 10, 20.0))
    c1 = client.post(f"/api/v1/windows/{wid}/certificates/issue", json={}).json()
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("a", 2, 0, 20.0))
    c2 = client.post(f"/api/v1/windows/{wid}/certificates/issue", json={}).json()

    body = {"old_certificate_id": c1["id"], "new_certificate_id": c2["id"]}
    rep1 = client.post(REPORT, json=body).json()

    # 签发之后再把 a 改成 rev3（超温）——活动状态变化，但快照不变
    r = client.post(f"/api/v1/windows/{wid}/readings", json=reading("a", 3, 0, 24.0))
    assert r.status_code == 201
    rep2 = client.post(REPORT, json=body).json()
    assert rep1 == rep2
