"""温度暴露审定核心算法（纯函数，无 IO，可独立测试）。

区段覆盖语义（对应需求原文）：
- 读数按时间排序，每条读数只“覆盖”到下一条读数的时刻为止；
  区间采用半开区间 [t_i, t_{i+1})，该区间内温度取读数 i 的温度
  （采样保持，直至下一条读数）。
- 相邻读数间隔超过“最大采样间隔”时，该区间记为未知。
- 窗口边界无读数的区段（窗口起点到首条读数、末条读数之后到窗口终点）
  记为未知。读数提交的窗口为闭区间 [start, end]：终点时刻读数仅作为
  “覆盖终止采样”，自身区段时长为 0。因此首读恰在起点、末读恰在终点、
  间隔均合法且温度全部正常时，可得到无未知区段的合格结论。

判定：
- 最长连续超限时长“超过”限值 -> 不合格（fail）；刚好等于限值不算超过。
- 否则只要存在未知区段 -> 待定（pending）。
- 其余 -> 合格（pass）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional, Sequence

from .models import Verdict


@dataclass(frozen=True)
class Segment:
    start: datetime
    end: datetime
    status: str  # "normal" | "exceeded" | "unknown"

    def duration(self) -> float:
        return (self.end - self.start).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        from .timeutils import to_iso

        return {
            "start": to_iso(self.start),
            "end": to_iso(self.end),
            "status": self.status,
            "duration_seconds": round(self.duration(), 3),
        }


@dataclass(frozen=True)
class ReadingRef:
    """一条读数的不可变引用（修订影响报告中作为区段证据使用）。"""

    reading_id: Optional[str]
    revision: Optional[int]
    timestamp: datetime
    temperature: float


@dataclass(frozen=True)
class RawPiece:
    """未合并的原始区段：除状态外还携带“该状态由哪条读数支撑”的证据。

    kind：
    - "sample"       : 读数 i 采样保持覆盖 [t_i, t_{i+1})，from_reading 为读数 i；
    - "sample_gap"   : 相邻读数间隔严格大于 T_gap，整段未知；
                       from_reading / to_reading 为间隔两侧读数；
    - "boundary_gap" : 窗口边界无读数区域（起点->首读 / 末读->终点 / 完全无读数）；
                       有读数时 to_reading（首读）或 from_reading（末读）指向边界读数，
                       无任何读数时两者均为 None。
    """

    start: datetime
    end: datetime
    status: str  # "normal" | "exceeded" | "unknown"
    kind: str    # "sample" | "sample_gap" | "boundary_gap"
    from_reading: Optional[ReadingRef] = None
    to_reading: Optional[ReadingRef] = None


@dataclass(frozen=True)
class _Reading:
    reading_id: Optional[str]
    revision: Optional[int]
    timestamp: datetime
    temperature: float


def _normalize_readings(
    readings: Sequence[tuple[datetime, float] | dict[str, Any]],
) -> list[_Reading]:
    """归一化读数输入：支持 (timestamp, temperature) 或含
    timestamp/measured_at、temperature 及可选 reading_id/revision 的 dict。"""
    normalized: list[_Reading] = []
    for item in readings:
        if isinstance(item, dict):
            normalized.append(
                _Reading(
                    reading_id=item.get("reading_id"),
                    revision=item.get("revision"),
                    timestamp=item.get("measured_at", item.get("timestamp")),
                    temperature=float(item["temperature"]),
                )
            )
        else:
            normalized.append(
                _Reading(reading_id=None, revision=None,
                         timestamp=item[0], temperature=float(item[1]))
            )
    normalized.sort(key=lambda r: r.timestamp)
    return normalized


def _as_ref(r: _Reading) -> ReadingRef:
    return ReadingRef(
        reading_id=r.reading_id,
        revision=r.revision,
        timestamp=r.timestamp,
        temperature=r.temperature,
    )


def build_raw_pieces(
    window_start: datetime,
    window_end: datetime,
    readings: Sequence[_Reading],
    max_sample_interval: float,
    temp_min: float,
    temp_max: float,
) -> list[RawPiece]:
    """按覆盖语义生成首尾相接、不重叠的原始区段（含证据）。

    审定与修订影响报告共用本函数，保证两版时间线与既有审定结果使用
    完全相同的区段切分规则。readings 需已按时间升序排列。
    """
    pieces: list[RawPiece] = []

    if not readings:
        # 无任何有效读数：整窗未知，且无法区分是哪种边界空白——
        # 窗口起点到首读、末读到终点同时存在（首读/末读均不存在）。
        return [RawPiece(window_start, window_end, "unknown", "boundary_gap")]

    first = readings[0]
    # 窗口起点 -> 首条读数：边界无读数，未知
    if first.timestamp > window_start:
        pieces.append(RawPiece(
            window_start, first.timestamp, "unknown", "boundary_gap",
            to_reading=_as_ref(first),
        ))

    for i, cur in enumerate(readings):
        if i + 1 >= len(readings):
            break
        nxt = readings[i + 1]
        gap = (nxt.timestamp - cur.timestamp).total_seconds()
        if gap > max_sample_interval:
            # 间隔超过上限：整个相邻读数区间未知（采样间隔超限）
            pieces.append(RawPiece(
                cur.timestamp, nxt.timestamp, "unknown", "sample_gap",
                from_reading=_as_ref(cur), to_reading=_as_ref(nxt),
            ))
        elif cur.temperature < temp_min or cur.temperature > temp_max:
            pieces.append(RawPiece(
                cur.timestamp, nxt.timestamp, "exceeded", "sample",
                from_reading=_as_ref(cur),
            ))
        else:
            pieces.append(RawPiece(
                cur.timestamp, nxt.timestamp, "normal", "sample",
                from_reading=_as_ref(cur),
            ))

    last = readings[-1]
    # 末条读数 -> 窗口终点：边界无读数（读数只覆盖到下一条读数），未知
    if last.timestamp < window_end:
        pieces.append(RawPiece(
            last.timestamp, window_end, "unknown", "boundary_gap",
            from_reading=_as_ref(last),
        ))

    return pieces


def _merge_adjacent(segments: Iterable[Segment | RawPiece]) -> list[Segment]:
    """合并相邻且状态相同的区段。"""
    merged: list[Segment] = []
    for seg in segments:
        if (seg.end - seg.start).total_seconds() <= 0:
            continue
        if merged and merged[-1].status == seg.status and merged[-1].end == seg.start:
            merged[-1] = Segment(merged[-1].start, seg.end, seg.status)
        else:
            merged.append(Segment(seg.start, seg.end, seg.status))
    return merged


def _max_continuous_exceeded(segments: Sequence[Segment]) -> float:
    """最长连续超限时长：相邻 exceeded 区段时长之和，被其它状态打断则重新累计。"""
    longest = 0.0
    current = 0.0
    for seg in segments:
        if seg.status == "exceeded":
            current += seg.duration()
            longest = max(longest, current)
        else:
            current = 0.0
    return longest


def evaluate(
    window_start: datetime,
    window_end: datetime,
    temp_min: float,
    temp_max: float,
    max_exceed_seconds: float,
    max_sample_interval: float,
    readings: Sequence[tuple[datetime, float] | dict[str, Any]],
) -> dict[str, Any]:
    """执行审定计算。

    readings: 已排序的 (timestamp, temperature) 序列，或含
              timestamp/temperature 键的 dict 序列。
    返回包含 normal/exceeded/unknown 区段、最长连续超限时长与结论的 dict。
    """
    normalized = _normalize_readings(readings)

    raw = build_raw_pieces(
        window_start, window_end, normalized,
        max_sample_interval, temp_min, temp_max,
    )
    segments = _merge_adjacent(raw)

    normal = [s for s in segments if s.status == "normal"]
    exceeded = [s for s in segments if s.status == "exceeded"]
    unknown = [s for s in segments if s.status == "unknown"]

    max_continuous = round(_max_continuous_exceeded(segments), 3)
    has_exceeded = bool(exceeded)
    has_unknown = bool(unknown)

    # “超过”才不合格：严格大于
    if max_continuous > max_exceed_seconds:
        verdict = Verdict.FAIL.value
    elif has_unknown:
        verdict = Verdict.PENDING.value
    else:
        verdict = Verdict.PASS.value

    return {
        "verdict": verdict,
        "normal_segments": [s.to_dict() for s in normal],
        "exceeded_segments": [s.to_dict() for s in exceeded],
        "unknown_segments": [s.to_dict() for s in unknown],
        "max_continuous_exceeded_seconds": max_continuous,
        "max_exceed_seconds": float(max_exceed_seconds),
        "has_exceeded": has_exceeded,
        "has_unknown": has_unknown,
        "reading_count": len(normalized),
    }
