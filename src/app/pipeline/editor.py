"""对话编辑应用层：把模型输出的结构化编辑操作确定性应用到 originalFpList。

契约（dialog_intents skill 定义，模型输出）：
  {"intent": "edit|consult", "reply": "说明文字",
   "edits": [
     {"op": "set",   "find": {"requirementName": "...", "moduleConfig": "...", "softwareObject": "..."},
                     "set": {"fpCount": 3, "fpType": "ILF", "requirementName": "新名", "description": "..."}},
     {"op": "remove", "find": {...}},
     {"op": "add",    "entry": {"requirementName": "...", "fpType": "EI", "fpCount": 1, ...层级字段可选}}
   ]}

原则：模型只表达"想改什么"，匹配与修改全部在本模块代码内确定性地完成；
应用结果生成摘要（已应用/未应用），拼进对话回复，保证叙述与数据一致。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from app.engine import formulas as F
from app.schemas.contract import FpType, OriginalFp, ToolCode


@dataclass
class EditOutcome:
    applied: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.applied)


def apply_edits(entries: list[OriginalFp], edits: list[dict],
                tool_code: ToolCode, user_message: str = "") -> tuple[list[OriginalFp], EditOutcome]:
    """按序应用编辑操作，返回（新列表, 结果摘要）。原列表不被修改。

    user_message：用户最后一轮原话，作为名称歧义裁决的最高信号。
    """
    out = copy.deepcopy(entries)
    outcome = EditOutcome()
    legal_types = set(F.weights_for(tool_code))

    for i, op in enumerate(edits or []):
        try:
            kind = str(op.get("op", "set")).lower()
            if kind == "set":
                _do_set(out, op, legal_types, outcome, user_message)
            elif kind == "remove":
                _do_remove(out, op, outcome, user_message)
            elif kind == "add":
                _do_add(out, op, legal_types, outcome)
            else:
                outcome.failed.append(f"第{i + 1}条：不支持的操作类型 {kind}")
        except AmbiguousMatch as exc:
            outcome.failed.append(f"第{i + 1}条：{exc}——请使用完整名称重试")
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            outcome.failed.append(f"第{i + 1}条：{exc}")
    return out, outcome


# ---------- 匹配 ----------

class AmbiguousMatch(Exception):
    """名称歧义：命中多个不同条目名，拒绝执行并携带候选清单。"""


def _match_indices(entries: list[OriginalFp], find: dict, user_message: str = "") -> list[int]:
    """确定性匹配：requirementName 精确 → 包含模糊；可用 moduleConfig/softwareObject 收窄范围。

    歧义裁决以**用户原话**为最高信号：
    - 精确命中，且用户原话里出现了更长的完整条目名（模型把用户说的全名缩写了）→ 重定向到全名；
    - 精确命中，用户原话无更长名称 → 精确命中生效（"用户信息"不被"查询用户信息"干扰）；
    - 模糊命中多个不同名称 → 用户原话恰好包含其一则选定，否则拒绝并列候选（宁失败不改错）。
    """
    name = str(find.get("requirementName") or "").strip()
    cfg = str(find.get("moduleConfig") or "").strip()
    obj = str(find.get("softwareObject") or "").strip()

    pool = [idx for idx, e in enumerate(entries)
            if (not cfg or e.moduleConfig == cfg) and (not obj or e.softwareObject == obj)]

    if not name:
        return pool  # 仅按范围匹配（如删除某对象下全部条目）
    exact = [idx for idx in pool if entries[idx].requirementName == name]
    if exact:
        longer = [entries[idx].requirementName for idx in pool
                  if entries[idx].requirementName != name
                  and name in entries[idx].requirementName
                  and entries[idx].requirementName in user_message]
        if longer:
            target = max(longer, key=len)
            return [idx for idx in pool if entries[idx].requirementName == target]
        return exact
    fuzzy = [idx for idx in pool
             if name in entries[idx].requirementName or entries[idx].requirementName in name]
    distinct = {entries[idx].requirementName for idx in fuzzy}
    if len(distinct) > 1:
        in_msg = [n for n in distinct if n in user_message]
        if in_msg:
            target = max(in_msg, key=len)  # 用户原话中最长（最具体）的名称优先
            return [idx for idx in fuzzy if entries[idx].requirementName == target]
        raise AmbiguousMatch(f"'{name}' 模糊命中多个条目：{'、'.join(sorted(distinct))}")
    return fuzzy


# ---------- 操作 ----------

_SETTABLE = {"fpCount", "fpType", "requirementName", "description"}


def _do_set(entries: list[OriginalFp], op: dict, legal_types: set[FpType],
            outcome: EditOutcome, user_message: str = "") -> None:
    find = op.get("find") or {}
    sets = op.get("set") or {}
    unknown = set(sets) - _SETTABLE
    if unknown:
        outcome.failed.append(f"set：不可修改字段 {sorted(unknown)}")
        sets = {k: v for k, v in sets.items() if k in _SETTABLE}
    idxs = _match_indices(entries, find, user_message)
    if not idxs:
        outcome.failed.append(f"set：未找到目标条目 {find.get('requirementName') or find}")
        return

    # 先整体校验（类型/取值），任一非法则该条操作整体失败，不产生半截修改
    fp_type = None
    if "fpType" in sets:
        try:
            fp_type = FpType(str(sets["fpType"]).upper())
        except ValueError:
            outcome.failed.append(f"set：fpType 非法值 {sets['fpType']}")
            return
        if fp_type not in legal_types:
            outcome.failed.append(f"set：fpType {fp_type.value} 不是工具 {ToolCode} 支持的类型")
            return
    fp_count = None
    if "fpCount" in sets:
        try:
            fp_count = Decimal(str(sets["fpCount"]))
        except InvalidOperation:
            outcome.failed.append(f"set：fpCount 非数值 {sets['fpCount']}")
            return
        if fp_count <= 0:
            outcome.failed.append(f"set：fpCount 须为正数，收到 {sets['fpCount']}")
            return

    for idx in idxs:
        e = entries[idx]
        changes = []
        if fp_count is not None and e.fpCount != fp_count:
            changes.append(f"fpCount {e.fpCount}→{fp_count}")
            e.fpCount = fp_count
        if fp_type is not None and e.fpType != fp_type:
            changes.append(f"fpType {e.fpType.value}→{fp_type.value}")
            e.fpType = fp_type
        if "requirementName" in sets and str(sets["requirementName"]).strip():
            new_name = str(sets["requirementName"]).strip()
            if e.requirementName != new_name:
                changes.append(f"名称→{new_name}")
                e.requirementName = new_name
        if "description" in sets:
            e.description = str(sets["description"])
        outcome.applied.append(
            f"{e.requirementName}（{e.softwareObject}）：" + ("、".join(changes) or "无变化")
        )


def _do_remove(entries: list[OriginalFp], op: dict, outcome: EditOutcome, user_message: str = "") -> None:
    find = op.get("find") or {}
    idxs = _match_indices(entries, find, user_message)
    if not idxs:
        outcome.failed.append(f"remove：未找到目标条目 {find.get('requirementName') or find}")
        return
    for idx in sorted(idxs, reverse=True):
        e = entries.pop(idx)
        outcome.applied.append(f"已删除：{e.requirementName}（{e.softwareObject}）")


def _do_add(entries: list[OriginalFp], op: dict, legal_types: set[FpType], outcome: EditOutcome) -> None:
    raw = op.get("entry") or op.get("set") or {}
    name = str(raw.get("requirementName") or "").strip()
    if not name:
        outcome.failed.append("add：缺少 requirementName")
        return
    try:
        fp_type = FpType(str(raw.get("fpType")).upper())
    except ValueError:
        outcome.failed.append(f"add：fpType 非法值 {raw.get('fpType')}")
        return
    if fp_type not in legal_types:
        outcome.failed.append(f"add：fpType {fp_type.value} 不是该工具支持的类型")
        return
    try:
        fp_count = Decimal(str(raw.get("fpCount", 1)))
    except InvalidOperation:
        outcome.failed.append(f"add：fpCount 非数值 {raw.get('fpCount')}")
        return
    if fp_count <= 0:
        outcome.failed.append(f"add：fpCount 须为正数，收到 {raw.get('fpCount')}")
        return

    # 层级字段缺省：沿用列表中最近一条的层级（追加到同一估算对象语境）
    if entries:
        ref = entries[-1]
        ms = str(raw.get("moduleSystem") or ref.moduleSystem)
        mc = str(raw.get("moduleConfig") or ref.moduleConfig)
        so = str(raw.get("softwareObject") or ref.softwareObject)
    else:
        ms = str(raw.get("moduleSystem") or "未分类")
        mc = str(raw.get("moduleConfig") or "未分类")
        so = str(raw.get("softwareObject") or "未分类")

    entries.append(OriginalFp(
        moduleSystem=ms, moduleConfig=mc, softwareObject=so,
        requirementName=name, fpType=fp_type, fpCount=fp_count,
        description=str(raw["description"]) if raw.get("description") else None,
    ))
    outcome.applied.append(f"已新增：{name}（{fp_type.value}×{fp_count}，{so}）")
