# -*- coding: utf-8 -*-
"""skill 资产完整性：组合内所有 SKILL.md 必须存在，prompt 拼装可用。"""
from pathlib import Path

from app.pipeline.extractor import _DEFAULT_COMBO, _SKILL_COMBOS

SKILLS_DIR = Path(__file__).resolve().parent.parent / "app" / "skills"


def test_skill_files_exist():
    combos = set(_SKILL_COMBOS.values()) | {_DEFAULT_COMBO}
    for combo in combos:
        for name in combo:
            p = SKILLS_DIR / name / "SKILL.md"
            assert p.exists(), f"缺少 skill 文件：{name}/SKILL.md"


def test_all_toolcodes_covered():
    assert set(_SKILL_COMBOS) == {
        "COST_ESTIMATION", "NO4_QUOTATION", "NO4_QUOTATION_REVIEW",
        "COST_MEASUREMENT", "NO4_AUDIT",
    }
    # 每个组合都必须含层级归属 skill（抽取的前提）
    assert all("hierarchy_mapping" in c for c in _SKILL_COMBOS.values())
