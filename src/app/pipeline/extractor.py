"""S4 功能点抽取：按配置项分段 → LLM 判定需求条目与类型（计数与权重折算在公式引擎）。

两个实现：
- MockExtractor：确定性样例（联调/演示/回归基准），不联网；
- LlmExtractor：litellm 真实调用，skill 静态绑定，JSON 校验失败回喂重试 ≤2。

方案 A：
- 支持多段 pack 合并调用（extract_pack），减少 LLM 往返；
- description 输出契约压缩为短溯源，降低 completion tokens；
- system prompt 按 (tool, mode) 缓存，避免重复读 skill。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.core.errors import LLMError
from app.core.logging import get_logger
from app.pipeline.segmenter import Segment
from app.schemas.contract import FpType

logger = get_logger(__name__)

# 单段正文上限；pack 时各段共享总预算，避免单包过大
_SEG_CONTENT_LIMIT = 12000
_PACK_SEG_CONTENT_LIMIT = 8000


@dataclass
class RawFpEntry:
    moduleSystem: str
    moduleConfig: str
    softwareObject: str
    requirementName: str
    fpType: FpType
    fpCount: Decimal
    description: str | None = None
    auditResult: int | None = None  # 审价工具：模型判定 1合理/0不合理


# 按工具静态绑定 skill 组合（AGENTS.md §6：不做运行时动态选择）。
# 2026-09-29 职责分离：hierarchy_mapping（结构知识）移至 S3.5 refiner，抽取阶段只挂工具 skill
# ——曾与工具 skill 混装同一次调用，两套字段契约冲突导致模型整包输出三级结构（班组看板实测零功能点）。
_SKILL_COMBOS: dict[str, tuple[str, ...]] = {
    "COST_ESTIMATION": ("cost_estimation",),
    "NO4_QUOTATION": ("fp_pricing",),
    "NO4_QUOTATION_REVIEW": ("fp_pricing",),
    "COST_MEASUREMENT": ("fp_pricing", "cost_measurement"),
    "NO4_AUDIT": ("no4_audit",),
}
_DEFAULT_COMBO = ("fp_pricing",)


class Extractor(Protocol):
    async def extract(self, segments: list[Segment], tool_code: str,
                      mode: str = "spec") -> list[RawFpEntry]: ...


class MockExtractor:
    """确定性 mock：每个估算对象产 3 条典型功能点（ILF/EIF/EI 各 1）。

    输出稳定，作为契约联调与公式回归的基准数据。
    """

    async def extract(self, segments: list[Segment], tool_code: str,
                      mode: str = "spec") -> list[RawFpEntry]:
        entries: list[RawFpEntry] = []
        for seg in segments:
            base = seg.softwareObject
            entries.append(RawFpEntry(seg.moduleSystem, seg.moduleConfig, base,
                                      f"{base}数据管理", FpType.ILF, Decimal(1),
                                      "内部维护的业务数据（mock）"))
            entries.append(RawFpEntry(seg.moduleSystem, seg.moduleConfig, base,
                                      f"{base}外部接口", FpType.EIF, Decimal(1),
                                      "引用外部系统数据（mock）"))
            entries.append(RawFpEntry(seg.moduleSystem, seg.moduleConfig, base,
                                      f"{base}信息录入", FpType.EI, Decimal(1),
                                      "事务类输入（mock）"))
            if tool_code == "COST_ESTIMATION":
                # 经费估算只保留 ILF/EIF 两类
                entries = [e for e in entries if e.fpType in (FpType.ILF, FpType.EIF)]
        return entries

    async def extract_pack(self, segments: list[Segment], tool_code: str,
                           mode: str = "spec") -> list[RawFpEntry]:
        return await self.extract(segments, tool_code, mode)


class LlmExtractor:
    """真实抽取。prompt = 工具 skill 包（判定知识）+ 按模式注入的输出契约 + 分段内容。"""

    def __init__(self, client, skills_dir: str, max_tokens: int = 4096):
        self._client = client  # app.llm.client.LLMClient
        self._skills_dir = skills_dir
        self._max_tokens = max_tokens
        self._system_cache: dict[tuple[str, str, str], str] = {}

    async def extract(self, segments: list[Segment], tool_code: str,
                      mode: str = "spec") -> list[RawFpEntry]:
        """兼容旧接口：逐段抽取（内部仍走单段 pack）。"""
        entries: list[RawFpEntry] = []
        for seg in segments:
            entries.extend(await self.extract_pack([seg], tool_code, mode))
        return entries

    async def extract_pack(self, segments: list[Segment], tool_code: str,
                           mode: str = "spec") -> list[RawFpEntry]:
        """一次调用处理 1..N 段；多段时要求模型按 segId 回填。"""
        if not segments:
            return []
        system = self._build_system_prompt(tool_code, mode, packed=len(segments) > 1)
        user = self._build_user_prompt(segments, mode)
        # 校验失败回喂重试（≤2 次）
        for attempt in range(3):
            try:
                data = await self._client.chat_json(system, user, max_tokens=self._max_tokens)
                return self._parse(data, segments, mode)
            except (ValueError, LLMError, KeyError, TypeError) as exc:
                if attempt == 2:
                    raise LLMError(f"抽取输出非法（重试后仍不满足）：{exc}", retryable=True) from exc
                need_so = "、softwareObject(string)" if mode == "system" else ""
                need_seg = "、segId(int,对应输入段编号)" if len(segments) > 1 else ""
                user = (
                    f"{user}\n\n【修正要求】你上次的输出无法解析为合法的功能点数组"
                    f"（错误：{str(exc)[:120]}）。请严格只输出一个合法的 JSON 数组，每个元素"
                    f"必须包含字段 requirementName(string)、fpType(枚举){need_so}{need_seg}、"
                    f"description(string,短溯源)。注意转义中文引号与内部双引号，不要输出任何额外文字。"
                )
        return []

    def skill_names(self, tool_code: str) -> tuple[str, ...]:
        return _SKILL_COMBOS.get(tool_code, _DEFAULT_COMBO)

    def _build_system_prompt(self, tool_code: str, mode: str = "spec",
                             packed: bool = False) -> str:
        key = (tool_code, mode, "pack" if packed else "one")
        cached = self._system_cache.get(key)
        if cached is not None:
            return cached

        import pathlib
        parts = []
        for skill in self.skill_names(tool_code):
            p = pathlib.Path(self._skills_dir) / skill / "SKILL.md"
            if p.exists():
                parts.append(p.read_text(encoding="utf-8"))
        # 输出契约由代码按 模式+工具 注入（单一事实源，skill 只保留判定知识）
        types = "ILF、EIF" if tool_code == "COST_ESTIMATION" else "ILF、EIF、EI、EO、EQ"
        fields = f"requirementName(string)、fpType(枚举 {types})"
        notes = ""
        if packed:
            fields += "、segId(int,输入段编号)"
            notes += "每条必须带 segId，指向产生该需求的输入段；禁止跨段串号。"
        if mode == "system":
            # 系统模式：softwareObject（功能块）由抽取时逐条判定
            fields += "、softwareObject(string)"
            notes += ("softwareObject=该需求所属的软件内功能块：同一功能块用同一名称，"
                      "优先使用文档原词，禁止编造。")
        if tool_code == "NO4_AUDIT":
            fields += "、auditResult(1合理/0不合理)"
            notes += "auditResult 仅当能明确引用审价规则时为 0，存疑一律 1（判 0 依据写入 description）。"
        if tool_code == "COST_ESTIMATION":
            notes += "fpType 仅允许 ILF/EIF，操作功能一律不输出。"
        if tool_code == "COST_MEASUREMENT":
            notes += "可附加 outsourceUnit 字段（null/缺省=自研，有明确文档依据时填单位名）。"
        # 方案 A：压缩 description，显著减少 completion tokens
        fields += "、description(string,≤40字短溯源，格式如 R10|3.2.1 或 章节名+规则编号)"
        notes += "description 只写短溯源，禁止长解释/复述正文。"
        parts.append(
            f"\n输出要求：只输出 JSON 数组，每个元素含字段 {fields}。"
            f"fpCount 可选默认 1（权重折算由系统完成）。{notes}不得输出任何其他文字。"
        )
        prompt = "\n\n".join(parts)
        self._system_cache[key] = prompt
        return prompt

    def _build_user_prompt(self, segments: list[Segment], mode: str = "spec") -> str:
        if len(segments) == 1:
            seg = segments[0]
            head = f"子系统：{seg.moduleSystem}\n软件（配置项）：{seg.moduleConfig}\n"
            if mode == "system":
                head += "（本段功能块的 softwareObject 由你逐条判定）\n"
            else:
                head += f"功能块（估算对象）：{seg.softwareObject}\n"
            return head + f"\n文档内容：\n{seg.content[:_SEG_CONTENT_LIMIT]}"

        blocks = [
            f"共 {len(segments)} 个文档段。请输出一个 JSON 数组，覆盖所有段中的功能点；"
            f"每条必须包含 segId（1..{len(segments)}）。"
        ]
        for i, seg in enumerate(segments, 1):
            head = (
                f"\n===== 段 {i} =====\n"
                f"子系统：{seg.moduleSystem}\n软件（配置项）：{seg.moduleConfig}\n"
            )
            if mode == "system":
                head += "（本段功能块的 softwareObject 由你逐条判定）\n"
            else:
                head += f"功能块（估算对象）：{seg.softwareObject}\n"
            blocks.append(head + f"文档内容：\n{seg.content[:_PACK_SEG_CONTENT_LIMIT]}")
        return "\n".join(blocks)

    def _parse(self, data, segments: list[Segment], mode: str = "spec") -> list[RawFpEntry]:
        if not isinstance(data, list):
            raise LLMError(f"抽取结果应为 JSON 数组，实际为 {type(data).__name__}", retryable=False)
        packed = len(segments) > 1
        out: list[RawFpEntry] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            if packed:
                try:
                    seg_id = int(item.get("segId"))
                except (TypeError, ValueError) as exc:
                    raise LLMError(f"多段抽取缺少合法 segId：{item!r}", retryable=True) from exc
                if not 1 <= seg_id <= len(segments):
                    raise LLMError(f"segId 越界：{seg_id}", retryable=True)
                seg = segments[seg_id - 1]
            else:
                seg = segments[0]
            fp_type = FpType(str(item["fpType"]).upper())
            audit = item.get("auditResult")
            obj = str(item.get("softwareObject") or "").strip() if mode == "system" else ""
            desc = item.get("description")
            if isinstance(desc, str) and len(desc) > 80:
                desc = desc[:80].rstrip()
            out.append(RawFpEntry(
                moduleSystem=seg.moduleSystem, moduleConfig=seg.moduleConfig,
                softwareObject=obj or seg.softwareObject,
                requirementName=str(item["requirementName"]),
                fpType=fp_type, fpCount=Decimal(str(item.get("fpCount", 1))),
                description=desc,
                auditResult=int(audit) if audit is not None else None,
            ))
        return out


def pack_segments(segments: list[Segment], max_chars: int = 10000,
                  max_segments: int = 8) -> list[list[Segment]]:
    """将短段合并为抽取包。

    规则：
    - 同 (moduleSystem, moduleConfig) 优先同包，减少跨软件串味；
    - 单段超限则独立成包；
    - max_chars/max_segments <=0 时退化为 1 段 1 包。
    """
    if not segments:
        return []
    if max_chars <= 0 or max_segments <= 1:
        return [[s] for s in segments]

    packs: list[list[Segment]] = []
    cur: list[Segment] = []
    cur_chars = 0
    cur_key: tuple[str, str] | None = None

    def flush() -> None:
        nonlocal cur, cur_chars, cur_key
        if cur:
            packs.append(cur)
        cur, cur_chars, cur_key = [], 0, None

    for seg in segments:
        key = (seg.moduleSystem, seg.moduleConfig)
        # 估算本段进入 prompt 的体积（含少量元数据开销）
        size = min(len(seg.content or ""), _PACK_SEG_CONTENT_LIMIT) + 120
        if size >= max_chars:
            flush()
            packs.append([seg])
            continue
        same_group = cur_key is None or key == cur_key
        if cur and (not same_group or len(cur) >= max_segments or cur_chars + size > max_chars):
            flush()
        cur.append(seg)
        cur_chars += size
        cur_key = key
    flush()
    return packs


def entries_to_cache(entries: list[RawFpEntry]) -> list[dict]:
    return [
        {
            "moduleSystem": e.moduleSystem,
            "moduleConfig": e.moduleConfig,
            "softwareObject": e.softwareObject,
            "requirementName": e.requirementName,
            "fpType": e.fpType.value,
            "fpCount": str(e.fpCount),
            "description": e.description,
            "auditResult": e.auditResult,
        }
        for e in entries
    ]


def entries_from_cache(rows: list[dict]) -> list[RawFpEntry]:
    out: list[RawFpEntry] = []
    for item in rows or []:
        out.append(RawFpEntry(
            moduleSystem=str(item.get("moduleSystem") or ""),
            moduleConfig=str(item.get("moduleConfig") or ""),
            softwareObject=str(item.get("softwareObject") or ""),
            requirementName=str(item.get("requirementName") or ""),
            fpType=FpType(str(item["fpType"]).upper()),
            fpCount=Decimal(str(item.get("fpCount", 1))),
            description=item.get("description"),
            auditResult=item.get("auditResult"),
        ))
    return out


def parse_json_block(text: str):
    """容错解析：模型输出可能带 json 围栏或前后杂文，取首个平衡的 JSON 块。"""
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        text = m.group(1)
    start = min((i for i in (text.find("["), text.find("{")) if i >= 0), default=-1)
    if start < 0:
        raise LLMError("模型未返回 JSON 内容", retryable=False)
    return json.loads(text[start:])
