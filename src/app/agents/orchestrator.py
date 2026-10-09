"""编排器：按 type 分流 analysis / conversation，按 toolCode 组装 result。

analysis  : 下载(支持本地路径联调)→S0→S1→S2→S3→功能章节聚焦→S4并行抽取→S5→公式引擎装配
conversation: 意图分流；凡返回 originalFpList 即连带重算全套表格（契约决议）
NO4_AUDIT : 识别链路 + auditResult 判定 + auditPriceList

方案 A 性能：
- S4 短段 pack 合并调用
- 解析 IR / 段抽取磁盘缓存
- 阶段化 max_tokens
- 自适应并发 + 抖动指数退避
"""
from __future__ import annotations

import asyncio
import hashlib
import random
import re
import time
from decimal import Decimal

from app.core.config import Settings
from app.core.logging import get_logger
from app.engine import assembler
from app.pipeline import fetcher, locator, markdown_ir, normalizer, segmenter, validator
from app.pipeline.extractor import MockExtractor
from app.schemas.contract import (
    AuditPrice,
    LlmRequest,
    LlmResponse,
    OriginalFp,
    ToolCode,
)

logger = get_logger(__name__)
D = Decimal

# 功能章节聚焦（与评测口径一致）：非功能章节（可靠性/保密性/环境…）不产功能点
_FUNCTIONAL_RE = re.compile(r"功能|能力|接口|数据|研制|业务|需求")
# 对话回复中出现修改叙述的特征（用于检测"模型说了改但没给 edits"）
_EDIT_HINT_RE = re.compile(r"改为|改成|修改|删除|新增|增加|调整|设为|调到|变为")


def make_executor(settings: Settings):
    """TaskManager 的执行回调工厂。"""
    mock_extractor = MockExtractor()

    async def execute(req: LlmRequest) -> LlmResponse:
        # 后端真实用法（2026-09-28 抓包确认）：首轮"对话"携带 files 且无历史功能点，
        # 用户语义是触发测算（"请基于上传的文件进行测算"）→ 路由到分析管线
        if req.type.value == "analysis" or (req.files and not req.originalFpList):
            return await _run_analysis(req, settings, mock_extractor)
        return await _run_conversation(req, settings, mock_extractor)

    return execute


def _is_rate_limited(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in ("rate_limit", "rate limit", "too many requests", "429", "速率限制", "超限"))


async def _backoff_sleep(attempt: int, rate_limited: bool) -> None:
    """抖动指数退避：限速 2/4/8s…，普通错误 1/2/4s…，均加随机抖动，避免惊群。"""
    base = 2.0 if rate_limited else 1.0
    delay = min(30.0, base * (2 ** (attempt - 1)))
    delay *= 0.7 + random.random() * 0.6
    await asyncio.sleep(delay)


