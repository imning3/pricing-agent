# -*- coding: utf-8 -*-
"""接口冒烟测试：覆盖契约的主要输入输出路径。

前置：服务已启动
    conda activate pricing-agent && cd src
    # 方式A 本机直跑（--doc 直接用 Windows 路径，默认即可）：
    LLM_BACKEND=litellm AGENT_DB_PATH=data/smoke.db python -m uvicorn app.main:app --port 8600
    # 方式B Docker（注意：--doc 必须用容器内路径 /data/testfiles/...，compose 已挂载测试语料）：
    docker compose up -d --build
    python scripts/api_smoke.py --doc "/data/testfiles/01HNSF02_需求规格说明（公开）/二期05数据模拟软件需求规格说明.docx"

用法：
    python scripts/api_smoke.py                          # 默认 http://127.0.0.1:8600 + 二期05 测试文档
    python scripts/api_smoke.py --docker                 # 服务在 Docker 容器（--doc 用容器内挂载路径）
    python scripts/api_smoke.py --base http://host:8600 --doc D:/path/xxx.docx
    python scripts/api_smoke.py --mock                   # mock 模式（跳过对话编辑断言）
    python scripts/api_smoke.py --body-file req.json     # ★粘请求体直接测：提交→轮询→存档响应
    python scripts/api_smoke.py --body '{"calculationId":...}'   （中文建议用 --body-file，避免 shell 转义）
    --body/--body-file 支持两种格式：原始 LlmRequest，或抓包格式 {"body": {...}, "headers": {...}}（自动解包）

覆盖路径：
    ① analysis 提交 → PROCESSING → 轮询 → SUCCESS（result 结构/单位/费用）
    ② conversation 传回功能点 → 编辑落地 + 全套表格重算
    ③ FAILED 路径（文件不存在 → errorMsg）
    ④ 任务不存在（status=null）
    ⑤ 幂等重提交（返回终态，不重复执行）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

DEFAULT_DOC = (Path(__file__).resolve().parent.parent.parent
               / "测试文件" / "01HNSF02_需求规格说明（公开）"
               / "二期05数据模拟软件需求规格说明.docx")
# 容器内挂载路径（compose: ../测试文件 → /data/testfiles）。
# 路径在代码内拼接而非命令行传入，规避 Git Bash 的 MSYS 路径自动转换。
DOCKER_DOC = "/data/testfiles/01HNSF02_需求规格说明（公开）/二期05数据模拟软件需求规格说明.docx"

PASS, FAIL = "  ✓", "  ✗"
failures: list[str] = []
log_lines: list[str] = []
RUN_DIR: Path | None = None


def check(cond: bool, label: str, detail: str = "") -> None:
    line = (PASS if cond else FAIL) + " " + label + (f"  [{detail}]" if detail else "")
    print(line)
    log_lines.append(line)
    if not cond:
        failures.append(label)


def save(name: str, request, response) -> None:
    """把每一步的请求体与完整响应体落盘，供人工查验接口真实返回。"""
    if RUN_DIR is None:
        return
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    (RUN_DIR / name).write_text(
        json.dumps({"request": request, "response": response}, ensure_ascii=False, indent=2),
        encoding="utf-8")


async def submit(c: httpx.AsyncClient, base: str, body: dict) -> dict:
    r = await c.post(f"{base}/api/v1/llm/tasks", json=body)
    return {"http": r.status_code, "body": r.json()}


async def poll(c: httpx.AsyncClient, base: str, cid: str, timeout: int = 300) -> dict:
    """轮询到终态（模拟后端：3s 间隔）。瞬时连接错误自动重试。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = await c.post(f"{base}/api/v1/llm/tasks/query", json={"calculationId": cid})
            q = r.json()
            if q["status"] != "PROCESSING":
                return q
        except httpx.ReadError:
            pass
        await asyncio.sleep(3)
    return {"status": "TIMEOUT", "errorMsg": f"{timeout}s 未到终态"}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8600")
    ap.add_argument("--doc", default=str(DEFAULT_DOC))
    ap.add_argument("--docker", action="store_true",
                    help="服务跑在 Docker：--doc 默认改用容器内挂载路径（Git Bash 下必须用本参数）")
    ap.add_argument("--mock", action="store_true", help="mock 模式（跳过对话编辑断言）")
    ap.add_argument("--token", default="", help="X-Api-Token（服务配置了鉴权时使用）")
    ap.add_argument("--body", default=None, help="直接传入请求体 JSON（原始 LlmRequest 或 {body,headers} 抓包格式）")
    ap.add_argument("--body-file", default=None, help="从文件读请求体（推荐：中文内容免 shell 转义问题）")
    args = ap.parse_args()
    base = args.base
    headers = {"X-Api-Token": args.token} if args.token else {}
    stamp = time.strftime("%H%M%S")
    global RUN_DIR
    RUN_DIR = Path(__file__).resolve().parent.parent / "evals" / "api_smoke" / f"run_{time.strftime('%Y%m%d')}_{stamp}"

    # ---- 粘请求体模式：只跑这一个请求，提交→轮询→存档→摘要 ----
    if args.body or args.body_file:
        raw_text = args.body if args.body else Path(args.body_file).read_text(encoding="utf-8")
        raw = json.loads(raw_text)
        if isinstance(raw, dict) and isinstance(raw.get("body"), dict):
            raw = raw["body"]  # 抓包格式解包
        if not raw.get("calculationId"):
            raw["calculationId"] = f"custom-{stamp}"
        cid = raw["calculationId"]
        async with httpx.AsyncClient(timeout=30, headers=headers) as c:
            print(f"目标服务：{base} | 请求体模式 | calculationId={cid} | type={raw.get('type')} tool={raw.get('toolCode')}")
            print(f"响应体存档：{RUN_DIR}\n")
            s = await submit(c, base, raw)
            save(f"custom_{cid}_submit.json", raw, s)
            print(f"提交：HTTP {s['http']} → {s['body']}")
            if s["http"] != 200 or s["body"].get("status") not in ("PROCESSING", "SUCCESS", "FAILED"):
                print(f"✗ 提交未受理：{s['body']}")
                sys.exit(1)
            q = await poll(c, base, cid, timeout=600)
            save(f"custom_{cid}_result.json", {"calculationId": cid}, q)
            print(f"终态：{q['status']}" + (f" | errorMsg: {q['errorMsg']}" if q.get("errorMsg") else ""))
            if q["status"] == "SUCCESS" and q.get("result"):
                r = q["result"]
                counts = {k: (len(v) if isinstance(v, list) else v) for k, v in r.items()}
                print(f"result 概览：{counts}")
                print(f"content：{(r['content'] or '')[:300]}")
                if r.get("originalFpList"):
                    e = r["originalFpList"][0]
                    print(f"功能点首条：{json.dumps(e, ensure_ascii=False)[:200]}")
            (RUN_DIR / "checks.txt").write_text(
                f"custom {cid} → {q['status']}\n{q.get('errorMsg') or ''}\n", encoding="utf-8")
            print(f"\n完整响应已存：{RUN_DIR / (f'custom_{cid}_result.json')}")
            sys.exit(0 if q["status"] == "SUCCESS" else 1)

    doc = DOCKER_DOC if (args.docker and args.doc == str(DEFAULT_DOC)) else args.doc

    async with httpx.AsyncClient(timeout=30, headers=headers) as c:
        print(f"目标服务：{base} | 测试文档：{Path(doc).name} | 模式：{'mock' if args.mock else 'litellm'}")
        print(f"响应体存档：{RUN_DIR}\n")

        # 0) 健康检查
        r = await c.get(f"{base}/api/v1/llm/health")
        check(r.status_code == 200, "0. 健康检查 /api/v1/llm/health", r.text)
        save("00_health.json", {"GET": "/api/v1/llm/health"}, {"http": r.status_code, "body": r.json()})

        # 1) analysis 全链路
        print("\n[1] analysis 提交 → 轮询 → SUCCESS")
        cid = f"smoke-{stamp}-a"
        req1 = {
            "calculationId": cid, "type": "analysis",
            "projectName": "二期05数据模拟软件", "toolCode": "NO4_QUOTATION_REVIEW",
            "files": [{"fileName": Path(doc).name, "fileType": "docx",
                       "category": "REQ_SPEC", "url": doc}],
        }
        s = await submit(c, base, req1)
        save("01_analysis_submit.json", req1, s)
        check(s["http"] == 200 and s["body"].get("status") == "PROCESSING",
              "1.1 提交受理（200 + PROCESSING）", str(s["body"]))
        q = await poll(c, base, cid)
        save("02_analysis_result.json", {"calculationId": cid}, q)
        check(q["status"] == "SUCCESS", f"1.2 轮询到 SUCCESS（{q.get('errorMsg') or 'ok'}）")
        if q["status"] == "SUCCESS":
            res = q["result"]
            fps = res["originalFpList"]
            check(bool(fps) and bool(res["fpScaleList"]) and bool(res["costItemList"]),
                  "1.3 result 三列表齐全", f"fp={len(fps)} scale={len(res['fpScaleList'])} cost={len(res['costItemList'])}")
            check(all(e["moduleSystem"] == "二期05数据模拟软件" for e in fps),
                  "1.4 moduleSystem=项目名（S3 v2）", dict.fromkeys(e["moduleSystem"] for e in fps))
            amounts = [float(i["subtotal"]) for i in res["costItemList"]]
            check(all(a > 100 for a in amounts), "1.5 金额单位为元（万元级数值）",
                  f"小计示例 {amounts[0]:.2f} 元，合计 {sum(amounts):.2f} 元")
            check(res.get("auditPriceList") is None and res.get("costDetailList") is None,
                  "1.6 非审价/非成本测算工具，两列表为 null")
            print(f"      content: {res['content'][:70]}")

            # 2) conversation 编辑落地 + 重算
            if q["status"] != "SUCCESS":
                print("\n[2] conversation 编辑落地 —— 跳过（analysis 未成功）")
            else:
                print("\n[2] conversation 编辑落地（结构化编辑→重算）")
                cid2 = f"smoke-{stamp}-c"
                target = fps[0]
                cfg = target["moduleConfig"]
                before = next(float(i["subtotal"]) for i in res["costItemList"] if i["moduleConfig"] == cfg)
                req2 = {
                    "calculationId": cid2, "type": "conversation",
                    "projectName": "二期05数据模拟软件", "toolCode": "NO4_QUOTATION_REVIEW",
                    "files": [], "originalFpList": fps,
                    "chatHistory": [{"role": "user",
                                     "content": f"把{target['requirementName']}的功能点个数改为3"}],
                }
                s2 = await submit(c, base, req2)
                save("03_conversation_submit.json", req2, s2)
                check(s2["http"] == 200, "2.1 对话任务受理")
                q2 = await poll(c, base, cid2)
                save("04_conversation_result.json", {"calculationId": cid2}, q2)
                check(q2["status"] == "SUCCESS", f"2.2 对话完成（{q2.get('errorMsg') or 'ok'}）")
                if q2["status"] == "SUCCESS":
                    r2 = q2["result"]
                    check(bool(r2["content"]), "2.3 有对话回复", (r2["content"] or "")[:50])
                    check(len(r2["originalFpList"]) == len(fps) and len(r2["costItemList"]) > 0,
                          "2.4 全量返回（列表+重算表格）",
                          f"fp={len(r2['originalFpList'])} cost={len(r2['costItemList'])}")
                    if not args.mock:
                        t2 = next((e for e in r2["originalFpList"]
                                   if e["requirementName"] == target["requirementName"]), None)
                        edited = t2 is not None and str(t2["fpCount"]) != str(target["fpCount"])
                        after = next((float(i["subtotal"]) for i in r2["costItemList"]
                                      if i["moduleConfig"] == cfg), None)
                        check(edited, "2.5 编辑真正落地（fpCount 已变）",
                              f"{target['fpCount']}→{t2['fpCount'] if t2 else '?'}")
                        check(after is not None and abs(after - before) > 0.01,
                              "2.6 费用同步重算", f"{before:.2f}→{after:.2f} 元")
                        check("已应用" in (r2["content"] or ""), "2.7 回复含【已应用】摘要")

        # 3) FAILED：文件不存在（仅真实模式——mock 管线不下载文件，坏路径也会成功）
        if args.mock:
            print("\n[3] FAILED 路径（mock 模式跳过：mock 不走文件下载）")
        else:
            print("\n[3] FAILED 路径")
            cid3 = f"smoke-{stamp}-f"
            req3 = {
                "calculationId": cid3, "type": "analysis", "projectName": "X",
                "toolCode": "NO4_QUOTATION",
                "files": [{"fileName": "x.docx", "fileType": "docx",
                           "category": "TECH_REQ", "url": "D:/not/exists.docx"}],
            }
            await submit(c, base, req3)
            q3 = await poll(c, base, cid3)
            save("05_failed_path.json", req3, q3)
            check(q3["status"] == "FAILED" and q3.get("errorMsg"),
                  "3.1 文件不存在 → FAILED + errorMsg", str(q3.get("errorMsg"))[:60])

        # 4) 任务不存在
        print("\n[4] 任务不存在")
        r4 = await c.post(f"{base}/api/v1/llm/tasks/query", json={"calculationId": "no-such-id"})
        b4 = r4.json()
        save("06_not_found.json", {"calculationId": "no-such-id"},
             {"http": r4.status_code, "body": b4})
        check(r4.status_code == 200 and b4["status"] is None
              and b4["errorMsg"] == "任务不存在或已过期",
              "4.1 未知任务 status=null + errorMsg", str(b4))

        # 5) 幂等重提交（回显当前状态——FAILED 也是正确行为，语义是"不重复执行"）
        print("\n[5] 幂等")
        s5 = await submit(c, base, {
            "calculationId": cid, "type": "analysis", "projectName": "二期05数据模拟软件",
            "toolCode": "NO4_QUOTATION_REVIEW",
            "files": [{"fileName": Path(doc).name, "fileType": "docx",
                       "category": "REQ_SPEC", "url": doc}],
        })
        save("07_idempotent_resubmit.json", {"calculationId": cid}, s5)
        check(s5["http"] == 200 and s5["body"].get("status") == q["status"],
              "5.1 同 calculationId 重提交回显当前状态（不报错不重复执行）", str(s5["body"]))

        # 6) 参数校验失败
        print("\n[6] 参数校验")
        r6 = await c.post(f"{base}/api/v1/llm/tasks", json={
            "calculationId": f"smoke-{stamp}-bad", "type": "analysis", "projectName": "X",
            "toolCode": "NO4_QUOTATION",
            "files": [{"fileName": "x.docx", "fileType": "docx",
                       "category": "TECH_REQUIREMENT", "url": "http://x"}],  # 契约外枚举值
        })
        save("08_bad_request.json",
             {"calculationId": f"smoke-{stamp}-bad",
              "files": [{"category": "TECH_REQUIREMENT"}]},
             {"http": r6.status_code, "body": r6.json() if r6.headers.get("content-type", "").startswith("application/json") else r6.text})
        check(r6.status_code == 400 and "errorMsg" in r6.text,
              "6.1 非法枚举 → HTTP 400 + errorMsg", r6.text[:70])

    # 落盘断言清单，与响应体同目录归档
    if RUN_DIR is not None:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        (RUN_DIR / "checks.txt").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        print(f"\n响应体与断言清单已存至：{RUN_DIR}")

    print("\n" + "=" * 46)
    if failures:
        print(f"结果：{len(failures)} 项失败 → {failures}")
        sys.exit(1)
    print("结果：全部通过 ✓")


if __name__ == "__main__":
    asyncio.run(main())
