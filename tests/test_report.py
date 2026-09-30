"""修订影响报告纯函数测试（不启动 HTTP，只操作证书快照）。

运行：PYTHONPATH=. python -m pytest tests/test_report.py -q
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.evaluation import RawPiece, ReadingRef, evaluate
from app.report import _align_rows, build_revision_impact_report
from app.timeutils import parse_stored

WS = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
WE = WS + timedelta(hours=1)
TEMP_MIN, TEMP_MAX = 18.0, 22.0
MAX_EXCEED, MAX_GAP = 300.0, 600.0


def iso(minute: float) -> str:
    return (WS + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z")


def r(rid: str, rev: int, minute: float, temp: float) -> dict:
    return {"reading_id": rid, "revision": rev,
            "measured_at": iso(minute), "temperature": temp}


def window() -> dict:
    return {
        "id": 1, "name": "case",
        "start_at": iso(0), "end_at": iso(60),
        "allowed_temp_min": TEMP_MIN, "allowed_temp_max": TEMP_MAX,
        "max_continuous_exceed_seconds": MAX_EXCEED,
        "max_sample_interval_seconds": MAX_GAP,
    }


def snapshot(readings: list[dict]) -> dict:
    """模拟签发快照：窗口参数 + 读数 + 按同一规则计算的审定结果。"""
    ev = evaluate(
        WS, WE, TEMP_MIN, TEMP_MAX, MAX_EXCEED, MAX_GAP,
        [{"reading_id": x["reading_id"], "revision": x["revision"],
          "timestamp": parse_stored(x["measured_at"]),
          "temperature": x["temperature"]}
         for x in readings],
    )
    return {"window": window(), "evaluation": ev, "readings": readings}


def cert(cid: int, version: int, snap: dict, issued_at: str | None = None) -> dict:
    return {
        "id": cid, "version": version,
        "window_id": 1,
        "verdict": snap["evaluation"]["verdict"],
        "verdict_label_zh": snap["evaluation"]["verdict"],
        "issued_at": issued_at or iso(90 + version),
        "note": None, "snapshot": snap,
    }


def find(rows, start_min: float, end_min: float):
    s, e = iso(start_min), iso(end_min)
    hit = [x for x in rows if x["start"] == s and x["end"] == e]
    assert hit, f"区间 [{s}, {e}) 不在变化区间中：{[(x['start'], x['end']) for x in rows]}"
    return hit[0]


# ---------------------------------------------------------------- 读数差异

def test_reading_diff_added_changed_unchanged():
    old = snapshot([r("a", 1, 0, 23.0), r("b", 1, 10, 20.0)])
    # a 修订温度；c..h 新增补齐整窗；b 未变
    new_readings = [r("a", 2, 0, 20.0), r("b", 1, 10, 20.0)]
    new_readings += [r(f"r{m:02d}", 1, m, 20.0) for m in range(20, 61, 10)]
    new = snapshot(new_readings)
    rep = build_revision_impact_report(cert(1, 1, old), cert(2, 2, new))
    diff = rep["reading_diff"]

    assert diff["counts"] == {"old_total": 2, "new_total": 7,
                              "added": 5, "changed": 1, "removed": 0, "unchanged": 1}
    assert [x["reading_id"] for x in diff["added_readings"]] == [
        "r20", "r30", "r40", "r50", "r60"]
    ch = diff["changed_readings"][0]
    assert ch["reading_id"] == "a"
    assert ch["before"]["revision"] == 1 and ch["after"]["revision"] == 2
    assert ch["before"]["temperature"] == 23.0 and ch["after"]["temperature"] == 20.0
    assert ch["revision_changed"] and ch["temperature_changed"]
    assert not ch["measured_at_changed"]
    assert diff["unchanged_reading_ids"] == ["b"]
    assert diff["removed_readings"] == []


def test_reading_diff_removed_and_moved_timestamp():
    # 删除读数（现有 API 不产生，但快照比较应支持）+ 修订同时移动时间戳
    old = snapshot([r("a", 1, 0, 20.0), r("b", 1, 10, 20.0), r("c", 1, 20, 20.0)])
    new = snapshot([r("a", 2, 5, 20.0), r("c", 1, 20, 20.0)])
    rep = build_revision_impact_report(cert(1, 1, old), cert(2, 2, new))
    diff = rep["reading_diff"]
    assert [x["reading_id"] for x in diff["removed_readings"]] == ["b"]
    ch = {x["reading_id"]: x for x in diff["changed_readings"]}["a"]
    assert ch["measured_at_changed"] and ch["revision_changed"]
    assert not ch["temperature_changed"]
    assert ch["before"]["measured_at"] == iso(0) and ch["after"]["measured_at"] == iso(5)


# ---------------------------------------------------------------- 时间线对齐

def test_exceeded_to_normal_and_boundary_filled():
    old = snapshot([r("a", 1, 0, 23.0), r("b", 1, 10, 20.0)])
    new_readings = [r("a", 2, 0, 20.0), r("b", 1, 10, 20.0)]
    new_readings += [r(f"r{m:02d}", 1, m, 20.0) for m in range(20, 61, 10)]
    new = snapshot(new_readings)
    rep = build_revision_impact_report(cert(1, 1, old), cert(2, 2, new))
    rows = rep["changed_segments"]

    # [00:00,00:10) 超限 -> 正常，证据从 a rev1 变为 a rev2
    row = find(rows, 0, 10)
    assert row["before"]["status"] == "exceeded"
    assert row["after"]["status"] == "normal"
    assert row["before"]["evidence"]["kind"] == "sample"
    assert row["before"]["evidence"]["from_reading"]["reading_id"] == "a"
    assert row["before"]["evidence"]["from_reading"]["revision"] == 1
    assert row["after"]["evidence"]["from_reading"]["revision"] == 2

    # [00:10,01:00) 旧版整段边界未知（证据指向末读 b rev1），新版按 10 分钟切片转正常
    assert find(rows, 10, 20)["before"]["evidence"]["kind"] == "boundary_gap"
    assert find(rows, 50, 60)["before"]["status"] == "unknown"
    assert find(rows, 50, 60)["after"]["status"] == "normal"
    # 未知成因必须是边界空白，而不是采样间隔超限
    for x in rows:
        if x["start"] >= iso(10):
            assert x["before"]["evidence"]["unknown_reason_code"] == "boundary_gap"
            assert x["before"]["evidence"]["from_reading"]["reading_id"] == "b"

    # 未变化的半开区间不得出现（不存在其它 before/after 状态相同的切片）
    assert len(rows) == 6


def test_sample_gap_distinguished_from_boundary_gap():
    # 旧版：00:00 与 00:30 间隔 1800s > 600s -> 采样间隔超限未知；尾部边界空白
    old = snapshot([r("a", 1, 0, 20.0), r("b", 1, 30, 20.0)])
    # 新版：中间补两条读数，间隔合法；尾部仍未知（状态不变 -> 不输出）
    new = snapshot([r("a", 1, 0, 20.0), r("m", 1, 10, 20.0),
                    r("n", 1, 20, 20.0), r("b", 1, 30, 20.0)])
    rep = build_revision_impact_report(cert(1, 1, old), cert(2, 2, new))
    rows = rep["changed_segments"]

    row = find(rows, 0, 10)
    assert row["before"]["status"] == "unknown" and row["after"]["status"] == "normal"
    ev = row["before"]["evidence"]
    assert ev["kind"] == "sample_gap"
    assert ev["unknown_reason_code"] == "sample_gap"
    assert ev["from_reading"]["reading_id"] == "a"
    assert ev["to_reading"]["reading_id"] == "b"
    assert "采样间隔超限" in ev["unknown_reason_zh"]
    find(rows, 10, 20)
    find(rows, 20, 30)
    # [00:30,01:00) 两侧都是边界未知 -> 不输出
    assert not [x for x in rows if x["start"] == iso(30)]


def test_no_readings_whole_window_reason():
    # 无任何读数：整窗未知，成因 no_readings（无法区分前后边界空白）
    old = snapshot([])
    new = snapshot([r("a", 1, 0, 20.0), r("b", 1, 10, 20.0)])
    rep = build_revision_impact_report(cert(1, 1, old), cert(2, 2, new))
    rows = rep["changed_segments"]
    row = find(rows, 0, 10)
    assert row["before"]["evidence"]["unknown_reason_code"] == "no_readings"
    assert row["before"]["evidence"]["from_reading"] is None
    # [00:10,01:00) 两侧均未知（旧 no_readings / 新 boundary_gap）-> 状态未变，不输出
    assert not [x for x in rows if x["start"] == iso(10)]


def test_normal_to_exceeded_single_piece_and_summary():
    old = snapshot([r("a", 1, 0, 20.0), r("b", 1, 10, 20.0), r("c", 1, 20, 20.0)])
    new = snapshot([r("a", 1, 0, 20.0), r("b", 2, 10, 23.0), r("c", 1, 20, 20.0)])
    rep = build_revision_impact_report(cert(1, 1, old), cert(2, 2, new))
    rows = rep["changed_segments"]
    assert len(rows) == 1
    row = rows[0]
    assert row["start"] == iso(10) and row["end"] == iso(20)
    assert row["before"]["status"] == "normal" and row["after"]["status"] == "exceeded"
    assert row["before"]["evidence"]["from_reading"]["revision"] == 1
    assert row["after"]["evidence"]["from_reading"]["revision"] == 2
    assert row["before"]["status_zh"] == "正常" and row["after"]["status_zh"] == "超限"

    s = rep["summary"]
    assert s["verdict_changed"]
    assert s["verdict_before"] == "pending" and s["verdict_after"] == "fail"
    assert s["max_continuous_exceeded_before_seconds"] == 0.0
    assert s["max_continuous_exceeded_after_seconds"] == 600.0
    assert s["max_continuous_exceeded_delta_seconds"] == 600.0
    assert s["status_transition_counts"] == {"normal->exceeded": 1}


def test_identical_reissue_has_no_impact():
    # 读数完全相同地再次签发：没有任何状态变化区间
    old = snapshot([r("a", 1, 0, 20.0), r("b", 1, 10, 23.0)])
    new = snapshot([dict(x) for x in old["readings"]])
    rep = build_revision_impact_report(cert(1, 1, old), cert(2, 2, new))
    assert rep["changed_segments"] == []
    assert rep["summary"]["changed_segment_count"] == 0
    assert not rep["summary"]["verdict_changed"]
    assert rep["reading_diff"]["counts"] == {"old_total": 2, "new_total": 2,
                                             "added": 0, "changed": 0,
                                             "removed": 0, "unchanged": 2}
    assert any("完全一致" in c for c in rep["summary"]["conclusions_zh"])


def test_max_continuous_exceeded_decreases():
    # fail（连续超限 1200s） -> 修订中间读数回正常，连续超限位移为仅剩 a 的 600s
    old = snapshot([r("a", 1, 0, 23.0), r("b", 1, 10, 23.0), r("c", 1, 20, 20.0)])
    assert old["evaluation"]["max_continuous_exceeded_seconds"] == 1200.0
    new = snapshot([r("a", 1, 0, 23.0), r("b", 2, 10, 20.0), r("c", 1, 20, 20.0)])
    rep = build_revision_impact_report(cert(1, 1, old), cert(2, 2, new))
    s = rep["summary"]
    assert s["max_continuous_exceeded_before_seconds"] == 1200.0
    assert s["max_continuous_exceeded_after_seconds"] == 600.0
    assert s["max_continuous_exceeded_delta_seconds"] == -600.0
    assert any("缩短" in c for c in s["conclusions_zh"])


# ---------------------------------------------------------------- 区间合并

def test_merge_adjacent_rows_identical_status_and_evidence():
    # 手工构造：旧侧同状态、同证据的相邻两段（切分伪影），新侧一整段 -> 应合并
    a = ReadingRef("a", 1, WS, 20.0)
    b = ReadingRef("b", 1, WS + timedelta(minutes=20), 20.0)
    old_pieces = [
        RawPiece(WS, WS + timedelta(minutes=10), "unknown", "boundary_gap",
                 from_reading=a),
        RawPiece(WS + timedelta(minutes=10), WS + timedelta(minutes=20),
                 "unknown", "boundary_gap", from_reading=a),
    ]
    new_pieces = [
        RawPiece(WS, WS + timedelta(minutes=20), "normal", "sample",
                 from_reading=b),
    ]
    rows = _align_rows(old_pieces, new_pieces)
    assert len(rows) == 1
    assert rows[0]["start"] == iso(0) and rows[0]["end"] == iso(20)
    assert rows[0]["duration_seconds"] == 1200.0
    assert rows[0]["before"]["status"] == "unknown"
    assert rows[0]["after"]["status"] == "normal"
