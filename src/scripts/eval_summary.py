# -*- coding: utf-8 -*-
"""汇总 data/extract_*.json 基线结果为对照表。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

data_dir = Path(__file__).resolve().parent.parent / "evals" / "baseline"
files = sorted(data_dir.glob("extract_*.json"))
if not files:
    sys.exit("没有 extract_*.json")

rows = []
for f in files:
    d = json.loads(f.read_text(encoding="utf-8"))
    s = d["summary"]
    rows.append((f.stem.replace("extract_", ""), d["file"], d["segments"], s["count"], s["types"], s["totalYuan"]))

print(f"{'基线':<12}{'文件':<38}{'段数':>4}{'条目':>5}{'综合费用(万元)':>12}")
print("-" * 78)
total_cnt = 0
for kw, name, segs, cnt, types, total in rows:
    total_cnt += cnt
    print(f"{kw:<12}{name[:36]:<38}{segs:>4}{cnt:>5}{total / 10000:>12.2f}")
print("-" * 78)
print(f"合计 {len(rows)} 篇，{total_cnt} 条功能点")
print("\n类型分布（合计）：")
agg = {}
for _, _, _, _, types, _ in rows:
    for k, v in types.items():
        agg[k] = agg.get(k, 0) + v
print(" ", dict(sorted(agg.items(), key=lambda x: -x[1])))
