"""S3 估算对象切分：章节树 → 三级结构映射。

映射规则（v2，2026-09-28 API 实测教训重构）：
- moduleSystem（分系统）= **项目名**（调用方传入；单文档=单项目场景最合理，不再用章节名）；
- 过滤"分组章节"（需求/能力需求（FR）/总体设计/技术要求/xx架构…）——它们是分类容器不是软件；
- moduleConfig（配置项）= 有效层级的**第一层**（如 登录功能（FR_1）、指挥监控应用子系统）；
- softwareObject（估算对象）= 有效层级的**最后一层**（叶子功能块）；
- 有效层级为空（纯分组章正文）→ config="默认配置项"，object=原叶子名兜底。

多文档总体方案场景（R-MASTER-1 顶层软件识别）由编排层按文件归属另行处理，本模块只管单文档。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.pipeline.markdown_ir import MarkdownIR, Section

# 分组章节特征：以分类词结尾（含 （FR）/（XN）尾缀剥离后判断）
_GROUP_TAIL = re.compile(
    r"(需求|要求|设计|架构|组成|概述|范围|文档|原则|目标|部署|选型|环境|因素|方法|状态)$"
)
_ID_SUFFIX = re.compile(r"（[A-Z]{1,4}）$")


def _is_group(name: str) -> bool:
    base = _ID_SUFFIX.sub("", name.strip())
    return bool(_GROUP_TAIL.search(base))


@dataclass
class Segment:
    moduleSystem: str
    moduleConfig: str
    softwareObject: str
    content: str
    path: str


def segment(ir: MarkdownIR, sections: list[Section], project_name: str = "未命名系统") -> list[Segment]:
    segs: list[Segment] = []
    for s in sections:
        parts = [p for p in (s.path.split("/") if s.path else []) if p and not _is_group(p)]
        if parts:
            module_config = parts[0]
            software_object = parts[-1]
        else:
            module_config = "默认配置项"
            raw = [p for p in (s.path.split("/") if s.path else []) if p]
            software_object = raw[-1] if raw else "默认估算对象"
        if s.text.strip():
            segs.append(Segment(
                moduleSystem=project_name, moduleConfig=module_config,
                softwareObject=software_object, content=s.text, path=s.path,
            ))
    # 无任何可用分段 → 单段兜底
    if not segs:
        all_text = "\n".join(t.text for t in ir.sections) or "(空文档)"
        segs.append(Segment(project_name, "默认配置项", "默认估算对象", all_text, "(正文)"))
    return segs
