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


@pytest.mark.asyncio
async def test_extract_parallel_reports_failures():
    """全部段落模型调用失败 → failures 如实上报（编排层据此报 LLM 根因，勿误报文档问题）。

    2026-09-29 实测教训：模型名错（gpt-4o-mini 落默认值）被报成"未能识别功能点"。"""
    from app.agents.orchestrator import _extract_parallel
    from app.pipeline.segmenter import Segment

    class Boom:
        async def extract(self, segs, tool, mode="spec"):
            raise RuntimeError("Invalid model name passed in model=gpt-4o-mini")

    seg = Segment("系统", "配置A", "对象A", "内容", "(正文)")
    entries, failures = await _extract_parallel(Boom(), [seg], "NO4_AUDIT")
    assert entries == []
    assert len(failures) == 1 and "gpt-4o-mini" in str(failures[0][1])

    class Ok:
        async def extract(self, segs, tool, mode="spec"):
            return ["entry"]

    entries2, failures2 = await _extract_parallel(Ok(), [seg], "NO4_AUDIT")
    assert entries2 == ["entry"] and failures2 == []
