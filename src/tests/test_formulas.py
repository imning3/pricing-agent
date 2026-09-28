# -*- coding: utf-8 -*-
"""公式引擎回归：以接口文档 SUCCESS 示例为验算基准。"""
from decimal import Decimal

import pytest

from app.engine import formulas as F
from app.schemas.contract import FpType, ToolCode

D = Decimal


def test_weights_by_tool():
    assert F.weights_for(ToolCode.COST_ESTIMATION)[FpType.ILF] == 35
    assert F.weights_for(ToolCode.COST_ESTIMATION)[FpType.EIF] == 15
    w = F.weights_for(ToolCode.NO4_QUOTATION)
    assert (w[FpType.ILF], w[FpType.EIF], w[FpType.EI], w[FpType.EO], w[FpType.EQ]) == (7, 5, 4, 5, 4)


def test_estimation_unsupported_types():
    with pytest.raises(ValueError):
        F.fp_points(ToolCode.COST_ESTIMATION, FpType.EI, D(1))


def test_doc_example_chain():
    """1 个 ILF（四号文）→ originalFp=7 → workload=7×8.46/176=0.33648 → 测算金额=0.95896 万元（文档示例 0.96）。"""
    pts = F.fp_points(ToolCode.NO4_QUOTATION, FpType.ILF, D(1))
    assert pts == 7
    adjust = F.adjust_factor([D(0)] * 7)
    assert adjust == D("1.0")
    adjusted = F.adjusted_fp(pts, D("1.0"), adjust)
    wl = F.workload(adjusted, pts)
    assert abs(wl - D("0.336477")) < D("0.00001")
    amount = F.calc_amount_wan(wl, D("2.85"), D(0))
    assert abs(amount - D("0.958960")) < D("0.00001")
    assert F.to_yuan(amount) == D("9589.60")


def test_adjust_factor_bounds():
    assert F.adjust_factor([D(2)] * 7) == D("2.4")
    with pytest.raises(ValueError):
        F.adjust_factor([D(3)] + [D(0)] * 6)
    with pytest.raises(ValueError):
        F.adjusted_fp(D(10), D("0.3"), D(1))  # 复用因子越界


def test_hour_rate_tiers():
    assert F.hour_rate(D(1000)) == D("8.46")
    assert F.hour_rate(D("1000.01")) == D("6.08")
    assert F.hour_rate(D(5000)) == D("5.27")
    assert F.hour_rate(D(10001)) == D("5.09")


def test_to_yuan_quantize():
    assert F.to_yuan(D("0.9690")) == D("9690.00")
