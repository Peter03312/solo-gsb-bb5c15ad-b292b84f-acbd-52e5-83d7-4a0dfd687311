"""Pydantic 请求/响应模型。"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


def _require_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("时间戳必须带时区信息，例如 2026-10-01T00:00:00Z 或 +08:00")
    return dt


class WindowCreate(BaseModel):
    """创建审定窗口。

    - allowed_temp_min/max：允许温度区间（闭区间，单位 °C）
    - max_continuous_exceed_seconds：最长连续超限时长（秒，超过它判不合格）
    - max_sample_interval_seconds：最大采样间隔（秒，超过则相邻读数间记为未知）
    """

    start_at: datetime = Field(..., description="窗口起始时间（含），带时区")
    end_at: datetime = Field(..., description="窗口结束时间（读数提交含该时刻；输出区段为半开），带时区")
    allowed_temp_min: float = Field(..., description="允许温度下限（含），°C")
    allowed_temp_max: float = Field(..., description="允许温度上限（含），°C")
    max_continuous_exceed_seconds: float = Field(..., gt=0, description="最长连续超限时长，秒")
    max_sample_interval_seconds: float = Field(..., gt=0, description="最大采样间隔，秒")
    name: Optional[str] = Field(None, max_length=200, description="可选备注名，如展柜编号")

    @field_validator("start_at", "end_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        return _require_aware(v)

    @field_validator("allowed_temp_max")
    @classmethod
    def _temp_range(cls, v: float, info: Any) -> float:
        if math.isnan(v) or math.isinf(v):
            raise ValueError("温度必须为有限数值")
        return v

    @field_validator("allowed_temp_min")
    @classmethod
    def _temp_min_finite(cls, v: float) -> float:
        if math.isnan(v) or math.isinf(v):
            raise ValueError("温度必须为有限数值")
        return v


class ReadingSubmit(BaseModel):
    """提交（或修订）一条温度读数。

    幂等与修订规则（按 同一窗口 + reading_id）：
    - revision 已存在：内容（时间戳/温度）完全一致 -> 幂等返回；
      内容不一致 -> 409 冲突。
    - revision 高于当前 -> 替换当前读数并重算；
    - revision 低于当前 -> 409 拒绝（旧修订号不得覆盖新修订号）。
    """

    reading_id: str = Field(..., min_length=1, max_length=100, description="读数 ID（窗口内唯一）")
    revision: int = Field(..., ge=1, description="修订号，从 1 开始递增")
    measured_at: datetime = Field(..., description="读数时间戳，带时区")
    temperature: float = Field(..., description="温度，°C")

    @field_validator("measured_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        return _require_aware(v)

    @field_validator("temperature")
    @classmethod
    def _finite(cls, v: float) -> float:
        if math.isnan(v) or math.isinf(v):
            raise ValueError("温度必须为有限数值")
        return v


class IssueRequest(BaseModel):
    note: Optional[str] = Field(None, max_length=500, description="签发备注")


class SegmentOut(BaseModel):
    start: str
    end: str
    status: str
    duration_seconds: float


class EvaluationOut(BaseModel):
    verdict: str
    normal_segments: list[SegmentOut]
    exceeded_segments: list[SegmentOut]
    unknown_segments: list[SegmentOut]
    max_continuous_exceeded_seconds: float
    max_exceed_seconds: float
    has_exceeded: bool
    has_unknown: bool
    reading_count: int


class ReadingOut(BaseModel):
    reading_id: str
    revision: int
    measured_at: str
    temperature: float
    status: str
    created_at: str


class WindowOut(BaseModel):
    id: int
    name: Optional[str]
    start_at: str
    end_at: str
    allowed_temp_min: float
    allowed_temp_max: float
    max_continuous_exceed_seconds: float
    max_sample_interval_seconds: float
    created_at: str
    current_evaluation: Optional[EvaluationOut] = None
    active_readings: list[ReadingOut] = []
    latest_certificate_version: Optional[int] = None
    latest_verdict: Optional[str] = None


class ReadingSubmitResponse(BaseModel):
    effect: str = Field(..., description="created | updated | idempotent")
    reading: ReadingOut
    current_evaluation: EvaluationOut


class CertificateOut(BaseModel):
    id: int
    window_id: int
    version: int
    verdict: str
    verdict_label_zh: str
    issued_at: str
    note: Optional[str]
    snapshot: dict[str, Any]


# ------------------------------------------------ 修订影响报告

class RevisionReportRequest(BaseModel):
    """对同一窗口的两张已签发证书生成修订影响报告。

    old_certificate_id 必须是早于 new_certificate_id 签发的旧版本；
    报告只依据两张证书的不可变快照，不读取当前活动读数。
    """

    old_certificate_id: int = Field(..., ge=1, description="旧版证书 ID（先签发）")
    new_certificate_id: int = Field(..., ge=1, description="新版证书 ID（后签发）")


class ReadingRefOut(BaseModel):
    reading_id: Optional[str]
    revision: Optional[int]
    measured_at: Optional[str] = None
    temperature: Optional[float] = None


class SegmentEvidenceOut(BaseModel):
    kind: str = Field(..., description="sample | sample_gap | boundary_gap")
    from_reading: Optional[ReadingRefOut]
    to_reading: Optional[ReadingRefOut]
    unknown_reason_code: Optional[str] = Field(
        None, description="boundary_gap | sample_gap | no_readings（仅未知区段）")
    unknown_reason_zh: Optional[str]


class SegmentSideOut(BaseModel):
    status: str
    status_zh: str
    evidence: SegmentEvidenceOut


class ChangedSegmentOut(BaseModel):
    start: str
    end: str
    duration_seconds: float
    before: SegmentSideOut
    after: SegmentSideOut


class AddedOrRemovedReadingOut(BaseModel):
    reading_id: str
    revision: int
    measured_at: str
    temperature: float


class ReadingValueOut(BaseModel):
    revision: int
    measured_at: str
    temperature: float


class ChangedReadingOut(BaseModel):
    reading_id: str
    before: ReadingValueOut
    after: ReadingValueOut
    revision_changed: bool
    measured_at_changed: bool
    temperature_changed: bool


class ReadingDiffOut(BaseModel):
    added_readings: list[AddedOrRemovedReadingOut]
    changed_readings: list[ChangedReadingOut]
    removed_readings: list[AddedOrRemovedReadingOut]
    unchanged_reading_ids: list[str]
    counts: dict[str, int]


class ReportSummaryOut(BaseModel):
    verdict_changed: bool
    verdict_before: str
    verdict_after: str
    verdict_before_zh: str
    verdict_after_zh: str
    max_continuous_exceeded_before_seconds: float
    max_continuous_exceeded_after_seconds: float
    max_continuous_exceeded_delta_seconds: float
    max_exceed_seconds: float
    changed_segment_count: int
    status_transition_counts: dict[str, int]
    changed_area_status_seconds_before: dict[str, float]
    changed_area_status_seconds_after: dict[str, float]
    conclusions_zh: list[str]


class RevisionImpactReportOut(BaseModel):
    window: dict[str, Any]
    old_certificate: dict[str, Any]
    new_certificate: dict[str, Any]
    reading_diff: ReadingDiffOut
    changed_segments: list[ChangedSegmentOut]
    summary: ReportSummaryOut
    basis: dict[str, str]


class ErrorBody(BaseModel):
    detail: str
