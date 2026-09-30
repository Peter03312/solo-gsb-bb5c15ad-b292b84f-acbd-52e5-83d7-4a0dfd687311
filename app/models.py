"""审定结论与读数状态常量。"""
from __future__ import annotations

from enum import Enum


class Verdict(str, Enum):
    """审定结论。

    判定优先级：
    1. 存在超温且最长连续超限时长超过限值 -> fail（不合格）
    2. 否则存在未知区段                    -> pending（待定）
    3. 否则                              -> pass（合格）
    """

    PASS = "pass"
    PENDING = "pending"
    FAIL = "fail"


VERDICT_LABELS_ZH = {
    Verdict.PASS.value: "合格",
    Verdict.PENDING.value: "待定",
    Verdict.FAIL.value: "不合格",
}


# 区段状态中文标签（审定与修订影响报告共用）
STATUS_LABELS_ZH = {
    "normal": "正常",
    "exceeded": "超限",
    "unknown": "未知",
}


# 未知区段成因中文标签：
# - boundary_gap：窗口边界无读数（窗口起点到首读，或末读到窗口终点）
# - sample_gap  ：相邻读数间隔严格大于最大采样间隔
# - no_readings ：窗口内没有任何有效读数，前后边界空白同时存在，无法进一步区分
UNKNOWN_REASON_LABELS_ZH = {
    "boundary_gap": "边界空白（窗口起点到首条读数，或末条读数到窗口终点无读数）",
    "sample_gap": "采样间隔超限（相邻读数间隔严格大于最大采样间隔）",
    "no_readings": "窗口内无任何有效读数（前后边界空白同时存在）",
}


# reading 状态：每个（窗口, 读数 ID）只有一条 active，历史行保留在 reading_revisions
READING_ACTIVE = "active"
READING_SUPERSEDED = "superseded"
