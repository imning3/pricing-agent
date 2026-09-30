"""结果装配器：功能点条目 → fpScaleList / costItemList / costDetailList。

粒度（契约决议）：fpScaleList 按 softwareObject；costItemList 按 moduleConfig；
costDetailList 按需求条目（含外协归属）。
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from app.engine import formulas as F
from app.schemas.contract import (
    CostDetail,
    CostItem,
    FpScale,
    OriginalFp,
    ToolCode,
)

D = Decimal


def build_fp_scale(entries: list[OriginalFp], tool_code: ToolCode) -> list[FpScale]:
    """按 softwareObject 聚合功能点（fpCount 已是原始功能点数，直接求和），
    套默认因子（复用 1.0、F1–F7 全 0）得出功能规模。"""
    weights = F.weights_for(tool_code)
    grouped: dict[tuple[str, str, str], Decimal] = defaultdict(lambda: D(0))
    for e in entries:
        if e.fpType not in weights:
            continue
        grouped[(e.moduleSystem, e.moduleConfig, e.softwareObject)] += e.fpCount

    scales: list[FpScale] = []
    for (ms, mc, so), original in grouped.items():
        reuse = F.DEFAULT_REUSE_FACTOR
        adjust = F.adjust_factor(F.DEFAULT_FACTORS)
        adjusted = F.adjusted_fp(original, reuse, adjust)
        scales.append(FpScale(
            moduleSystem=ms, moduleConfig=mc, softwareObject=so,
            originalFp=F.q2(original), reuseFactor=reuse,
            f1Criticality=D(0), f2Distributed=D(0), f3Performance=D(0), f4Resource=D(0),
            f5Complex=D(0), f6Reusability=D(0), f7MultiEnv=D(0),
            adjustFactor=adjust, adjustedFp=F.q2(adjusted),
        ))
    return scales


def build_cost_items(scales: list[FpScale]) -> list[CostItem]:
    """按 moduleConfig 聚合功能规模 → 工作量 → 测算金额/小计（元）。"""
    by_config: dict[str, list[FpScale]] = defaultdict(list)
    for s in scales:
        by_config[s.moduleConfig].append(s)

    items: list[CostItem] = []
    for mc, group in by_config.items():
        fp_scale = sum((s.adjustedFp for s in group), D(0))
        original = sum((s.originalFp for s in group), D(0))
        wl = F.workload(fp_scale, original)
        amount_wan = F.calc_amount_wan(wl, F.DEFAULT_REGION_RATE, F.DEFAULT_ALLOCATION_FEE)
        items.append(CostItem(
            moduleConfig=mc,
            fpScale=F.q2(fp_scale),
            workload=F.q2(wl),
            regionRate=F.DEFAULT_REGION_RATE,
            allocationFee=F.DEFAULT_ALLOCATION_FEE,
            calculatedAmount=F.to_yuan(amount_wan),
            talentSalary=F.DEFAULT_TALENT_SALARY,
            subtotal=F.to_yuan(F.subtotal_wan(amount_wan, D(0))),  # talentSalary 默认 0 元
        ))
    return items


def build_cost_details(entries: list[OriginalFp], scales: list[FpScale],
                       items: list[CostItem]) -> list[CostDetail]:
    """成本明细：配置项小计 → 按对象规模占比分摊 → 对象内条目均摊。外协归属暂全自研。

    分摊口径：对象成本 = 配置项小计 ×（对象原始功能点 ÷ 配置项原始功能点）。
    外协归属依赖 S3/S4 识别的外协单位信息，真实管线接入后替换 outsource 字段。
    """
    details: list[CostDetail] = []
    by_config: dict[str, list[FpScale]] = defaultdict(list)
    for s in scales:
        by_config[s.moduleConfig].append(s)

    for mc, group in by_config.items():
        config_subtotal = next((i.subtotal for i in items if i.moduleConfig == mc), D(0))
        config_fp = sum((s.originalFp for s in group), D(0))
        if config_fp == 0:
            continue
        for s in group:
            obj_cost = F.q2(config_subtotal * (s.originalFp / config_fp))
            # mc+so 联合匹配：不同软件下同名功能块不得互相吸收（条目重复/金额错配）
            obj_entries = [e for e in entries
                           if e.moduleConfig == s.moduleConfig and e.softwareObject == s.softwareObject]
            if not obj_entries:
                continue
            per = F.q2(obj_cost / len(obj_entries))
            for e in obj_entries:
                details.append(CostDetail(
                    moduleSystem=s.moduleSystem, moduleConfig=s.moduleConfig,
                    softwareObject=s.softwareObject, requirementName=e.requirementName,
                    cost=per, outsourceUnitCost=None, outsourceUnit=None,
                ))
    return details
