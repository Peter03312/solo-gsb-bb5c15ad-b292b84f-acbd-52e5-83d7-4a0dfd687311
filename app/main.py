"""FastAPI 入口：博物馆展柜温度暴露审定服务。"""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Depends, FastAPI, Query, status

from . import services
from .database import connect, get_conn, init_db, write_transaction
from .models import VERDICT_LABELS_ZH, Verdict
from .schemas import (
    CertificateOut,
    EvaluationOut,
    IssueRequest,
    ReadingOut,
    ReadingSubmit,
    ReadingSubmitResponse,
    WindowCreate,
    WindowOut,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时幂等建表并初始化 WAL
    conn = connect()
    try:
        init_db(conn)
    finally:
        conn.close()
    yield


app = FastAPI(
    title="博物馆展柜温度暴露审定服务",
    version="1.0.0",
    description=(
        "对展柜在给定审定窗口内的温度读数进行暴露审定："
        "输出正常/超限/未知区段与最长连续超限时长，"
        "并支持读数修订、幂等提交、证书版本化签发。"
    ),
    lifespan=lifespan,
)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", tags=["meta"])
def root() -> dict[str, Any]:
    return {
        "service": "museum-case-temperature-audit",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "verdicts": {v.value: VERDICT_LABELS_ZH[v.value] for v in Verdict},
    }


# ---------------------------------------------------------------- 窗口

@app.post("/api/v1/windows", response_model=WindowOut, status_code=status.HTTP_201_CREATED,
          tags=["windows"])
def create_window(
    payload: WindowCreate, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    with write_transaction(conn):
        return services.create_window(conn, payload)


@app.get("/api/v1/windows", response_model=list[WindowOut], tags=["windows"])
def list_windows(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict[str, Any]]:
    return services.list_windows(conn, limit=limit, offset=offset)


@app.get("/api/v1/windows/{window_id}", response_model=WindowOut, tags=["windows"])
def get_window(
    window_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    row = services.get_window_row(conn, window_id)
    return services.window_detail(conn, row)


@app.get("/api/v1/windows/{window_id}/evaluation", response_model=EvaluationOut,
         tags=["windows"])
def get_evaluation(
    window_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    row = services.get_window_row(conn, window_id)
    return services.compute_evaluation(conn, row)


# ---------------------------------------------------------------- 读数

@app.get("/api/v1/windows/{window_id}/readings", response_model=list[ReadingOut],
         tags=["readings"])
def list_readings(
    window_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> list[dict[str, Any]]:
    services.get_window_row(conn, window_id)
    return [services.reading_to_dict(r) for r in services.list_active_readings(conn, window_id)]


@app.post(
    "/api/v1/windows/{window_id}/readings",
    response_model=ReadingSubmitResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["readings"],
)
def submit_reading(
    window_id: int,
    payload: ReadingSubmit,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    # 单个写事务：校验 + 替换/插入 + 基于同一状态立即重算
    with write_transaction(conn):
        win = services.get_window_row(conn, window_id)
        effect, reading = services.submit_reading(conn, window_id, payload)
        evaluation = services.compute_evaluation(conn, win)
        return {"effect": effect, "reading": reading, "current_evaluation": evaluation}


# ---------------------------------------------------------------- 签发

@app.post(
    "/api/v1/windows/{window_id}/certificates/issue",
    response_model=CertificateOut,
    status_code=status.HTTP_201_CREATED,
    tags=["certificates"],
)
def issue_certificate(
    window_id: int,
    payload: Optional[IssueRequest] = None,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    note = payload.note if payload is not None else None
    # BEGIN IMMEDIATE：与读数修订并发时，要么修订已提交（纳入本次签发），
    # 要么修订等待锁（在签发提交后生效，不改变本次快照）。
    with write_transaction(conn):
        return services.issue_certificate(conn, window_id, note)


@app.get("/api/v1/windows/{window_id}/certificates", response_model=list[CertificateOut],
         tags=["certificates"])
def list_certificates(
    window_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> list[dict[str, Any]]:
    return services.list_certificates(conn, window_id)


@app.get("/api/v1/certificates/{certificate_id}", response_model=CertificateOut,
         tags=["certificates"])
def get_certificate(
    certificate_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    return services.get_certificate(conn, certificate_id)
