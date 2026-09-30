# -*- coding: utf-8 -*-
"""S3 拆分（含 LLM）。两模式（toolCode 硬映射，2026-09-29 定标）：

- 系统模式（工具①-④，建设方案/总体方案类）：章节树交 LLM 归组到 (子系统, 软件) 坐标，
  拆到软件（moduleConfig）级；softwareObject（功能块）留给 S4 抽取时 LLM 判；
- 软件模式（工具⑤，需规说明类）：LLM 判文档身份（所属子系统/软件名，从正文内容判断）
  + 功能块校准，拆到功能块（softwareObject）级。

三级字段语义：moduleSystem=子系统、moduleConfig=软件（配置项）、softwareObject=功能块。

通用性原则：skill 只写通用结构判定知识，代码只写普适启发式——不针对具体文档/章节号特化。

可靠性契约：每模式单次 LLM 调用；输出校验失败回喂重试 ≤2 次，仍失败整体回退规则初分
（宁降级不失败）；mock 模式不经过本模块。
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.pipeline.markdown_ir import MarkdownIR, Section
from app.pipeline.segmenter import Segment, _expand_tables

logger = logging.getLogger(__name__)

# 模式硬映射：①-④ 建设方案类 → 系统模式；⑤ 需规类 → 软件模式
MODE_BY_TOOL: dict[str, str] = {
    "COST_ESTIMATION": "system",
    "NO4_QUOTATION": "system",
    "COST_MEASUREMENT": "system",
    "NO4_AUDIT": "system",
    "NO4_QUOTATION_REVIEW": "spec",
}

_ANCHOR_MAX_LEVEL = 5   # 系统模式只映射浅层锚点，深层小节按文档序继承锚点坐标
_ANCHOR_MAX_COUNT = 600  # 锚点过多（超大文档）时放弃 LLM 拆分，回退规则初分
_CHUNK_CHARS = 16000     # 同一软件正文拼接的超长切分段上限（extractor 截断 20000）

_SYSTEM_TASK = """
你是文档结构拆分器，上面是建设方案类文档的层级判定规则。下面给出章节清单（id 连续编号，
只列浅层锚点，更深小节自动继承锚点归属，不必列出）。请按坐标分组返回 JSON 数组，每组：
{"sub": "<子系统名>", "sw": "<软件名>", "role": "functional|design|skip", "ranges": "<id区间，如 12-18,300-315>"}
要求：
1. 所有组的 ranges 并集必须覆盖清单全部 id，不得遗漏（归属不明的章节归入 skip 组）；
2. role：functional=功能需求叙述；design=详细设计/组成叙述；skip=非功能内容
   （概述/依据/管理/进度/保障/配套/结论/风险/标准规范类）——skip 组的 sub/sw 填 null；
3. sub=子系统层，sw=软件（配置项）层：按层级形态学判断而非固定章节位置——子系统标题
   常含"子系统/分系统"，软件是子系统下可交付的软件产品（软件/平台/系统/模块混称时
   按"可独立交付"归一）；同一软件的功能章与设计章给相同 (sub, sw)，代码会拼接正文；
4. sub/sw 用文档原词，跨章同物同名；不确定宁 skip 不编造；
5. 只输出 JSON 数组，不得输出任何其他文字。
"""

_SPEC_TASK = """
你是文档结构拆分器，上面是需求规格说明类文档的拆分规则。下面给出文档头部正文摘录与
规则初分的功能章节清单。请返回一个 JSON 对象：
{"subsystem": "<所属子系统名或 null>", "software": "<软件（配置项）名或 null>",
 "items": [{"id": <原编号>, "softwareObject": "<功能块名>", "drop": false}]}
要求：
1. subsystem/software 从正文内容判断（通常写在范围/系统概述等叙述中），判不出返回 null，
   禁止拿章节标题或项目名硬填；
2. items 覆盖清单全部 id：softwareObject=该章节所属功能块（文档原词，同块同名）；
   drop=true 仅用于无功能正文的章节（模板骨架/术语/可追踪性类），有正文一律归属；
