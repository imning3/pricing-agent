# -*- coding: utf-8 -*-
"""S3 拆分双模式：系统模式归组 / 软件模式身份判定+校准 / 降级回退。"""
from pathlib import Path

import pytest

from app.pipeline.markdown_ir import MarkdownIR, Section
from app.pipeline.segmenter import Segment
from app.pipeline.splitter import (
    MODE_BY_TOOL,
    _apply_system,
    _build_system_segments,
    _chunk_text,
    split_spec,
    split_system,
)

SKILLS_DIR = str(Path(__file__).resolve().parent.parent / "app" / "skills")


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    async def chat_json(self, system, user):
        self.calls += 1
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def _ir(sections: list[Section]) -> MarkdownIR:
    return MarkdownIR(fileName="t.docx", sections=sections)


def test_mode_mapping_covers_all_tools():
    assert MODE_BY_TOOL == {
        "COST_ESTIMATION": "system", "NO4_QUOTATION": "system",
        "COST_MEASUREMENT": "system", "NO4_AUDIT": "system",
        "NO4_QUOTATION_REVIEW": "spec",
    }


# ---------- 系统模式 ----------

def _sys_sections() -> list[Section]:
    return [
        Section("概述", 1, "项目概述内容，非功能。"),
        Section("总体设计/某子系统", 2, "子系统组成描述。"),
        Section("总体设计/某子系统/A软件", 3, "A软件功能要求：应提供数据管理功能。" + "填充" * 20),
        Section("总体设计/某子系统/A软件/数据管理", 4, "数据管理详细设计：新增/查询/导出。"),
        Section("总体设计/某子系统/B软件", 3, "B软件功能要求：应提供态势显示功能。"),
        Section("项目管理", 1, "进度与人员安排。"),
    ]


def test_apply_system_mapping_and_skip():
    sections = _sys_sections()
    anchors = [s for s in sections if s.level <= 5]
    mapping = _apply_system(anchors, [
        {"sub": None, "sw": None, "role": "skip", "ranges": "1-2,6"},  # 概述/容器/管理章
        {"sub": "某子系统", "sw": "A软件", "role": "functional", "ranges": "3"},
        {"sub": "某子系统", "sw": "A软件", "role": "design", "ranges": "4"},
        {"sub": "某子系统", "sw": "B软件", "role": "functional", "ranges": "5"},
    ])
    assert mapping is not None
    ir = _ir(sections)
    segs = _build_system_segments(ir, sections, anchors, mapping)
    # A软件：功能章+设计章（含深层继承）归并；B软件独立；概述/容器/管理章被跳过
    by_key = {(s.moduleSystem, s.moduleConfig): s for s in segs}
    assert set(by_key) == {("某子系统", "A软件"), ("某子系统", "B软件")}
    a = by_key[("某子系统", "A软件")]
    assert "A软件功能要求" in a.content and "数据管理详细设计" in a.content
    assert a.softwareObject == "A软件"  # 占位，功能块由抽取阶段判


def test_apply_system_invalid_when_mismatch():
    sections = _sys_sections()
    anchors = [s for s in sections if s.level <= 5]
    assert _apply_system(anchors, [{"sub": "X", "sw": "Y", "role": "functional", "ranges": "99"}]) is None
    # 功能组缺坐标 → 无效项不计覆盖
    assert _apply_system(anchors, [
        {"role": "skip", "ranges": "1"},
        {"sub": "", "sw": "", "role": "functional", "ranges": "3"}]) is None


