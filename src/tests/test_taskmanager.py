# -*- coding: utf-8 -*-
"""任务管理器：幂等 / 状态机 / TTL。"""
import asyncio
from decimal import Decimal

import pytest

from app.schemas.contract import (
    FileCategory,
    LlmFileInfo,
    LlmRequest,
    LlmResponse,
    OriginalFp,
    TaskStatus,
    TaskType,
    ToolCode,
)
from app.tasks.manager import TaskManager


def _req(cid: str = "t1") -> LlmRequest:
    return LlmRequest(
        calculationId=cid, type=TaskType.ANALYSIS, projectName="测试项目",
        toolCode=ToolCode.NO4_QUOTATION,
        files=[LlmFileInfo(fileName="a.docx", fileType="docx",
                           category=FileCategory.TECH_REQ, url="http://x/a.docx")],
    )


def _manager(tmp_path, executor) -> TaskManager:
    return TaskManager(str(tmp_path / "t.db"), workers=1, ttl_hours=24, executor=executor)


@pytest.mark.asyncio
async def test_success_flow(tmp_path):
    async def ok(req: LlmRequest) -> LlmResponse:
        return LlmResponse(content="done")

    m = _manager(tmp_path, ok)
    m.startup()
    await m.start_workers()
    status = await m.submit(_req())
    assert status == TaskStatus.PROCESSING
    for _ in range(100):
        row = m.query("t1")
        if row and row.status == TaskStatus.SUCCESS:
            break
        await asyncio.sleep(0.02)
    assert row.status == TaskStatus.SUCCESS
    assert row.result.content == "done"


@pytest.mark.asyncio
async def test_failed_flow(tmp_path):
    async def boom(req):
        raise RuntimeError("大模型调用超时")

    m = _manager(tmp_path, boom)
    m.startup()
    await m.start_workers()
    await m.submit(_req("t2"))
    for _ in range(100):
        row = m.query("t2")
        if row and row.status == TaskStatus.FAILED:
            break
        await asyncio.sleep(0.02)
    assert row.status == TaskStatus.FAILED
    assert "超时" in row.errorMsg


@pytest.mark.asyncio
async def test_idempotent_submit(tmp_path):
    calls = []

    async def ok(req):
        calls.append(req.calculationId)
        return LlmResponse(content="done")

    m = _manager(tmp_path, ok)
    m.startup()
    await m.start_workers()
    s1 = await m.submit(_req("t3"))
    s2 = await m.submit(_req("t3"))
    assert s1 == s2
    for _ in range(100):
        if len(calls) == 1:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.1)
    assert len(calls) == 1  # 重复提交不重复执行


@pytest.mark.asyncio
async def test_failed_task_resubmit_retries(tmp_path):
    """FAILED 后同 id 重提 = 重试（契约：失败由后端重提），应重新执行而不是回 FAILED。"""
    calls = []

    async def flaky(req):
        calls.append(req.calculationId)
        if len(calls) == 1:
            raise RuntimeError("首次失败")
        return LlmResponse(content="retry ok")

    m = _manager(tmp_path, flaky)
    m.startup()
    await m.start_workers()
    await m.submit(_req("t-retry"))
    for _ in range(100):
        row = m.query("t-retry")
        if row and row.status == TaskStatus.FAILED:
            break
        await asyncio.sleep(0.02)
    assert row.status == TaskStatus.FAILED and len(calls) == 1

    s = await m.submit(_req("t-retry"))          # 同 id 重提
    assert s == TaskStatus.PROCESSING            # 重新受理
    for _ in range(100):
        row = m.query("t-retry")
        if row and row.status == TaskStatus.SUCCESS:
            break
        await asyncio.sleep(0.02)
    assert row.status == TaskStatus.SUCCESS      # 重试成功
    assert len(calls) == 2
    assert row.result.content == "retry ok"


def test_query_missing(tmp_path):
    async def ok(req):
        return LlmResponse()

    m = _manager(tmp_path, ok)
    m.startup()
    assert m.query("nope") is None