class _AdaptivePool:
    """简单自适应并发：成功缓慢爬升，限速立刻降半。"""

    def __init__(self, limit: int):
        self.limit = max(1, int(limit))
        self._current = self.limit
        self._inflight = 0
        self._cond = asyncio.Condition()
        self._success_streak = 0

    async def acquire(self) -> None:
        async with self._cond:
            while self._inflight >= self._current:
                await self._cond.wait()
            self._inflight += 1

    async def release(self, *, rate_limited: bool = False, success: bool = False) -> None:
        async with self._cond:
            self._inflight = max(0, self._inflight - 1)
            if rate_limited:
                self._current = max(1, self._current // 2)
                self._success_streak = 0
            elif success:
                self._success_streak += 1
                if self._success_streak >= 3 and self._current < self.limit:
                    self._current += 1
                    self._success_streak = 0
            self._cond.notify_all()


async def _extract_parallel(extractor, segs, tool_code: str, mode: str = "spec", conc: int = 6,
                            pack_max_chars: int = 10000, pack_max_segments: int = 8,
                            cache=None, cache_namespace: str = "extract",
                            skill_ver: str = "", model: str = ""):
    """并行包抽取：短段合并 + 信号量/自适应并发 + 段级缓存 + 抖动退避。

    返回 (entries, failures)：failures 为最终仍失败的 (path, 异常) 列表——
    零条目且存在失败时，编排层据此如实上报 LLM 故障根因（勿误报为文档问题）。
    """
    from app.pipeline.extractor import (
        entries_from_cache,
        entries_to_cache,
        pack_segments,
    )

    packs = pack_segments(segs, max_chars=pack_max_chars, max_segments=pack_max_segments)
    pool = _AdaptivePool(conc)
    logger.info("S4 打包：%d 段 → %d 包（max_chars=%s max_segs=%s conc≤%s）",
                len(segs), len(packs), pack_max_chars, pack_max_segments, conc)

    def pack_cache_key(pack) -> str:
        h = hashlib.sha256()
        h.update(tool_code.encode())
        h.update(mode.encode())
        h.update(skill_ver.encode())
        h.update(model.encode())
        for seg in pack:
            h.update(seg.moduleSystem.encode("utf-8", "ignore"))
            h.update(b"|")
            h.update(seg.moduleConfig.encode("utf-8", "ignore"))
            h.update(b"|")
            h.update(seg.softwareObject.encode("utf-8", "ignore"))
            h.update(b"|")
            h.update(seg.path.encode("utf-8", "ignore"))
            h.update(b"|")
            h.update((seg.content or "").encode("utf-8", "ignore"))
            h.update(b"\n")
        return h.hexdigest()

    async def one(pack):
        label = pack[0].path if len(pack) == 1 else f"{pack[0].path}等{len(pack)}段"
        key = pack_cache_key(pack)
        if cache is not None:
            cached = cache.get(cache_namespace, key)
            if cached is not None:
                try:
                    return entries_from_cache(cached), None, True
                except Exception as exc:  # noqa: BLE001
                    logger.debug("抽取缓存反序列化失败，忽略：%s", exc)

        await pool.acquire()
        rate_limited = False
        success = False
        try:
            for attempt in (1, 2, 3):
                try:
                    # 优先走 pack 接口；旧 extractor 仅有 extract 时回退
                    if hasattr(extractor, "extract_pack"):
                        entries = await extractor.extract_pack(pack, tool_code, mode)
                    else:
                        entries = await extractor.extract(pack, tool_code, mode)
                    success = True
                    if cache is not None:
                        cache.set(cache_namespace, key, entries_to_cache(entries))
                    return entries, None, False
                except Exception as exc:  # noqa: BLE001 —— 单包失败不拖垂整任务
                    rate_limited = _is_rate_limited(exc)
                    if attempt == 3:
                        logger.warning("段落抽取失败 %s: %s", label, exc)
                        return [], (label, exc), False
                    await _backoff_sleep(attempt, rate_limited)
        finally:
            await pool.release(rate_limited=rate_limited, success=success)

    t0 = time.perf_counter()
    results = await asyncio.gather(*[one(p) for p in packs])
    entries = [e for chunk, _, _ in results for e in chunk]
    failures = [f for _, f, _ in results if f]
    cache_hits = sum(1 for _, _, hit in results if hit)
    logger.info("S4 完成：%d 条 / %d 包（缓存命中 %d，耗时 %.1fs）",
                len(entries), len(packs), cache_hits, time.perf_counter() - t0)
    return entries, failures


async def _run_analysis(req: LlmRequest, settings: Settings, mock_extractor) -> LlmResponse:
    failures: list = []  # 模型调用失败的段落（仅 litellm 分支填充）
    segs = []
    if settings.llm_backend == "mock":
        # mock 模式：不联网，从文件名合成确定性分段
        segs = [
            segmenter.Segment(
                moduleSystem=req.projectName or "未命名系统",
                moduleConfig=f.fileName.rsplit(".", 1)[0],
                softwareObject=f.fileName.rsplit(".", 1)[0],
                content="(mock 内容)",
                path=f.fileName,
            )
            for f in req.files
        ] or [segmenter.Segment(req.projectName or "未命名系统", "默认配置项", "默认估算对象", "(mock)", "(正文)")]
        entries = await mock_extractor.extract(segs, req.toolCode.value)
    else:
        from pathlib import Path

        from app.llm.client import LiteLLMClient
        from app.pipeline.cache import DiskCache, content_hash, skill_version
        from app.pipeline.extractor import LlmExtractor
        from app.pipeline.splitter import MODE_BY_TOOL, split_spec, split_system

        # 绝对路径定位 skills（原 cwd 相对路径在非 src/ 工作目录下会静默丢失 skill）
        skills_dir = str(Path(__file__).resolve().parent.parent / "skills")
        # 拆分模式硬映射：①-④建设方案类→系统模式（拆到软件级）；⑤需规类→软件模式（拆到功能块级）
        mode = MODE_BY_TOOL.get(req.toolCode.value, "spec")
        client = LiteLLMClient(settings.llm_model, settings.llm_base_url,
                               settings.llm_api_key, settings.llm_timeout_seconds,
                               max_tokens=settings.llm_max_tokens)
        extractor = LlmExtractor(client, skills_dir=skills_dir,
                                 max_tokens=settings.llm_extract_max_tokens)
        cache = DiskCache(settings.cache_dir, enabled=settings.cache_enabled,
                          ttl_hours=settings.cache_ttl_hours)
        skill_ver = skill_version(skills_dir, extractor.skill_names(req.toolCode.value))
        split_skill = ("split_system",) if mode == "system" else ("split_spec",)
        split_ver = skill_version(skills_dir, split_skill)

        # 给拆分调用注入阶段 max_tokens（不改 splitter 签名，用薄包装）
        class _SplitClient:
            def __init__(self, inner, max_tokens: int):
                self._inner = inner
                self._max_tokens = max_tokens

            async def chat_json(self, system: str, user: str, *, max_tokens: int | None = None):
                return await self._inner.chat_json(
                    system, user, max_tokens=max_tokens or self._max_tokens)

            async def chat_text(self, system: str, user: str, *, max_tokens: int | None = None):
                return await self._inner.chat_text(
                    system, user, max_tokens=max_tokens or self._max_tokens)

        split_client = _SplitClient(client, settings.llm_split_max_tokens)

        for f in req.files:
            data = await fetcher.download(f.url, settings.max_file_mb)
            file_digest = content_hash(data)
            parse_key = f"{file_digest}|{f.fileName}|{f.fileType}|{req.projectName or ''}|{mode}|{split_ver}|{settings.llm_model}"
            cached_parse = cache.get("parse_segs", parse_key)
            if cached_parse is not None:
                try:
                    file_segs = [
                        segmenter.Segment(
                            moduleSystem=row["moduleSystem"],
                            moduleConfig=row["moduleConfig"],
                            softwareObject=row["softwareObject"],
                            content=row.get("content") or "",
                            path=row.get("path") or "",
                        )
                        for row in cached_parse
                    ]
                    logger.info("[%s] 文件 %s 命中解析缓存 %d 段",
                                req.calculationId, f.fileName, len(file_segs))
                    segs.extend(file_segs)
                    continue
                except Exception as exc:  # noqa: BLE001
                    logger.debug("解析缓存反序列化失败，忽略：%s", exc)

            doc = normalizer.normalize(data, f.fileName, f.fileType)
            ir = markdown_ir.build_ir(doc)
            sections = locator.locate(ir)
            rule_segs = segmenter.segment(ir, sections, project_name=req.projectName or "未命名系统")
            if mode == "system":
                file_segs, split_ok = await split_system(split_client, ir, sections, rule_segs, skills_dir)
            else:
                file_segs, split_ok = await split_spec(split_client, ir, sections, rule_segs, skills_dir)
            if not split_ok:
                logger.info("[%s] 文件 %s LLM 拆分降级，沿用规则初分 %d 段",
                            req.calculationId, f.fileName, len(file_segs))
            # 功能章节聚焦仅在软件模式/拆分降级时使用（关键词粗筛兜底）；
            # 系统模式拆分成功时 LLM 已做语义级 skip，再按路径关键词过滤反而误伤
            if mode == "spec" or not split_ok:
                focused = [s for s in file_segs if _FUNCTIONAL_RE.search(s.path)]
                file_segs = focused or file_segs
            cache.set("parse_segs", parse_key, [
                {
                    "moduleSystem": s.moduleSystem,
                    "moduleConfig": s.moduleConfig,
                    "softwareObject": s.softwareObject,
                    "content": s.content,
                    "path": s.path,
                }
                for s in file_segs
            ])
            segs.extend(file_segs)

        logger.info("[%s] 抽取段落 %d 个（模式=%s，聚焦后）", req.calculationId, len(segs), mode)
        entries, failures = await _extract_parallel(
            extractor, segs, req.toolCode.value, mode,
            conc=settings.llm_concurrency,
            pack_max_chars=settings.llm_pack_max_chars,
            pack_max_segments=settings.llm_pack_max_segments,
            cache=cache,
            skill_ver=skill_ver,
            model=settings.llm_model,
        )

    fp_list = validator.validate_and_merge(entries, req.toolCode)
    if not fp_list:
        from app.core.errors import LLMError, ValidationError
        if failures and not entries:
            # 全部段落模型调用失败（如模型名/网关配置错）——上报真实根因，勿误报为文档问题
            last_path, last_exc = failures[-1]
            raise LLMError(f"全部 {len(segs)} 个段落的模型抽取均失败"
                           f"（示例 {last_path}: {str(last_exc)[:160]}），请检查 LLM_MODEL/LLM_BASE_URL/LLM_API_KEY 配置")
        raise ValidationError("未能从文档中识别出任何功能点，请检查文件内容与类型")

    scales = assembler.build_fp_scale(fp_list, req.toolCode)
    items = assembler.build_cost_items(scales)
    content = (
        f"已完成识别与测算：{len(fp_list)} 条功能需求、"
        f"{len({(e.moduleSystem, e.moduleConfig) for e in fp_list})} 个配置项、"
        f"综合费用合计 {sum(i.subtotal for i in items) / 10000:.2f} 万元。"
    )
    resp = LlmResponse(content=content, originalFpList=fp_list, fpScaleList=scales, costItemList=items)

    if req.toolCode == ToolCode.NO4_AUDIT:
        resp.originalFpList = _mark_audit_results(fp_list)
        resp.auditPriceList = _build_audit_prices(fp_list, scales, items)
    if req.toolCode == ToolCode.COST_MEASUREMENT:
        resp.costDetailList = assembler.build_cost_details(fp_list, scales, items)
    return resp


async def _run_conversation(req: LlmRequest, settings: Settings, mock_extractor) -> LlmResponse:
    last_user = next((m.content for m in reversed(req.chatHistory) if m.role.value == "user"), "")
    fp_list = req.originalFpList

    if settings.llm_backend == "mock":
        reply = f"（mock）已收到您的需求：{last_user[:100]}"
    else:
        from pathlib import Path

        from app.llm.client import LiteLLMClient
        from app.pipeline.editor import apply_edits

        skills_dir = Path(__file__).resolve().parent.parent / "skills"
        skill = skills_dir / "dialog_intents" / "SKILL.md"
        system = skill.read_text(encoding="utf-8") if skill.exists() else (
            "你是军用软件计价助手。输出 JSON：{\"reply\": str, \"edits\": []}"
        )
        client = LiteLLMClient(settings.llm_model, settings.llm_base_url,
                               settings.llm_api_key, settings.llm_timeout_seconds,
                               max_tokens=settings.llm_dialog_max_tokens)
        ctx = _context_digest(req)
        try:
            data = await client.chat_json(
                system + "\n\n当前业务上下文（紧凑摘要）：\n" + ctx,
                last_user,
                max_tokens=settings.llm_dialog_max_tokens,
            )
            if not isinstance(data, dict):
                raise ValueError("对话输出应为 JSON 对象")
        except Exception as exc:  # noqa: BLE001 —— 结构化失败降级纯文本
            logger.warning("[%s] 对话结构化输出失败，降级纯文本：%s", req.calculationId, exc)
            data = {"reply": await client.chat_text(
                "你是军用软件计价助手，简短回答。" + "\n\n当前业务上下文：\n" + ctx, last_user,
                max_tokens=settings.llm_dialog_max_tokens)}

        reply = str(data.get("reply") or "").strip()
        edits = data.get("edits") or []

        # 纠正重试：模型在 reply 里叙述了修改却没输出 edits（实测偶发）→ 带纠错指令重问一次
        if fp_list and not edits and _EDIT_HINT_RE.search(reply):
            logger.info("[%s] 对话 edits 缺失但 reply 含修改叙述，纠正重试", req.calculationId)
            try:
                data2 = await client.chat_json(
                    system + "\n\n当前业务上下文（紧凑摘要）：\n" + ctx,
                    last_user + "\n\n【系统纠正】你刚才只在 reply 中描述了修改，但没有输出 edits 数组。"
                                "请重新输出 JSON：把每处修改写成 edits 中的操作对象"
                                "（op=set/remove/add + find/entry），reply 只作解释。",
                    max_tokens=settings.llm_dialog_max_tokens,
                )
                if isinstance(data2, dict) and data2.get("edits"):
                    data = data2
                    reply = str(data2.get("reply") or reply).strip()
                    edits = data2["edits"]
            except Exception as exc:  # noqa: BLE001 —— 纠正失败保持原样
                logger.warning("[%s] 对话纠正重试失败：%s", req.calculationId, exc)
        # 编辑落地：代码确定性应用模型的结构化编辑指令（用户原话作名称歧义裁决），附上实际应用结果
        if fp_list and edits:
            fp_list, outcome = apply_edits(fp_list, edits, req.toolCode, user_message=last_user)
            logger.info("[%s] 对话编辑指令 %d 条 → 应用 %d / 失败 %d",
                        req.calculationId, len(edits), len(outcome.applied), len(outcome.failed))
            parts = [reply] if reply else []
            if outcome.applied:
                parts.append("【已应用】\n" + "\n".join(f"- {p}" for p in outcome.applied))
            if outcome.failed:
                parts.append("【未应用】\n" + "\n".join(f"- {p}" for p in outcome.failed))
            reply = "\n\n".join(parts) or "（编辑未能应用，请换一种描述，或直接在表格中修改）"
        elif not reply:
            reply = "（未生成回复）"

    # 契约决议：对话凡涉及功能点（传回了 originalFpList），同步重算全套表格全量返回
    if not fp_list:
        return LlmResponse(content=reply)

    scales = assembler.build_fp_scale(fp_list, req.toolCode)
    items = assembler.build_cost_items(scales)
    resp = LlmResponse(content=reply, originalFpList=fp_list,
                       fpScaleList=scales, costItemList=items)
    if req.toolCode == ToolCode.COST_MEASUREMENT:
        resp.costDetailList = assembler.build_cost_details(fp_list, scales, items)
    return resp


def _context_digest(req: LlmRequest) -> str:
    """对话上下文紧凑编码：CSV 风格、仅判断所需列（AGENTS.md 对话预处理约定）。"""
    lines = ["[原始功能点] 系统|配置项|估算对象|需求|类型|个数"]
    for e in req.originalFpList[:200]:
        lines.append(f"{e.moduleSystem}|{e.moduleConfig}|{e.softwareObject}|{e.requirementName}|{e.fpType.value}|{e.fpCount}")
    lines.append("[综合费用] 配置项|规模|工作量|小计(元)")
    for i in req.costItemList[:100]:
        lines.append(f"{i.moduleConfig}|{i.fpScale}|{i.workload}|{i.subtotal}")
    return "\n".join(lines)


def _mark_audit_results(fp_list: list[OriginalFp]) -> list[OriginalFp]:
    """审价合理性标注：模型已判定的保留（真实链路），缺失的按确定性规则补齐（mock 链路）。"""
    for idx, e in enumerate(fp_list):
        if e.auditResult is None:
            e.auditResult = 0 if idx % 7 == 6 else 1
    return fp_list


def _build_audit_prices(fp_list: list[OriginalFp], scales, items) -> list[AuditPrice]:
    """auditPriceList：mock 口径——报价=我方测算全额上浮 10%，审价=剔除不合理项后重折算。"""
    from app.engine import formulas as F
    out: list[AuditPrice] = []
    configs = []
    for s in scales:
        if s.moduleConfig not in configs:
            configs.append(s.moduleConfig)
    for mc in configs:
        quote_wan = next((i.subtotal for i in items if i.moduleConfig == mc), D(0)) / D(10000) * D("1.10")
        # fpCount 已是原始功能点数（权重×个数），不合理条目按 fpCount 直接求和剔除
        unreasonable = sum(
            (e.fpCount for e in fp_list if e.moduleConfig == mc and e.auditResult == 0), D(0))
        total_fp = sum((s.originalFp for s in scales if s.moduleConfig == mc), D(0))
        audited_wan = quote_wan * (D(1) - unreasonable / total_fp) if total_fp else quote_wan
        out.append(AuditPrice(
            moduleConfig=mc,
            fpScaleQuotation=F.q2(total_fp),
            outsourcePrice=F.to_yuan(quote_wan),
            fpScaleAudit=F.q2(total_fp - unreasonable),
            auditPrice=F.to_yuan(audited_wan),
        ))
    return out
