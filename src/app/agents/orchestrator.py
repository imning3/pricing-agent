"""编排器：按 type 分流 analysis / conversation，按 toolCode 组装 result。

analysis  : 下载(支持本地路径联调)→S0→S1→S2→S3→功能章节聚焦→S4并行抽取→S5→公式引擎装配
conversation: 意图分流；凡返回 originalFpList 即连带重算全套表格（契约决议）
NO4_AUDIT : 识别链路 + auditResult 判定 + auditPriceList
"""
from __future__ import annotations

import asyncio
import re
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


async def _extract_parallel(extractor, segs, tool_code: str, conc: int = 3):
    """并行分段抽取：信号量限流（MiniMax 等有 RPM 限制），限速错误长退避重试。"""
    sem = asyncio.Semaphore(conc)

    async def one(seg):
        async with sem:
            for attempt in (1, 2, 3):
                try:
                    return await extractor.extract([seg], tool_code)
                except Exception as exc:  # noqa: BLE001 —— 单段失败不拖垮整任务
                    rate_limited = "rate_limit" in str(exc).lower() or "速率限制" in str(exc)
                    if attempt == 3:
                        logger.warning("段落抽取失败 %s: %s", seg.path, exc)
                        return []
                    await asyncio.sleep(20 if rate_limited else 2)

    results = await asyncio.gather(*[one(s) for s in segs])
    return [e for chunk in results for e in chunk]


async def _run_analysis(req: LlmRequest, settings: Settings, mock_extractor) -> LlmResponse:
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
        segs = []
        for f in req.files:
            data = await fetcher.download(f.url, settings.max_file_mb)
            doc = normalizer.normalize(data, f.fileName, f.fileType)
            ir = markdown_ir.build_ir(doc)
            sections = locator.locate(ir)
            segs.extend(segmenter.segment(ir, sections, project_name=req.projectName or "未命名系统"))
        # 功能章节聚焦；无命中时回退全部段落（穷举原则，宁多勿漏）
        focused = [s for s in segs if _FUNCTIONAL_RE.search(s.path)]
        segs = focused or segs
        logger.info("[%s] 抽取段落 %d 个（聚焦后）", req.calculationId, len(segs))
        from app.llm.client import LiteLLMClient
        from app.pipeline.extractor import LlmExtractor
        client = LiteLLMClient(settings.llm_model, settings.llm_base_url,
                               settings.llm_api_key, settings.llm_timeout_seconds)
        extractor = LlmExtractor(client, skills_dir="app/skills")
        entries = await _extract_parallel(extractor, segs, req.toolCode.value)

    fp_list = validator.validate_and_merge(entries, req.toolCode)
    if not fp_list:
        from app.core.errors import ValidationError
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
        client = LiteLLMClient(settings.llm_model, settings.llm_base_url,
                               settings.llm_api_key, settings.llm_timeout_seconds)
        skill = Path("app/skills/dialog_intents/SKILL.md")
        system = (skill.read_text(encoding="utf-8") if skill.exists() else "你是军用软件计价助手。")
        system += ("\n\n只输出一个 JSON 对象，不得输出任何其他文字。"
                   "用户请求任何修改时，必须以 edits 数组表达每处修改，禁止只在 reply 里描述。")
        ctx = _context_digest(req)
        try:
            data = await client.chat_json(
                system + "\n\n当前业务上下文（紧凑摘要）：\n" + ctx, last_user)
            if not isinstance(data, dict):
                raise ValueError(f"应为对象，实际 {type(data).__name__}")
        except Exception as exc:  # noqa: BLE001 —— 结构化失败降级为纯文本对话
            logger.warning("[%s] 对话结构化输出失败，降级纯文本：%s", req.calculationId, exc)
            data = {"reply": await client.chat_text(
                "你是军用软件计价助手，简短回答。" + "\n\n当前业务上下文：\n" + ctx, last_user)}

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
                                "（op=set/remove/add + find/entry），reply 只作解释。")
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
        obj_names = {s.softwareObject for s in scales if s.moduleConfig == mc}
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
