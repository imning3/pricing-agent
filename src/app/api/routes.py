"""API 路由：接口一（异步提交）/ 接口二（查询）。

约定：提交成功恒 200 + PROCESSING；参数错误 400 + errorMsg；任务不存在 200 + status=null。
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request

from app.schemas.contract import (
    LlmRequest,
    QueryRequest,
    QueryResponse,
    SubmitResponse,
    TaskStatus,
)

_EXPIRED_MSG = "任务不存在或已过期"
router = APIRouter(prefix="/api/v1/llm")


def _check_token(request: Request, x_api_token: str | None) -> None:
    expected = request.app.state.settings.api_token
    if expected and x_api_token != expected:
        raise HTTPException(
            status_code=401,
            detail={"errorMsg": "鉴权失败：X-Api-Token 无效或缺失"},
        )


@router.post("/tasks", response_model=SubmitResponse)
async def submit_task(
    req: LlmRequest,
    request: Request,
    x_api_token: str | None = Header(default=None),
):
    _check_token(request, x_api_token)
    status = await request.app.state.task_manager.submit(req)
    return SubmitResponse(calculationId=req.calculationId, status=status)


@router.post("/tasks/query", response_model=QueryResponse)
async def query_task(
    req: QueryRequest,
    request: Request,
    x_api_token: str | None = Header(default=None),
):
    _check_token(request, x_api_token)
    row = request.app.state.task_manager.query(req.calculationId)
    if row is None:
        return QueryResponse(calculationId=req.calculationId, status=None, result=None, errorMsg=_EXPIRED_MSG)
    return QueryResponse(
        calculationId=row.calculationId,
        status=row.status,
        result=row.result,
        errorMsg=row.errorMsg,
    )


@router.get("/health")
async def health():
    return {"status": "ok"}