@pytest.mark.asyncio
async def test_split_system_end_to_end_and_fallback():
    sections = _sys_sections()
    ir = _ir(sections)
    rule = [Segment("P", "默认配置项", "默认估算对象", "兜底", "(正文)")]
    payload = [
        {"sub": None, "sw": None, "role": "skip", "ranges": "1-2,6"},
        {"sub": "某子系统", "sw": "A软件", "role": "functional", "ranges": "3-4"},
        {"sub": "某子系统", "sw": "B软件", "role": "functional", "ranges": "5"},
    ]
    segs, ok = await split_system(FakeClient(payload), ir, sections, rule, SKILLS_DIR)
    assert ok and {(s.moduleSystem, s.moduleConfig) for s in segs} == {
        ("某子系统", "A软件"), ("某子系统", "B软件")}
    # 输出异常 → 整体回退规则初分
    segs2, ok2 = await split_system(FakeClient(ValueError("坏输出")), ir, sections, rule, SKILLS_DIR)
    assert not ok2 and segs2 == rule
    # skill 缺失 → 直接回退且不调用
    c = FakeClient(payload)
    segs3, ok3 = await split_system(c, ir, sections, rule, "/no-skills")
    assert not ok3 and segs3 == rule and c.calls == 0


def test_chunk_text_splits_on_newline():
    text = "\n".join(f"第{i}行" + "字" * 30 for i in range(500))
    chunks = _chunk_text(text, 16000)
    assert len(chunks) > 1 and all(len(c) <= 16000 for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


# ---------- 软件模式 ----------

def _spec_segs() -> list[Segment]:
    return [
        Segment("P", "FR_1", "登录功能", "登录场景描述", "需求/能力需求（FR）/登录功能（FR_1）"),
        Segment("P", "FR_1", "登录功能", "多要素认证描述", "需求/能力需求（FR）/登录功能（FR_1）/认证"),
        Segment("P", "FR_2", "图元管理", "图元维护描述", "需求/能力需求（FR）/图元管理（FR_2）"),
    ]


@pytest.mark.asyncio
async def test_split_spec_identity_items_merge():
    ir = _ir([Section("范围", 1, "本软件是XX系统指挥控制子系统的数据管理软件，" + "背景" * 30)])
    rule = _spec_segs()
    out, ok = await split_spec(FakeClient({
        "subsystem": "指挥控制子系统", "software": "数据管理软件",
        "items": [
            {"id": 1, "softwareObject": "登录功能"},
            {"id": 2, "softwareObject": "登录功能"},   # 同块归并
            {"id": 3, "softwareObject": "图元管理"},
        ],
    }), ir, [], rule, SKILLS_DIR)
    assert ok
    assert all(s.moduleSystem == "指挥控制子系统" and s.moduleConfig == "数据管理软件" for s in out)
    objs = {(s.softwareObject, s.content.count("描述")) for s in out}
    # 登录功能两段归并为一段（内容含两处"描述"）
    assert any(s.softwareObject == "登录功能" and "场景描述" in s.content and "认证描述" in s.content for s in out)
    assert len(out) == 2


@pytest.mark.asyncio
async def test_split_spec_null_identity_keeps_initial():
    ir = _ir([Section("范围", 1, "未提及隶属关系的普通范围说明，" + "背景" * 30)])
    rule = _spec_segs()
    out, ok = await split_spec(FakeClient({
        "subsystem": None, "software": None,
        "items": [{"id": 1, "drop": True}, {"id": 2}, {"id": 3}],
    }), ir, [], rule, SKILLS_DIR)
    assert ok
    assert [s.softwareObject for s in out] == ["登录功能", "图元管理"]  # id1 被 drop
    assert out[0].moduleSystem == "P" and out[0].moduleConfig == "FR_1"  # 身份 null 保持初分


@pytest.mark.asyncio
async def test_split_spec_fallback_on_garbage():
    ir = _ir([])
    rule = _spec_segs()
    out, ok = await split_spec(FakeClient({"items": [{"id": 98}]}), ir, [], rule, SKILLS_DIR)
    assert not ok and out == rule
    out2, ok2 = await split_spec(FakeClient(ValueError("坏")), ir, [], rule, SKILLS_DIR)
    assert not ok2 and out2 == rule
