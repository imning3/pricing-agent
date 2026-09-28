# -*- coding: utf-8 -*-
"""S5 垃圾条目过滤与去重规则。"""
from decimal import Decimal

from app.pipeline.extractor import RawFpEntry
from app.pipeline.validator import is_junk, validate_and_merge
from app.schemas.contract import FpType, ToolCode

D = Decimal


def _e(name: str, obj: str = "对象A", desc: str | None = None, fp: FpType = FpType.EI) -> RawFpEntry:
    return RawFpEntry("系统", "配置项", obj, name, fp, D(1), desc)


def test_generic_name_junk():
    assert is_junk(_e("其他功能", "其他功能"))
    assert is_junk(_e("其他"))
    assert is_junk(_e("概述", "系统概述"))
    assert not is_junk(_e("图元类别维护", "图元管理功能（FR_1.1）"))


def test_chapter_self_reference_junk():
    assert is_junk(_e("内部接口需求", "内部接口需求", "内部接口需求章节，整体描述系统内部模块间接口"))
    assert not is_junk(_e("数据中台实时数据接入", "外部接口需求", "由外部系统维护，仅引用"))


def test_merge_dedup_and_type_filter():
    entries = [
        _e("功能A", desc="d1"),
        _e("功能A", desc="d2"),                      # 重复 → 去重
        _e("其他功能", "其他功能"),                     # 垃圾 → 过滤
        _e("外部输入X", fp=FpType.EQ),
    ]
    out = validate_and_merge(entries, ToolCode.NO4_QUOTATION)
    assert len(out) == 2
    # 经费估算：仅 ILF/EIF 合法，其余类型剔除
    out2 = validate_and_merge(
        [_e("数据管理", fp=FpType.ILF), _e("录入", fp=FpType.EI)], ToolCode.COST_ESTIMATION
    )
    assert len(out2) == 1 and out2[0].fpType == FpType.ILF


def test_fpcount_is_weighted_points():
    """fpCount 口径（2026-09-28 定标）：原始功能点数 = 标准权重 × 个数，代码折算。"""
    out = validate_and_merge([_e("用户信息", fp=FpType.ILF), _e("查询", fp=FpType.EQ)],
                             ToolCode.NO4_QUOTATION)
    assert out[0].fpCount == 7 and out[1].fpCount == 4          # ILF=7 / EQ=4
    out2 = validate_and_merge([_e("用户信息", fp=FpType.ILF)], ToolCode.COST_ESTIMATION)
    assert out2[0].fpCount == 35                                 # 经费估算 ILF=35
    out3 = validate_and_merge([RawFpEntry("s", "c", "o", "批量导入", FpType.EI, D(3))],
                              ToolCode.NO4_QUOTATION)
    assert out3[0].fpCount == 12                                 # EI=4 × 3 个


def test_fp_scale_sums_fpcount():
    """估算对象原始功能点 = Σ fpCount 简单求和（口径定标后 assembler 不再做权重乘法）。"""
    from app.engine.assembler import build_fp_scale
    fps = validate_and_merge(
        [_e("用户信息", fp=FpType.ILF, obj="对象X"), _e("录入", fp=FpType.EI, obj="对象X")],
        ToolCode.NO4_QUOTATION)
    scales = build_fp_scale(fps, ToolCode.NO4_QUOTATION)
    assert scales[0].originalFp == 11  # 7 + 4
    assert scales[0].adjustedFp == 11  # 默认因子 1.0
