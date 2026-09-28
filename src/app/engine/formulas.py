"""公式引擎 —— 全部确定性计算，模型永不产出金额。

依据《智能计价工具-数据计算公式汇总 v3.0》。
单位规则（决议）：内部按万元计算，接口金额输出统一为元（×10000）；
费率类字段（regionRate 等）保持公式文档口径（万元/人月），默认值取原型默认。
精度策略：内部 Decimal 全精度，仅在产出接口字段时 quantize（金额 0.01）。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from app.schemas.contract import FpType, ToolCode

D = Decimal
Q2 = Decimal("0.01")

# 权重表：fpCount（个数）× 权重 → 功能点数
WEIGHTS_ESTIMATION = {FpType.ILF: D(35), FpType.EIF: D(15)}
WEIGHTS_NO4 = {FpType.ILF: D(7), FpType.EIF: D(5), FpType.EI: D(4), FpType.EO: D(5), FpType.EQ: D(4)}

# 原型默认业务参数
DEFAULT_REGION_RATE = D("2.8")     # 地区费率（万元/人月）
DEFAULT_ALLOCATION_FEE = D("0")    # 分摊国拨事业费
DEFAULT_TALENT_SALARY = D("0")     # 高端人才薪资及劳务费（元）
DEFAULT_REUSE_FACTOR = D("1.0")
DEFAULT_FACTORS = [D(0)] * 7       # F1–F7


def weights_for(tool_code: ToolCode) -> dict[FpType, Decimal]:
    return WEIGHTS_ESTIMATION if tool_code == ToolCode.COST_ESTIMATION else WEIGHTS_NO4


def fp_points(tool_code: ToolCode, fp_type: FpType, count: Decimal) -> Decimal:
    w = weights_for(tool_code).get(fp_type)
    if w is None:
        raise ValueError(f"工具 {tool_code.value} 不支持功能点类型 {fp_type.value}")
    return w * count


def adjust_factor(factors: list[Decimal]) -> Decimal:
    """调整因子 = 1.0 + 0.1×(F1+…+F7)，取值范围 1.0~2.4。"""
    if len(factors) != 7:
        raise ValueError("需 7 个调整因子 F1–F7")
    for f in factors:
        if f not in (D(0), D(1), D(2)):
            raise ValueError(f"调整因子取值必须为 0/1/2，收到 {f}")
    return D("1.0") + D("0.1") * sum(factors, D(0))


def adjusted_fp(original: Decimal, reuse: Decimal, adjust: Decimal) -> Decimal:
    if not (D("0.5") <= reuse <= D("1.0")):
        raise ValueError(f"复用因子须在 0.5~1.0，收到 {reuse}")
    return original * reuse * adjust


def hour_rate(original_fp: Decimal) -> Decimal:
    """功能点耗时率（人月/功能点），按原始功能点数分档。"""
    if original_fp <= 1000:
        return D("8.46")
    if original_fp <= 2000:
        return D("6.08")
    if original_fp <= 5000:
        return D("5.27")
    if original_fp <= 10000:
        return D("5.14")
    return D("5.09")


def workload(adjusted: Decimal, original: Decimal) -> Decimal:
    """工作量（人月）=（调整后功能点 × 耗时率）÷ 176。"""
    return adjusted * hour_rate(original) / D(176)


def calc_amount_wan(wl: Decimal, region_rate: Decimal, allocation_fee: Decimal) -> Decimal:
    """测算金额（万元）= 工作量 ×（地区费率 − 分摊国拨事业费）。"""
    return wl * (region_rate - allocation_fee)


def subtotal_wan(amount_wan: Decimal, talent_salary_wan: Decimal) -> Decimal:
    return amount_wan + talent_salary_wan


def to_yuan(amount_wan: Decimal) -> Decimal:
    """万元 → 元（接口单位）。"""
    return (amount_wan * D(10000)).quantize(Q2, rounding=ROUND_HALF_UP)


def outsource_share(unit_fp: Decimal, system_fp: Decimal, system_self_cost_wan: Decimal) -> Decimal:
    """外协单位成本（万元）=（外协单位功能点 ÷ 分系统功能点）× 分系统全自研总成本。"""
    if system_fp == 0:
        return D(0)
    return unit_fp / system_fp * system_self_cost_wan


def q2(x: Decimal) -> Decimal:
    return x.quantize(Q2, rounding=ROUND_HALF_UP)
