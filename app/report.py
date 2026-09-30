"""证书修订影响报告（纯函数，只依据两张证书的不可变快照）。

输入同一审定窗口的旧版、新版两张已签发证书，输出：

1. 读数差异：按 (reading_id, revision, measured_at, temperature) 比较
   两版快照读数，列出新增读数 / 变更读数（含变更前后值）/ 删除读数；
2. 时间线对齐：把两版审定时间线按“所有区段边界”（窗口起止 + 两版
   全部原始区段边界）切成半开区间对齐，仅输出状态发生变化的区间；
   每个区间给出变化前后状态，以及各侧状态所依据的读数 ID 与修订号；
3. 未知成因：无有效读数时区分“边界空白”与“采样间隔超限”；窗口完全
   无读数时两种边界空白同时存在，标记为 no_readings；
4. 汇总结论：结论变化、最长连续超限时长变化、区段统计。

相邻且两侧状态与证据（读数 ID+修订号）均相同的区间会合并。

本模块不读取任何“当前活动读数”，所有结论只来自入参快照；
区段切分复用 evaluation.build_raw_pieces，与既有审定规则同源。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, Sequence

from .evaluation import RawPiece, _normalize_readings, build_raw_pieces
from .models import STATUS_LABELS_ZH, UNKNOWN_REASON_LABELS_ZH, VERDICT_LABELS_ZH
from .timeutils import parse_stored, to_iso

# 温度视为未变化的容差（同一 JSON 数值往返，仅防 float 解析误差）
_TEMP_EPS = 1e-9


# ---------------------------------------------------------------- 快照解析

def _snapshot_readings(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(snapshot.get("readings", []), key=lambda r: (r["measured_at"], r["reading_id"]))


def _snapshot_raw_pieces(snapshot: dict[str, Any]) -> list[RawPiece]:
    """依据快照（窗口参数 + 快照内读数）重建带证据的原始区段。

    规则与签发时 compute_evaluation 使用的 evaluate 完全一致，
    但数据只取自快照而非活动表。
    """
    win = snapshot["window"]
    readings = _normalize_readings(
        [
            {
                "reading_id": r["reading_id"],
                "revision": r["revision"],
                "measured_at": parse_stored(r["measured_at"]),
                "temperature": float(r["temperature"]),
            }
            for r in snapshot.get("readings", [])
        ]
    )
    return build_raw_pieces(
        window_start=parse_stored(win["start_at"]),
        window_end=parse_stored(win["end_at"]),
        readings=readings,
        max_sample_interval=float(win["max_sample_interval_seconds"]),
        temp_min=float(win["allowed_temp_min"]),
        temp_max=float(win["allowed_temp_max"]),
    )


# ---------------------------------------------------------------- 读数差异

@dataclass(frozen=True)
class _ReadingKey:
    reading_id: str
    revision: int


def _reading_index(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["reading_id"]: r for r in snapshot.get("readings", [])}


def _temp_equal(a: float, b: float) -> bool:
    return abs(float(a) - float(b)) <= _TEMP_EPS


def build_reading_diff(
    old_snapshot: dict[str, Any], new_snapshot: dict[str, Any]
) -> dict[str, Any]:
    """比较两版快照读数，返回新增/变更/删除/未变。"""
    old_map = _reading_index(old_snapshot)
    new_map = _reading_index(new_snapshot)

    added: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    unchanged_ids: list[str] = []

    for rid in sorted(new_map):
        nr = new_map[rid]
        if rid not in old_map:
            added.append({
                "reading_id": rid,
                "revision": nr["revision"],
                "measured_at": nr["measured_at"],
                "temperature": float(nr["temperature"]),
            })
            continue
        or_ = old_map[rid]
        ts_changed = or_["measured_at"] != nr["measured_at"]
        temp_changed = not _temp_equal(or_["temperature"], nr["temperature"])
        if or_["revision"] != nr["revision"] or ts_changed or temp_changed:
            changed.append({
                "reading_id": rid,
                "before": {
                    "revision": or_["revision"],
                    "measured_at": or_["measured_at"],
                    "temperature": float(or_["temperature"]),
                },
                "after": {
                    "revision": nr["revision"],
                    "measured_at": nr["measured_at"],
                    "temperature": float(nr["temperature"]),
                },
                "revision_changed": or_["revision"] != nr["revision"],
                "measured_at_changed": ts_changed,
                "temperature_changed": temp_changed,
            })
        else:
            unchanged_ids.append(rid)

    for rid in sorted(old_map):
        if rid not in new_map:
            or_ = old_map[rid]
            removed.append({
                "reading_id": rid,
                "revision": or_["revision"],
                "measured_at": or_["measured_at"],
                "temperature": float(or_["temperature"]),
            })

    return {
        "added_readings": added,
        "changed_readings": changed,
        "removed_readings": removed,
        "unchanged_reading_ids": unchanged_ids,
        "counts": {
            "old_total": len(old_map),
            "new_total": len(new_map),
            "added": len(added),
            "changed": len(changed),
            "removed": len(removed),
            "unchanged": len(unchanged_ids),
        },
    }


# ---------------------------------------------------------------- 时间线对齐

def _piece_at(pieces: Sequence[RawPiece], t: datetime) -> RawPiece:
    """找到覆盖时刻 t（作为区间起点）的原始区段。两侧区段均为 [start,end)。"""
    for p in pieces:
        if p.start <= t < p.end:
            return p
    raise AssertionError(f"时刻 {t} 不在任何原始区段内")  # 理论不可达：切分点同源


def _unknown_reason(piece: RawPiece) -> str:
    if piece.kind == "sample_gap":
        return "sample_gap"
    # boundary_gap：无任何读数时整窗两侧边界空白同时存在，无法区分
    if piece.from_reading is None and piece.to_reading is None:
        return "no_readings"
    return "boundary_gap"


def _ref_dict(ref: Any) -> Optional[dict[str, Any]]:
    if ref is None:
        return None
    return {
        "reading_id": ref.reading_id,
        "revision": ref.revision,
        "measured_at": to_iso(ref.timestamp),
        "temperature": float(ref.temperature),
    }


def _evidence(piece: RawPiece) -> dict[str, Any]:
    """某一侧在某半开区间上的状态证据：读数 ID + 修订号 + 未知成因。"""
    return {
        "kind": piece.kind,
        "from_reading": _ref_dict(piece.from_reading),
        "to_reading": _ref_dict(piece.to_reading),
        "unknown_reason_code": _unknown_reason(piece) if piece.status == "unknown" else None,
        "unknown_reason_zh": (
            UNKNOWN_REASON_LABELS_ZH[_unknown_reason(piece)]
            if piece.status == "unknown" else None
        ),
    }


def _evidence_key(ev: dict[str, Any]) -> tuple:
    """证据等价判定：读数 ID+修订号（采样保持取 from；间隔未知取两侧）+ 成因。"""
    def ref_key(r: Optional[dict[str, Any]]) -> tuple:
        return (None, None) if r is None else (r["reading_id"], r["revision"])

    return (
        ev["kind"],
        ref_key(ev["from_reading"]),
        ref_key(ev["to_reading"]),
        ev["unknown_reason_code"],
    )


def _side(piece: RawPiece) -> dict[str, Any]:
    return {"status": piece.status, "status_zh": STATUS_LABELS_ZH[piece.status],
            "evidence": _evidence(piece)}


def _align_rows(
    old_pieces: Sequence[RawPiece], new_pieces: Sequence[RawPiece]
) -> list[dict[str, Any]]:
    """按两版所有区段边界切分对齐，仅保留状态变化的半开区间；
    相邻且两侧状态与证据均相同的输出区间合并。"""
    boundaries = sorted(
        {p.start for p in old_pieces} | {p.end for p in old_pieces}
        | {p.start for p in new_pieces} | {p.end for p in new_pieces}
    )

    rows: list[dict[str, Any]] = []
    for start, end in zip(boundaries, boundaries[1:]):
        if (end - start).total_seconds() <= 0:
            continue
        op = _piece_at(old_pieces, start)
        np_ = _piece_at(new_pieces, start)
        if op.status == np_.status:
            continue  # 只输出状态发生变化的半开区间
        rows.append({
            "start": to_iso(start),
            "end": to_iso(end),
            "duration_seconds": round((end - start).total_seconds(), 3),
            "before": _side(op),
            "after": _side(np_),
        })

    # 合并相邻且两侧状态与证据均相同的区间（切分边界来自对侧时可能产生）
    merged: list[dict[str, Any]] = []
    for row in rows:
        key = (
            row["before"]["status"], _evidence_key(row["before"]["evidence"]),
            row["after"]["status"], _evidence_key(row["after"]["evidence"]),
        )
        if (merged and merged[-1]["end"] == row["start"]
                and merged[-1]["_key"] == key):
            merged[-1]["end"] = row["end"]
            merged[-1]["duration_seconds"] = round(
                (parse_stored(merged[-1]["end"]) - parse_stored(merged[-1]["start"]))
                .total_seconds(), 3)
        else:
            row = dict(row)
            row["_key"] = key
            merged.append(row)
    for row in merged:
        row.pop("_key", None)
    return merged


def _status_totals(rows: Sequence[dict[str, Any]], side: str) -> dict[str, float]:
    totals: dict[str, float] = {"normal": 0.0, "exceeded": 0.0, "unknown": 0.0}
    for row in rows:
        totals[row[side]["status"]] += row["duration_seconds"]
    return {k: round(v, 3) for k, v in totals.items()}


# ---------------------------------------------------------------- 报告主入口

def build_revision_impact_report(
    old_cert: dict[str, Any], new_cert: dict[str, Any]
) -> dict[str, Any]:
    """根据旧版、新版两张证书（含 snapshot）生成修订影响报告。

    调用方负责完成窗口一致性、版本先后、证书存在性校验。
    """
    old_snap = old_cert["snapshot"]
    new_snap = new_cert["snapshot"]
    win = new_snap["window"]

    reading_diff = build_reading_diff(old_snap, new_snap)
    old_pieces = _snapshot_raw_pieces(old_snap)
    new_pieces = _snapshot_raw_pieces(new_snap)
    changed_segments = _align_rows(old_pieces, new_pieces)

    old_eval = old_snap["evaluation"]
    new_eval = new_snap["evaluation"]
    old_max = float(old_eval["max_continuous_exceeded_seconds"])
    new_max = float(new_eval["max_continuous_exceeded_seconds"])

    # 各状态在“发生变化的区间”上的时长汇总（变化前/变化后两侧分别累计）
    before_totals = _status_totals(changed_segments, "before")
    after_totals = _status_totals(changed_segments, "after")

    transition_counts: dict[str, int] = {}
    for row in changed_segments:
        pair = f"{row['before']['status']}->{row['after']['status']}"
        transition_counts[pair] = transition_counts.get(pair, 0) + 1

    if new_max > old_max:
        max_change_zh = (
            f"最长连续超限时长由 {old_max:g}s 增加至 {new_max:g}s，"
            f"增加 {new_max - old_max:g}s（限值 {float(new_eval['max_exceed_seconds']):g}s）"
        )
    elif new_max < old_max:
        max_change_zh = (
            f"最长连续超限时长由 {old_max:g}s 缩短至 {new_max:g}s，"
            f"缩短 {old_max - new_max:g}s（限值 {float(new_eval['max_exceed_seconds']):g}s）"
        )
    else:
        max_change_zh = f"最长连续超限时长不变，仍为 {new_max:g}s"

    c = reading_diff["counts"]
    conclusions: list[str] = [
        f"审定结论由“{VERDICT_LABELS_ZH[old_eval['verdict']]}”"
        f"（{old_eval['verdict']}）变为“{VERDICT_LABELS_ZH[new_eval['verdict']]}”"
        f"（{new_eval['verdict']}）。"
        if old_eval["verdict"] != new_eval["verdict"]
        else f"审定结论未变化，仍为“{VERDICT_LABELS_ZH[new_eval['verdict']]}”"
        f"（{new_eval['verdict']}）。",
        max_change_zh + "。",
        f"读数：新增 {c['added']} 条、变更 {c['changed']} 条、"
        f"删除 {c['removed']} 条、未变 {c['unchanged']} 条。",
        f"状态发生变化的半开区间共 {len(changed_segments)} 段"
        + (f"（转移统计：{('、'.join(f'{k} ×{v}' for k, v in sorted(transition_counts.items())))}）。"
           if transition_counts else "。"),
    ]
    if not changed_segments and c["changed"] == 0 and c["added"] == 0 and c["removed"] == 0:
        conclusions.append("两版快照读数与审定时间线完全一致，修订无实质影响。")

    return {
        "window": {
            "id": win["id"],
            "name": win.get("name"),
            "start_at": win["start_at"],
            "end_at": win["end_at"],
        },
        "old_certificate": {
            "id": old_cert["id"],
            "version": old_cert["version"],
            "verdict": old_cert["verdict"],
            "verdict_label_zh": VERDICT_LABELS_ZH[old_cert["verdict"]],
            "issued_at": old_cert["issued_at"],
        },
        "new_certificate": {
            "id": new_cert["id"],
            "version": new_cert["version"],
            "verdict": new_cert["verdict"],
            "verdict_label_zh": VERDICT_LABELS_ZH[new_cert["verdict"]],
            "issued_at": new_cert["issued_at"],
        },
        "reading_diff": reading_diff,
        "changed_segments": changed_segments,
        "summary": {
            "verdict_changed": old_eval["verdict"] != new_eval["verdict"],
            "verdict_before": old_eval["verdict"],
            "verdict_after": new_eval["verdict"],
            "verdict_before_zh": VERDICT_LABELS_ZH[old_eval["verdict"]],
            "verdict_after_zh": VERDICT_LABELS_ZH[new_eval["verdict"]],
            "max_continuous_exceeded_before_seconds": old_max,
            "max_continuous_exceeded_after_seconds": new_max,
            "max_continuous_exceeded_delta_seconds": round(new_max - old_max, 3),
            "max_exceed_seconds": float(new_eval["max_exceed_seconds"]),
            "changed_segment_count": len(changed_segments),
            "status_transition_counts": transition_counts,
            "changed_area_status_seconds_before": before_totals,
            "changed_area_status_seconds_after": after_totals,
            "conclusions_zh": conclusions,
        },
        "basis": {
            "source": "certificate_snapshots",
            "note": "报告仅依据两张证书签发时固化的不可变快照，不读取当前活动读数。",
        },
    }
