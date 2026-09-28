# -*- coding: utf-8 -*-
"""真实 LLM 抽取评测：对真实需规文档跑完整管线（S0→S3 + LlmExtractor S4 + S5 + 装配）。

用法：
    python scripts/extract_real.py 二期01                     # 按关键词选文件
    python scripts/extract_real.py 伴飞 --filter 能力需求      # 只抽功能需求章节
可选：--tool TOOLCODE（默认 NO4_QUOTATION_REVIEW）；--limit N 限制段数；--concurrency K（默认 3）
结果存 data/extract_<关键词>_<tool>.json
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC_ROOT))

from app.core.config import load_settings
from app.engine import assembler
from app.llm.client import LiteLLMClient
from app.pipeline import locator, markdown_ir, normalizer, segmenter, validator
from app.pipeline.extractor import LlmExtractor, RawFpEntry
from app.schemas.contract import ToolCode


def find_file(kw: str) -> Path:
    root = SRC_ROOT.parent / "测试文件"
    hits = sorted(p for p in root.rglob("*.docx") if kw in p.name)
    if not hits:
        raise SystemExit(f"找不到匹配 '{kw}' 的 .docx 文档（目录 {root}）")
    return hits[0]


async def extract_one(extractor: LlmExtractor, seg, tool: str, sem: asyncio.Semaphore):
    """单段抽取；限速错误长退避（MiniMax Token Plan 有 RPM 限制），其他错误短重试一次。"""
    async with sem:
        for attempt in (1, 2, 3):
            try:
                return await extractor.extract([seg], tool)
            except Exception as exc:
                rate_limited = "rate_limit" in str(exc).lower() or "速率限制" in str(exc)
                if attempt == 3:
                    print(f"    ⚠️ 段落失败 {seg.path}: {exc}")
                    return []
                await asyncio.sleep(20 if rate_limited else 2)


async def run(kw: str, filter_re: str | None, limit: int | None, conc: int, tool: ToolCode) -> None:
    settings = load_settings()
    path = find_file(kw)
    print(f"工具：{tool.value}")
    print(f"文件：{path.name}")
    print(f"模型：{settings.llm_model}")

    t0 = time.time()
    doc = normalizer.normalize(path.read_bytes(), path.name, path.suffix.lstrip("."))
    ir = markdown_ir.build_ir(doc)
    kept = locator.locate(ir)
    segs = segmenter.segment(ir, kept, project_name=path.stem)
    if filter_re:
        pat = re.compile(filter_re)
        segs = [s for s in segs if pat.search(s.path)]
    if limit:
        segs = segs[:limit]
    print(f"S0-S3 完成：候选段 {len(segs)} 个（耗时 {time.time()-t0:.1f}s）")

    client = LiteLLMClient(settings.llm_model, settings.llm_base_url,
                           settings.llm_api_key, settings.llm_timeout_seconds)
    extractor = LlmExtractor(client, skills_dir=str(SRC_ROOT / "app" / "skills"))
    sem = asyncio.Semaphore(conc)

    t1 = time.time()
    results = await asyncio.gather(*[extract_one(extractor, s, tool.value, sem) for s in segs])
    entries: list[RawFpEntry] = [e for chunk in results for e in chunk]
    print(f"S4 完成：{len(entries)} 条原始条目（{time.time()-t1:.1f}s，并发 {conc}）")

    fp_list = validator.validate_and_merge(entries, tool)
    scales = assembler.build_fp_scale(fp_list, tool)
    items = assembler.build_cost_items(scales)
    total = sum(i.subtotal for i in items)

    types = Counter(e.fpType.value for e in fp_list)
    print(f"\n===== 结果 =====")
    print(f"功能点 {len(fp_list)} 条 | 类型分布 {dict(types)}")
    print(f"估算对象 {len(scales)} | 配置项 {len(items)} | 综合费用合计 {total/10000:.2f} 万元")
    print(f"\n--- 抽样（前 8 条）---")
    for e in fp_list[:8]:
        desc = (e.description or "")[:20]
        print(f"  [{e.fpType.value}] {e.softwareObject} / {e.requirementName}  ({desc})")

    out = SRC_ROOT / "evals" / "baseline" / f"extract_{kw}_{tool.value}.json"
    out.parent.mkdir(exist_ok=True)
    payload = {
        "file": path.name, "model": settings.llm_model, "tool": tool.value,
        "segments": len(segs), "elapsed_s": round(time.time() - t1, 1),
        "fpList": [e.model_dump(mode="json") for e in fp_list],
        "summary": {"count": len(fp_list), "types": dict(types), "totalYuan": float(total)},
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n已保存：{out}")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    kw = args[0]
    filter_re = args[args.index("--filter") + 1] if "--filter" in args else None
    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else None
    conc = int(args[args.index("--concurrency") + 1]) if "--concurrency" in args else 3
    tool = ToolCode(args[args.index("--tool") + 1]) if "--tool" in args else ToolCode.NO4_QUOTATION_REVIEW
    asyncio.run(run(kw, filter_re, limit, conc, tool))


if __name__ == "__main__":
    main()
