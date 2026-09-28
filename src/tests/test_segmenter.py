# -*- coding: utf-8 -*-
"""S3 层级映射 v2：项目名为 system 层 + 分组章过滤。"""
from app.pipeline.markdown_ir import MarkdownIR, Section
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
