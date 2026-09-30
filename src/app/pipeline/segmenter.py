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

from app.pipeline.markdown_ir import _NON_TITLE_TAIL, MarkdownIR, Section

# 分组章节特征：以分类词结尾（含 （FR）/（XN）尾缀剥离后判断）
_GROUP_TAIL = re.compile(
    r"(需求|要求|设计|架构|组成|概述|范围|文档|原则|目标|部署|选型|环境|因素|方法|状态)$"
)
_ID_SUFFIX = re.compile(r"（[A-Z]{1,4}）$")
_TABLE_REF = re.compile(r"\[TABLE:(\d+)\]")
# 伪标题行特征（无标题文档的正文结构线索）：引导词+编号（"模块1：班组信息"）、
# 编号前缀（"1. xxx"/"一、xxx"）、或有内容佐证的极短无标点行（"页面定义"后随长正文/表格）。
# 注意：无标题语境下允许单级编号当结构（有标题样式的文档不进此分支，不与
# markdown_ir"单级编号判列表项"三件套冲突）。
_PSEUDO_GUIDED = re.compile(r"^(?:模块|功能|单元|子系统|组件|页面|菜单)\s*\d+\s*[:：、.\s]?\s*\S")
_PSEUDO_NUM = re.compile(r"^(?:\d+(?:\.\d+)*|[一二三四五六七八九十]+)\s*[、.．:：]\s*\S")


def _pseudo_head_at(lines: list[str], i: int) -> bool:
    s = lines[i].strip()
    if not s or len(s) > 30 or s.startswith(("[TABLE", "#")):
        return False
    if _NON_TITLE_TAIL.search(s):  # 陈述句/列表项结尾不是标题
        return False
    if _PSEUDO_GUIDED.match(s) or _PSEUDO_NUM.match(s):
        return True
    # 极短无标点行须有内容佐证：后随长正文或表格占位（避免把普通短句切碎）；
    # 误漏/误切由 S3.5 LLM 精修裁决 drop
    if len(s) > 12 or any(ch in s for ch in "，。；：、！？"):
        return False
    nxt = next((lines[j] for j in range(i + 1, len(lines)) if lines[j].strip()), "")
    return bool(nxt) and (nxt.lstrip().startswith("[TABLE") or len(nxt.strip()) >= 20)


def _expand_tables(text: str, tables: list) -> str:
    """把 [TABLE:n] 占位展开为行文本——纯表格驱动文档（无标题结构）的功能定义常全在表格里，
    不展开则模型永远看不到表格内容（2026-09-29 班组看板需规实测：正文 4K 字 + 表格 5K 字全被占位符挡住）。"""
    def _sub(m):
        idx = int(m.group(1))
        if idx >= len(tables):
            return ""
        t = tables[idx]
        lines = []
        if t.header:
            lines.append(" | ".join(c.replace("\n", " ").strip() for c in t.header))
        for row in t.rows:
            lines.append(" | ".join(c.replace("\n", " ").strip() for c in row))
        return "\n".join(lines)
    return _TABLE_REF.sub(_sub, text)


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


def _split_body_section(s: Section) -> list[Section]:
    """"(正文)"兜底段的伪结构初分：伪标题行切块（path 前缀 (正文)/）。

    只产候选——误切/碎片由 S3.5 LLM 精修（hierarchy_mapping）裁决 drop/归并；
    候选太少（<2，无结构可言）或太多（>60，普通行文误判）都放弃切分。"""
    lines = s.text.split("\n")
    heads = [i for i in range(len(lines)) if _pseudo_head_at(lines, i)]
    if not 2 <= len(heads) <= 60:
        return [s]
    bounds = heads + [len(lines)]
    sections: list[Section] = []
    pre = [ln for ln in lines[: heads[0]] if ln.strip()]
    if pre:
        sections.append(Section(path=s.path, level=s.level, text="\n".join(pre)))
    for k in range(len(heads)):
        chunk = [ln for ln in lines[bounds[k] : bounds[k + 1]] if ln.strip()]
        if chunk:
            title = lines[heads[k]].strip()
            sections.append(Section(path=f"{s.path}/{title}", level=s.level + 1,
                                    text="\n".join(chunk)))
    return sections or [s]


def _drop_toc_sections(sections: list[Section]) -> list[Section]:
    """目录特征识别（通用启发式）：文档前部连续的近空章节簇，标题在后续章节路径中
    重复出现 → 判为目录页剔除（目录被解析成半棵树会污染层级映射与拆分清单）。"""
    if len(sections) < 4:
        return sections
    boundary = next((i for i, s in enumerate(sections) if len(s.text.strip()) >= 80), None)
    if boundary is None or boundary < 3:
        return sections
    head = sections[:boundary]
    later = "\n".join(s.path for s in sections[boundary:])
    hits = sum(1 for s in head if s.path.rsplit("/", 1)[-1] and s.path.rsplit("/", 1)[-1] in later)
    if hits >= max(2, len(head) // 2):
        return sections[boundary:]
    return sections


def segment(ir: MarkdownIR, sections: list[Section], project_name: str = "未命名系统") -> list[Segment]:
    sections = _drop_toc_sections(sections)
    # 无标题文档的"(正文)"段先做伪结构初分（2026-09-29 班组看板实测驱动）
    expanded: list[Section] = []
    for s in sections:
        expanded.extend(_split_body_section(s) if s.path == "(正文)" else [s])
    sections = expanded
    segs: list[Segment] = []
    for s in sections:
        parts = [p for p in (s.path.split("/") if s.path else [])
                 if p and p != "(正文)" and not _is_group(p)]
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
                softwareObject=software_object,
                content=_expand_tables(s.text, ir.tables), path=s.path,
            ))
    # 无任何可用分段 → 单段兜底
    if not segs:
        all_text = _expand_tables("\n".join(t.text for t in ir.sections), ir.tables) or "(空文档)"
        segs.append(Segment(project_name, "默认配置项", "默认估算对象", all_text, "(正文)"))
    return segs
