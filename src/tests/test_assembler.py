# -*- coding: utf-8 -*-
"""成本明细装配：mc+so 联合匹配——同名功能块跨软件不得互相吸收。"""
from decimal import Decimal as D

from app.engine.assembler import build_cost_details, build_cost_items, build_fp_scale
from app.schemas.contract import FpType, OriginalFp, ToolCode


def _fp(ms: str, mc: str, so: str, name: str) -> OriginalFp:
    return OriginalFp(moduleSystem=ms, moduleConfig=mc, softwareObject=so,
                      requirementName=name, fpType=FpType.EI, fpCount=D(4))


def test_cost_details_scoped_by_config():
    """两个软件各有同名功能块"综合调度"：明细行必须各归各软件（修复前按 so 全局匹配，
    会把两个软件的条目都算进先迭代的 scale，行数翻倍且金额错配）。"""
    tool = ToolCode.COST_MEASUREMENT
    entries = [
        _fp("子系统A", "甲软件", "综合调度", "甲调度条目"),
        _fp("子系统B", "乙软件", "综合调度", "乙调度条目"),
    ]
    scales = build_fp_scale(entries, tool)
    items = build_cost_items(scales)
    details = build_cost_details(entries, scales, items)
    assert {(d.moduleConfig, d.requirementName) for d in details} == {
        ("甲软件", "甲调度条目"), ("乙软件", "乙调度条目")}
    assert len(details) == 2
