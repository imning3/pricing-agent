"""S2 相关章节定位：规则粗筛（关键词），目标砍掉 50–70% 无关内容。

真实管线此层叠加"目录级轻量分类"（仅喂章节标题+首行）；当前实现规则版，
LLM 增强位留在 LlmLocator（接入 litellm 后启用）。
"""
from __future__ import annotations

import re

from app.pipeline.markdown_ir import MarkdownIR, Section

_RELEVANT = re.compile(r"功能|性能|接口|数据|模块|需求|研制内容|技术要求|技术指标|用户|业务")
_IRRELEVANT = re.compile(r"引用文件|术语|定义|缩略语|可靠性|维修性|保障性|安全保密|交付|进度|验收|质量保证|标准化|培训|参考资料|编写说明")


def locate(ir: MarkdownIR) -> list[Section]:
    kept: list[Section] = []
    for s in ir.sections:
        if _IRRELEVANT.search(s.path) and not _RELEVANT.search(s.path):
            continue
        if _RELEVANT.search(s.path):
            kept.append(s)
            continue
        # 章节标题不相关时，正文密度足够也保留（功能描述常散落在无编号正文）
        if _RELEVANT.search(s.text[:2000]):
            kept.append(s)
    # 兜底：全被过滤时保留原文，宁多勿漏（识别任务要穷举）
    return kept or ir.sections
