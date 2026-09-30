# -*- coding: utf-8 -*-
"""skill 资产完整性 + 抽取契约按模式注入。"""
from pathlib import Path

from app.pipeline.extractor import _DEFAULT_COMBO, _SKILL_COMBOS, LlmExtractor

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
    assert all(c for c in _SKILL_COMBOS.values())  # 组合非空


def test_split_skills_exist_and_old_removed():
    """两个拆分 skill 就位（split_system=系统模式 / split_spec=软件模式），
    旧 hierarchy_mapping 已随职责分离删除。"""
    assert (SKILLS_DIR / "split_system" / "SKILL.md").exists()
    assert (SKILLS_DIR / "split_spec" / "SKILL.md").exists()
    assert not (SKILLS_DIR / "hierarchy_mapping").exists()


def _extractor() -> LlmExtractor:
    class _C:  # prompt 构造不触网络
        async def chat_json(self, s, u): return []
        async def chat_text(self, s, u): return ""
    return LlmExtractor(_C(), str(SKILLS_DIR))


def test_contract_by_mode():
    """输出契约按模式注入（单一事实源在代码）：系统模式含 softwareObject，软件模式不含。"""
    ex = _extractor()
    sys_prompt = ex._build_system_prompt("NO4_QUOTATION", "system")
    spec_prompt = ex._build_system_prompt("NO4_QUOTATION_REVIEW", "spec")
    assert "softwareObject" in sys_prompt
    assert "softwareObject" not in spec_prompt
    assert "requirementName" in sys_prompt and "requirementName" in spec_prompt


def test_contract_by_tool():
    ex = _extractor()
    assert "ILF、EIF、EI、EO、EQ" in ex._build_system_prompt("NO4_QUOTATION", "spec")
    assert "仅允许 ILF/EIF" in ex._build_system_prompt("COST_ESTIMATION", "spec")
    assert "auditResult" in ex._build_system_prompt("NO4_AUDIT", "spec")
    assert "outsourceUnit" in ex._build_system_prompt("COST_MEASUREMENT", "spec")
    # 工具 skill 已剥离契约段，prompt 中契约只出现一次（代码注入的这份）
    assert ex._build_system_prompt("NO4_QUOTATION", "spec").count("只输出 JSON 数组") == 1
