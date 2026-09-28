"""S5 校验归并：RawFpEntry → OriginalFp 契约对象；去重；类型白名单过滤；垃圾条目过滤。

fpCount 口径（2026-09-28 业务定标）：**原始功能点数 = 标准权重 × 个数**——
模型只判条目与类型（个数默认 1，可不给），权重折算完全在代码：
  - 四号文系（NO4_QUOTATION/REVIEW/COST_MEASUREMENT/NO4_AUDIT）：ILF=7/EIF=5/EI=4/EO=5/EQ=4
  - 经费估算（COST_ESTIMATION）：ILF=35/EIF=15
估算对象原始功能点 = Σ fpCount 简单求和（见 assembler）。

垃圾条目来源（实测）：模型把章节标题本身抽成了一条功能点——
如"其他功能/其他功能"（空泛章节）、"内部接口需求"（章节自引用，描述含"章节，整体描述…"）。
"""
from __future__ import annotations

import re
from decimal import Decimal

from app.engine import formulas as F
from app.pipeline.extractor import RawFpEntry
from app.schemas.contract import FpType, OriginalFp, ToolCode

# 空泛章节名：单独成条无业务含义
_GENERIC_NAME = re.compile(r"^(其他功能?|其他|概述|简介|说明|汇总|合计|要求)$")
# 章节自引用的描述特征
_SELF_REF_DESC = re.compile(r"章节|整体描述|本章描述")
# 章节标题型条目名（功能需求条目极少以"XX需求"结尾命名）
_CHAPTER_TITLE = re.compile(r"(内部|外部)?(接口|数据|能力|性能|适应性|可靠性|安全性|保密性|环境)?需求$")
# 元条目：裸 FR_x 编号、"xx逻辑文件"类自指名称（新 skill 实测出现的垃圾形态）
_BARE_ID = re.compile(r"^(FR|XN|YF|JF)[_-]?\d+(\.\d+)*$", re.IGNORECASE)
_META_NOUN = re.compile(r"(逻辑文件|功能点|五元素|数据实体)$")


def is_junk(e: RawFpEntry) -> bool:
    name = e.requirementName.strip()
    if _GENERIC_NAME.match(name):
        return True
    if _BARE_ID.match(name):
        return True
    if _META_NOUN.search(name):
        return True
    if name == e.softwareObject.strip() and _SELF_REF_DESC.search(e.description or ""):
        return True
    if _CHAPTER_TITLE.match(name) and name == e.softwareObject.strip():
        return True
    return False


def validate_and_merge(entries: list[RawFpEntry], tool_code: ToolCode) -> list[OriginalFp]:
    weights = F.weights_for(tool_code)
    seen: set[tuple[str, str, str, str, str]] = set()
    out: list[OriginalFp] = []
    for e in entries:
        if e.fpType not in weights:
            continue  # 该工具不支持的类型直接剔除（如经费估算下的 EI/EO/EQ）
        if e.fpCount is None or e.fpCount <= 0:
            count = Decimal(1)
        else:
            count = Decimal(e.fpCount)
        if is_junk(e):
            continue
        key = (e.moduleSystem, e.moduleConfig, e.softwareObject, e.requirementName, e.fpType.value)
        if key in seen:
            continue
        seen.add(key)
        out.append(OriginalFp(
            moduleSystem=e.moduleSystem, moduleConfig=e.moduleConfig,
            softwareObject=e.softwareObject, requirementName=e.requirementName,
            fpType=e.fpType,
            fpCount=weights[e.fpType] * count,  # 原始功能点数 = 标准权重 × 个数
            description=e.description,
            auditResult=e.auditResult,
        ))
    return out
