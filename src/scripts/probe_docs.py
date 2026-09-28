# -*- coding: utf-8 -*-
"""管线探针：对真实文档实弹跑 S0→S3，并用 mock 抽取器离线跑完 S4/S5/装配。

用途：验证格式归一化（含 Word97 转换）、章节树、表格检测、章节定位、
估算对象切分在真实文档上的表现；输出统计报告 + 章节树抽样。

用法：
    python scripts/probe_docs.py [文档目录] [--tree 文件名关键词]
"""
from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC_ROOT))

from app.engine import assembler
from app.pipeline import locator, markdown_ir, normalizer, segmenter, validator
from app.pipeline.extractor import MockExtractor
from app.schemas.contract import ToolCode

TOOL = ToolCode.NO4_QUOTATION_REVIEW  # 需求规格说明 → 可信四/需求评审工具


def probe_file(path: Path, tree: bool) -> dict:
    data = path.read_bytes()
    doc = normalizer.normalize(data, path.name, path.suffix.lstrip("."))
    ir = markdown_ir.build_ir(doc)
    kept = locator.locate(ir)
    segs = segmenter.segment(ir, kept, project_name=path.stem)

    levels = Counter(s.level for s in ir.sections)
    merged_tables = sum(1 for t in ir.tables if t.merged)
    stats = {
        "file": path.name,
        "kind": doc.kind,
        "sections": len(ir.sections),
        "levels": dict(sorted(levels.items())),
        "tables": len(ir.tables),
        "merged_tables": merged_tables,
        "kept_sections": len(kept),
        "keep_rate": f"{len(kept) / len(ir.sections) * 100:.0f}%" if ir.sections else "-",
        "segments": len(segs),
    }

    print(f"\n=== {path.name} ===")
    print(f"  归一化={stats['kind']}  章节={stats['sections']}{stats['levels']}"
          f"  表格={stats['tables']}(合并单元格 {stats['merged_tables']})")
    print(f"  定位保留={stats['kept_sections']}/{stats['sections']}({stats['keep_rate']})  估算对象分段={stats['segments']}")

    if tree:
        print("  --- 章节树（前 35 个标题）---")
        shown = 0
        for s in ir.sections:
            if s.level <= 3 and shown < 35:
                print(f"  {'  ' * (s.level - 1)}[L{s.level}] {s.path}")
                shown += 1

    # 离线端到端：mock 抽取 → 校验 → 公式装配
    entries = asyncio.run(MockExtractor().extract(segs, TOOL.value))
    fp_list = validator.validate_and_merge(entries, TOOL)
    scales = assembler.build_fp_scale(fp_list, TOOL)
    items = assembler.build_cost_items(scales)
    total = sum(i.subtotal for i in items)
    print(f"  [mock端到端] 功能点 {len(fp_list)} 条 | 配置项 {len(scales)} | 综合费用合计 {total / 10000:.2f} 万元")
    return stats


def main() -> None:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else SRC_ROOT.parent / "测试文件"
    tree_kw = None
    if "--tree" in sys.argv:
        tree_kw = sys.argv[sys.argv.index("--tree") + 1]

    files = sorted(p for p in root.rglob("*") if p.suffix.lower() == ".docx")
    if not files:
        print(f"目录下没有 .docx：{root}")
        return
    print(f"探针目录：{root}（{len(files)} 个文件）")

    all_stats = []
    for p in files:
        try:
            all_stats.append(probe_file(p, tree_kw is None or tree_kw in p.name))
        except Exception as exc:
            print(f"\n=== {p.name} ===\n  ❌ 失败：{exc}")

    ok = len(all_stats)
    print(f"\n===== 汇总：成功 {ok}/{len(files)} =====")
    if ok:
        total_seg = sum(s["segments"] for s in all_stats)
        total_tables = sum(s["tables"] for s in all_stats)
        print(f"  分段合计 {total_seg}，表格合计 {total_tables}，"
              f"定位保留率区间 {min(s['keep_rate'] for s in all_stats if s['keep_rate'] != '-')}~{max(s['keep_rate'] for s in all_stats if s['keep_rate'] != '-')}")


if __name__ == "__main__":
    main()
