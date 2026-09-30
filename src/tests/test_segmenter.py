# -*- coding: utf-8 -*-
"""S3 层级映射 v2：项目名为 system 层 + 分组章过滤。"""
from app.pipeline.markdown_ir import MarkdownIR, Section, TableBlock
from app.pipeline.segmenter import segment

PROJ = "某指挥信息系统"


def _ir(sections: list[tuple[str, int]]) -> MarkdownIR:
    return MarkdownIR(fileName="t.docx",
                      sections=[Section(path=p, level=l, text="内容") for p, l in sections])


def test_gjb_fr_tree_mapping():
    ir = _ir([
        ("需求", 1),
        ("需求/能力需求（FR）", 2),
        ("需求/能力需求（FR）/登录功能（FR_1）", 3),
        ("需求/能力需求（FR）/登录功能（FR_1）/多种要素认证（FR_1.1）", 4),
    ])
    segs = segment(ir, ir.sections, PROJ)
    by_path = {s.path: s for s in segs}
    fr1 = by_path["需求/能力需求（FR）/登录功能（FR_1）"]
    fr11 = by_path["需求/能力需求（FR）/登录功能（FR_1）/多种要素认证（FR_1.1）"]
    # system=项目名；分组章（需求/能力需求（FR））被过滤
    assert all(s.moduleSystem == PROJ for s in segs)
    assert fr1.moduleConfig == "登录功能（FR_1）" and fr1.softwareObject == "登录功能（FR_1）"
    assert fr11.moduleConfig == "登录功能（FR_1）" and fr11.softwareObject == "多种要素认证（FR_1.1）"


def test_design_doc_tree_mapping():
    ir = _ir([
        ("总体设计", 1),
        ("C3I系统详细设计", 1),
        ("C3I系统详细设计/指挥监控应用子系统", 2),
        ("C3I系统详细设计/指挥监控应用子系统/指挥监控应用软件设计", 3),
        ("C3I系统详细设计/指挥监控应用子系统/指挥监控应用软件设计/三维显示功能", 4),
    ])
    segs = segment(ir, ir.sections, PROJ)
    leaf = next(s for s in segs if s.path.endswith("三维显示功能"))
    # 分组章（总体设计/xx设计）过滤后：config=子系统，object=具体功能
    assert leaf.moduleSystem == PROJ
    assert leaf.moduleConfig == "指挥监控应用子系统"
    assert leaf.softwareObject == "三维显示功能"
    # "xx软件设计"这类分组章自身的正文（多为概述）回落到子系统层
    design = next(s for s in segs if s.path.endswith("指挥监控应用软件设计"))
    assert design.moduleConfig == "指挥监控应用子系统"
    assert design.softwareObject == "指挥监控应用子系统"


def test_group_only_section_fallback():
    ir = _ir([("需求", 1), ("需求/性能需求（XN）", 2)])
    segs = segment(ir, ir.sections, PROJ)
    xn = next(s for s in segs if "XN" in s.path)
    assert xn.moduleConfig == "默认配置项"
    assert xn.softwareObject == "性能需求（XN）"  # 兜底保留原叶子名


def test_empty_ir_fallback():
    ir = MarkdownIR(fileName="t.docx", sections=[])
    segs = segment(ir, [], PROJ)
    assert len(segs) == 1 and segs[0].moduleSystem == PROJ


def test_table_placeholder_expanded():
    """纯表格驱动文档（无标题结构）：[TABLE:n] 占位必须展开为表格行文本喂给模型。

    2026-09-29 班组看板需规实测：功能定义全在 24 张表里，占位符不展开则真实抽取为零。"""
    ir = _ir([("(正文)", 1)])
    ir.sections[0].text = "页面定义如下\n[TABLE:0]\n角色定义见后"
    ir.tables.append(TableBlock(path="(正文)", header=["编号", "页面"],
                                rows=[["1", "班组信息展示"], ["2", "任务管理"]]))
    segs = segment(ir, ir.sections, PROJ)
    assert "[TABLE:0]" not in segs[0].content
    assert "编号 | 页面" in segs[0].content
    assert "班组信息展示" in segs[0].content and "任务管理" in segs[0].content


def test_pseudo_heading_split_for_body_section():
    """无标题文档："(正文)"段按伪标题（模块N：）初分；容器"(正文)"不作层级。"""
    ir = _ir([("(正文)", 1)])
    ir.sections[0].text = (
        "文档前言说明。\n模块1：班组信息\n展示班组基础信息与文化建设成果的完整描述行\n"
        "模块2：任务管理\n任务总览入口\n模块3：知识管理\n知识条目维护"
    )
    segs = segment(ir, ir.sections, PROJ)
    module_segs = [s for s in segs if "模块" in s.path]
    assert len(module_segs) == 3
    m2 = next(s for s in segs if "模块2" in s.path)
    assert "任务总览入口" in m2.content                       # 短正文行留在段内不误切
    assert all("任务总览入口" not in s.content for s in segs if s is not m2)
    assert m2.moduleConfig == "模块2：任务管理"                # "(正文)"容器被过滤
    assert m2.softwareObject == "模块2：任务管理"


def test_no_pseudo_split_when_plain_text():
    """普通行文（伪标题候选不足 2 个）不切分，保持单段。"""
    ir = _ir([("(正文)", 1)])
    ir.sections[0].text = "普通句子一行。\n另一句普通的话，长度超过十二个字符的正文行。\n再来一句总结。"
    segs = segment(ir, ir.sections, PROJ)
    assert len(segs) == 1


def test_structured_sections_not_split():
    """有标题结构的章节不进伪切分分支。"""
    ir = _ir([("需求/能力需求（FR）/登录功能（FR_1）", 3)])
    ir.sections[0].text = "# 登录功能（FR_1）\n模块1：认证方式\n模块2：会话管理"
    segs = segment(ir, ir.sections, PROJ)
    assert len(segs) == 1 and segs[0].softwareObject == "登录功能（FR_1）"


def test_toc_block_dropped():
    """目录特征识别：前部近空章节簇、标题与后续正文章节重复 → 判为目录剔除。"""
    ir = _ir([
        ("范围", 1), ("需求", 1), ("需求/能力需求（FR）/登录功能（FR_1）", 3),  # 目录残留（近空）
        ("范围", 1), ("需求", 1), ("需求/能力需求（FR）/登录功能（FR_1）", 3),  # 真实正文
    ])
    for s in ir.sections[:3]:
        s.text = "【目录残留】" + s.path.rsplit("/", 1)[-1]  # 目录行：只有标题级短文本
    ir.sections[3].text = "# 范围\n" + "范围正文" * 40
    ir.sections[4].text = "# 需求\n" + "需求正文" * 40
    ir.sections[5].text = "# 登录功能（FR_1）\n" + "登录需求正文" * 40
    segs = segment(ir, ir.sections, PROJ)
    joined = "\n".join(s.content for s in segs)
    assert "范围正文" in joined and "登录需求正文" in joined   # 真实正文保留
    assert "【目录残留】" not in joined                        # 目录残留簇被剔除


def test_short_prefix_not_mistaken_as_toc():
    """正常的前部短章节（标题不与后续重复）不误删。"""
    ir = _ir([("引言", 1), ("需求/能力需求（FR）/登录功能（FR_1）", 3)])
    ir.sections[0].text = "# 引言\n编写说明。" + "背景" * 30
    ir.sections[1].text = "# 登录功能（FR_1）\n应提供登录功能。" + "描述" * 30
    segs = segment(ir, ir.sections, PROJ)
    assert any(s.path == "引言" for s in segs)