3. 同一功能块的多个下级小节给相同 softwareObject（代码会归并正文）；
4. 不确定时保持初分值（见各条括号内），禁止编造文档中不存在的名称；
5. 只输出 JSON 对象，不得输出任何其他文字。
"""


async def split_system(client, ir: MarkdownIR, sections: list[Section],
                       rule_segs: list[Segment], skills_dir: str) -> tuple[list[Segment], bool]:
    """系统模式：章节树 → (子系统, 软件) 归组，拆到软件级。失败回退规则初分。"""
    anchors = [s for s in sections if s.level <= _ANCHOR_MAX_LEVEL]
    if not anchors or len(anchors) > _ANCHOR_MAX_COUNT:
        return rule_segs, False
    skill = Path(skills_dir) / "split_system" / "SKILL.md"
    if not skill.exists():
        return rule_segs, False
    system = skill.read_text(encoding="utf-8") + "\n" + _SYSTEM_TASK

    def build_user() -> str:
        listing = "\n".join(
            f"{i} | L{s.level} | {s.path} | {len(s.text)}字" for i, s in enumerate(anchors, 1)
        )
        return "章节清单（id | 层级 | path | 正文字数）：\n" + listing

    user = build_user()
    err: object = "未知错误"
    for attempt in range(3):
        try:
            data = await client.chat_json(system, user)
        except Exception as exc:  # noqa: BLE001 —— 拆分失败整体降级，不拖垮任务
            err = exc
        else:
            mapping = _apply_system(anchors, data)
            if mapping is not None:
                segs = _build_system_segments(ir, sections, anchors, mapping)
                if segs:
                    logger.info("系统模式拆分：%d 锚点 → %d 个软件分段", len(anchors), len(segs))
                    return segs, True
                err = "归组结果无可用功能内容"
            else:
                err = "输出与输入清单不匹配（id 须逐条返回）"
        if attempt < 2:
            user = (f"{user}\n\n【修正要求】你上次的输出无法使用（错误：{str(err)[:120]}）。"
                    f"请严格按清单逐条返回原 id 的 JSON 数组，不要输出任何其他文字。")
    logger.warning("系统模式拆分失败，回退规则初分：%s", str(err)[:160])
    return rule_segs, False


async def split_spec(client, ir: MarkdownIR, sections: list[Section],
                     rule_segs: list[Segment], skills_dir: str) -> tuple[list[Segment], bool]:
    """软件模式：文档身份判定 + 功能块校准。失败回退规则初分。"""
    if not rule_segs:
        return rule_segs, False
    skill = Path(skills_dir) / "split_spec" / "SKILL.md"
    if not skill.exists():
        return rule_segs, False
    system = skill.read_text(encoding="utf-8") + "\n" + _SPEC_TASK

    def build_user() -> str:
        listing = "\n".join(
            f"{i} | {s.path} | 正文{len(s.content)}字 | 初分object={s.softwareObject}"
            for i, s in enumerate(rule_segs, 1)
        )
        return (f"【文档头部正文摘录】\n{_identity_excerpt(ir)}\n\n"
                f"【功能章节清单（规则初分）】（id | path | 正文字数 | 初分）\n{listing}")

    user = build_user()
    err: object = "未知错误"
    for attempt in range(3):
        try:
            data = await client.chat_json(system, user)
        except Exception as exc:  # noqa: BLE001 —— 拆分失败整体降级，不拖垮任务
            err = exc
        else:
            fixed = _apply_spec(rule_segs, data)
            if fixed is not None:
                out = _merge_objects(fixed)
                logger.info("软件模式拆分：%d → %d 段（子系统=%s，软件=%s）",
                            len(rule_segs), len(out), out[0].moduleSystem, out[0].moduleConfig)
                return out, True
            err = "输出与输入清单不匹配（items.id 须覆盖清单）"
        if attempt < 2:
            user = (f"{user}\n\n【修正要求】你上次的输出无法使用（错误：{str(err)[:120]}）。"
                    f"请返回含 subsystem/software/items 的 JSON 对象，items 覆盖全部 id。")
    logger.warning("软件模式拆分失败，回退规则初分：%s", str(err)[:160])
    return rule_segs, False


# ---------- 系统模式 ----------

def _parse_ranges(spec) -> list[int]:
    """解析 "12-18,300-315" 形式的 id 区间（容忍全角逗号/破折号）。"""
    if spec is None:
        return []
    text = str(spec).replace("，", ",").replace("－", "-").replace("–", "-")
    ids: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                ids.extend(range(int(a.strip()), int(b.strip()) + 1))
            except ValueError:
                continue
        else:
            try:
                ids.append(int(part))
            except ValueError:
                continue
    return ids


def _apply_system(anchors: list[Section], data) -> dict[int, tuple[str, str, str]] | None:
    """校验并应用坐标分组（ranges 并集）。覆盖不足半数判无效。"""
    if not isinstance(data, list):
        return None
    n = len(anchors)
    mapping: dict[int, tuple[str, str, str]] = {}
    for group in data:
        if not isinstance(group, dict):
            continue
        role_raw = str(group.get("role") or "").strip().lower()
        role = {"f": "functional", "d": "design", "s": "skip"}.get(role_raw[:1], role_raw)
        if role not in ("functional", "design", "skip"):
            role = "skip"
        sub = str(group.get("sub") or "").strip()
        sw = str(group.get("sw") or "").strip()
        if role != "skip" and (not sub or not sw):
            continue  # 功能/设计组缺坐标视为无效项
        for idx in _parse_ranges(group.get("ranges")):
            if 0 < idx <= n and (idx - 1) not in mapping:
                mapping[idx - 1] = (sub, sw, role)
    if len(mapping) < (n + 1) // 2:
        return None
    return mapping


def _build_system_segments(ir: MarkdownIR, sections: list[Section],
                           anchors: list[Section], mapping: dict) -> list[Segment]:
    """深层小节按文档序继承最近锚点坐标；按 (子系统, 软件) 归组拼接正文，超长切多段。"""
    groups: dict[tuple[str, str], list[str]] = {}
    order: list[tuple[str, str]] = []
    ai = 0
    current: tuple[str, str] | None = None
    for s in sections:
        if ai < len(anchors) and s is anchors[ai]:
            m = mapping.get(ai)
            current = (m[0], m[1]) if m and m[2] != "skip" else None
            ai += 1
        if current is not None and s.text.strip():
            if current not in groups:
                groups[current] = []
                order.append(current)
            groups[current].append(_expand_tables(s.text, ir.tables))
    segs: list[Segment] = []
    for sub, sw in order:
        content = "\n".join(groups[(sub, sw)])
        for chunk in _chunk_text(content, _CHUNK_CHARS):
            segs.append(Segment(sub, sw, sw, chunk, f"{sub}/{sw}"))
    return segs


def _chunk_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    rest = text
    while rest:
        if len(rest) <= limit:
            out.append(rest)
            break
        cut = rest.rfind("\n", int(limit * 0.8), limit)
        cut = cut if cut > 0 else limit
        out.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    return out


# ---------- 软件模式 ----------

def _identity_excerpt(ir: MarkdownIR, limit: int = 1500) -> str:
    """文档身份判定素材：前部有实质正文的章节文本（跳过目录/标题行）。"""
    parts: list[str] = []
    total = 0
    for s in ir.sections:
        t = s.text.strip()
        if len(t) < 40:
            continue
        parts.append(t)
        total += len(t)
        if total >= limit:
            break
    return "\n".join(parts)[: limit + 200]


def _apply_spec(rule_segs: list[Segment], data) -> list[Segment] | None:
    """应用 {subsystem, software, items}。items 命中不足半数判无效。"""
    if not isinstance(data, dict):
        return None
    n = len(rule_segs)
    sub = str(data.get("subsystem") or "").strip() or None
    sw = str(data.get("software") or "").strip() or None
    updates: dict[int, Segment | None] = {}
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item["id"]) - 1
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= idx < n or idx in updates:
            continue
        if item.get("drop") is True:
            updates[idx] = None
        else:
            base = rule_segs[idx]
            obj = str(item.get("softwareObject") or "").strip()
            ms = sub or base.moduleSystem
            mc = sw or base.moduleConfig
            updates[idx] = Segment(ms, mc, obj or base.softwareObject,
                                   base.content, base.path)
    if len(updates) < (n + 1) // 2:
        return None
    # 未提及的段也要统一身份（子系统/软件名），保持 mc 一致
    out: list[Segment] = []
    for i, s in enumerate(rule_segs):
        u = updates.get(i, s)
        if u is None:
            continue
        if sub or sw:
            u = Segment(sub or u.moduleSystem, sw or u.moduleConfig,
                        u.softwareObject, u.content, u.path)
        out.append(u)
    return out


def _merge_objects(segs: list[Segment]) -> list[Segment]:
    """按 (moduleConfig, softwareObject) 归并分段（内容拼接，保首现顺序）。"""
    index: dict[tuple[str, str], Segment] = {}
    for s in segs:
        key = (s.moduleConfig, s.softwareObject)
        if key in index:
            m = index[key]
            index[key] = Segment(m.moduleSystem, m.moduleConfig, m.softwareObject,
                                 m.content + "\n" + s.content, m.path)
        else:
            index[key] = s
    return list(index.values())
