# -*- coding: utf-8 -*-
"""对话编辑应用层：匹配、修改、校验、边界。"""
from decimal import Decimal

from app.pipeline.editor import apply_edits
from app.schemas.contract import FpType, OriginalFp, ToolCode

D = Decimal


def _fp(name="用户信息", fp=FpType.ILF, cnt=1, obj="登录功能", cfg="配置A") -> OriginalFp:
    return OriginalFp(moduleSystem="系统", moduleConfig=cfg, softwareObject=obj,
                      requirementName=name, fpType=fp, fpCount=D(cnt))


def test_set_fpcount_exact_match():
    out, oc = apply_edits([_fp()], [{"op": "set", "find": {"requirementName": "用户信息"},
                                     "set": {"fpCount": 3}}], ToolCode.NO4_QUOTATION)
    assert out[0].fpCount == 3 and oc.changed and not oc.failed


def test_set_fuzzy_and_scope():
    fps = [_fp(name="查询用户信息", fp=FpType.EQ, obj="对象A"),
           _fp(name="查询用户信息", fp=FpType.EQ, obj="对象B")]
    out, oc = apply_edits(
        fps,
        [{"op": "set", "find": {"requirementName": "查询用户信息", "softwareObject": "对象B"},
          "set": {"fpCount": 2}}], ToolCode.NO4_QUOTATION)
    assert out[0].fpCount == 1 and out[1].fpCount == 2


def test_set_multiple_matches_apply_all():
    fps = [_fp(name="用户信息", obj="A"), _fp(name="用户信息", obj="B")]
    out, oc = apply_edits(fps, [{"op": "set", "find": {"requirementName": "用户信息"},
                                 "set": {"fpCount": 5}}], ToolCode.NO4_QUOTATION)
    assert all(e.fpCount == 5 for e in out) and len(oc.applied) == 2


def test_set_invalid_fptype_for_tool_rejected():
    out, oc = apply_edits([_fp()], [{"op": "set", "find": {"requirementName": "用户信息"},
                                     "set": {"fpType": "EI"}}], ToolCode.COST_ESTIMATION)
    assert out[0].fpType == FpType.ILF and oc.failed  # 经费估算不接受 EI


def test_set_invalid_fpcount_rejected():
    out, oc = apply_edits([_fp()], [{"op": "set", "find": {"requirementName": "用户信息"},
                                     "set": {"fpCount": 0}}], ToolCode.NO4_QUOTATION)
    assert out[0].fpCount == 1 and oc.failed


def test_set_unknown_field_reported():
    _, oc = apply_edits([_fp()], [{"op": "set", "find": {"requirementName": "用户信息"},
                                   "set": {"workload": 9}}], ToolCode.NO4_QUOTATION)
    assert any("不可修改字段" in f for f in oc.failed)  # 计算结果字段不可直接改


def test_remove_and_add():
    fps = [_fp(), _fp(name="查询用户信息", fp=FpType.EQ)]
    out, oc = apply_edits(
        fps,
        [{"op": "remove", "find": {"requirementName": "查询用户信息"}},
         {"op": "add", "entry": {"requirementName": "权限配置信息", "fpType": "ILF", "fpCount": 1}}],
        ToolCode.NO4_QUOTATION)
    assert [e.requirementName for e in out] == ["用户信息", "权限配置信息"]
    assert out[1].softwareObject == "登录功能"  # add 缺省沿用最近条目层级


def test_no_match_reports_failure():
    out, oc = apply_edits([_fp()], [{"op": "set", "find": {"requirementName": "不存在"},
                                     "set": {"fpCount": 2}}], ToolCode.NO4_QUOTATION)
    assert out[0].fpCount == 1 and oc.failed and not oc.applied


def test_original_list_untouched():
    src = [_fp()]
    apply_edits(src, [{"op": "set", "find": {"requirementName": "用户信息"},
                       "set": {"fpCount": 9}}], ToolCode.NO4_QUOTATION)
    assert src[0].fpCount == 1  # 深拷贝，原列表不被修改


def test_ambiguous_short_name_rejected():
    """用户原话裁决（实测两个教训的统一解法）：
    ① 模型缩写（用户说"导出需求可追踪报告"，find 给"导出需求"）→ 重定向到用户说的全名；
    ② 精确命中（"用户信息"）不应被包含它的更长名称（"查询用户信息"）误判为歧义。"""
    fps = [_fp(name="导出需求", fp=FpType.EO, obj="A"),
           _fp(name="导出需求可追踪报告", fp=FpType.EO, obj="A")]
    # ① 用户原话含全名 → 重定向删全名那条，不动短名条目
    out, oc = apply_edits(
        fps, [{"op": "remove", "find": {"requirementName": "导出需求"}}],
        ToolCode.NO4_QUOTATION, user_message="请删除导出需求可追踪报告")
    assert [e.requirementName for e in out] == ["导出需求"]
    assert oc.changed and not oc.failed
    # 无用户原话时精确命中生效（删的就是字面完全同名的条目）
    out2, oc2 = apply_edits(fps, [{"op": "remove", "find": {"requirementName": "导出需求"}}],
                            ToolCode.NO4_QUOTATION)
    assert [e.requirementName for e in out2] == ["导出需求可追踪报告"]

    # ② 精确命中不被更长名称干扰（用户没说全名就不重定向）
    fps2 = [_fp(name="用户信息", fp=FpType.ILF), _fp(name="查询用户信息", fp=FpType.EQ)]
    out3, oc3 = apply_edits(
        fps2, [{"op": "set", "find": {"requirementName": "用户信息"}, "set": {"fpCount": 3}}],
        ToolCode.NO4_QUOTATION, user_message="把用户信息的功能点个数改为3")
    assert out3[0].fpCount == 3 and out3[1].fpCount == 1 and not oc3.failed


def test_fuzzy_multi_distinct_names_rejected():
    fps = [_fp(name="查询用户", fp=FpType.EQ), _fp(name="查询用户日志", fp=FpType.EQ)]
    # 无用户原话线索 → 拒绝
    out, oc = apply_edits(fps, [{"op": "set", "find": {"requirementName": "查询"},
                                 "set": {"fpCount": 2}}], ToolCode.NO4_QUOTATION)
    assert out[0].fpCount == 1 and out[1].fpCount == 1 and oc.failed
    # 用户原话恰好包含其一 → 选定
    out2, oc2 = apply_edits(
        fps, [{"op": "set", "find": {"requirementName": "查询"}, "set": {"fpCount": 2}}],
        ToolCode.NO4_QUOTATION, user_message="把查询用户日志改成2个")
    assert out2[0].fpCount == 1 and out2[1].fpCount == 2 and not oc2.failed
