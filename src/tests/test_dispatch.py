# -*- coding: utf-8 -*-
"""编排路由：首轮 conversation（带 files 无历史功能点）→ 分析管线。"""
import pytest

from app.agents.orchestrator import make_executor
from app.core.config import Settings
from app.schemas.contract import FileCategory, LlmFileInfo, LlmRequest, TaskType, ToolCode


def _req(t: TaskType, files=True, fp=False) -> LlmRequest:
    return LlmRequest(
        calculationId="t-dispatch", type=t, projectName="P", toolCode=ToolCode.NO4_QUOTATION,
        files=[LlmFileInfo(fileName="a.docx", fileType="docx",
                           category=FileCategory.TECH_REQ, url="/x/a.docx")] if files else [],
        originalFpList=[] if not fp else [
            # 一条合法历史功能点（对话轮次）
        ],
    )


@pytest.mark.asyncio
async def test_conversation_with_files_runs_analysis():
    """抓包场景：type=conversation + files + 无历史列表 → 触发分析（mock 管线出功能点）。"""
    ex = make_executor(Settings(llm_backend="mock"))
    resp = await ex(_req(TaskType.CONVERSATION, files=True, fp=False))
    assert len(resp.originalFpList) > 0            # 走了分析管线
    assert len(resp.fpScaleList) > 0


@pytest.mark.asyncio
async def test_conversation_without_files_stays_chat():
    """type=conversation 且无 files（纯对话/带历史列表）→ 保持对话分支（无功能点输入时仅文本）。"""
    ex = make_executor(Settings(llm_backend="mock"))
    resp = await ex(_req(TaskType.CONVERSATION, files=False, fp=False))
    assert resp.originalFpList == [] and resp.content


@pytest.mark.asyncio
async def test_analysis_always_analysis():
    ex = make_executor(Settings(llm_backend="mock"))
    resp = await ex(_req(TaskType.ANALYSIS, files=True))
    assert len(resp.originalFpList) > 0
