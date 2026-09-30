# -*- coding: utf-8 -*-
"""大模型 API 可用性自检：不启动服务，用与服务一致的配置链路（.env + 环境变量）发真实调用。

用途：
    部署前验证模型网关可达、密钥有效、模型能回文本与结构化 JSON（S4/S5 管线硬依赖）；
    服务异常时快速定位是模型侧问题还是服务自身问题。

用法（cd src 后运行；自动读 .env，命令行参数覆盖同名配置）：
    python scripts/check_llm.py                     # 用当前 LLM_* 配置直测
    python scripts/check_llm.py --base-url http://<内网模型网关> --model anthropic/<模型名> --api-key <密钥>
    python scripts/check_llm.py --timeout 60        # 内网网关慢时放宽

检查项：
    1) litellm 依赖可导入（LLM_BACKEND=litellm 的前提）
    2) 网关连通性（HTTP 层建连即可，任意状态码都算通——只看网络路径）
    3) 文本补全（一次真实 chat 调用，测时延与回包）
    4) 结构化 JSON 输出（chat_json 可解析出对象）

退出码：0 = 全部通过；1 = 有失败项（可嵌入部署自检脚本）。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

import httpx

# 内网适配：必须在 litellm 导入（LiteLLMClient 构造）之前设置，
# 否则 litellm 会拉外网价格表 raw.githubusercontent.com，重试 3 次每次约 10s
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

SRC_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC_ROOT))

from app.core.config import load_settings
from app.llm.client import LiteLLMClient

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(("  ✓ " if ok else "  ✗ ") + label + (f"  [{detail}]" if detail else ""))
    if not ok:
        failures.append(label)


def brief(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:120]


def mask(key: str) -> str:
    return key[:6] + "***" if key else "(空)"


async def probe_http(base_url: str, timeout: int) -> tuple[bool, str]:
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=min(timeout, 10)) as c:
            r = await c.get(base_url)
        return True, f"HTTP {r.status_code}，{time.time()-t0:.2f}s（任意状态码均视为网络可达）"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {brief(exc)}"


async def main() -> int:
    ap = argparse.ArgumentParser(description="大模型 API 可用性自检")
    ap.add_argument("--model", help="覆盖 LLM_MODEL")
    ap.add_argument("--base-url", help="覆盖 LLM_BASE_URL")
    ap.add_argument("--api-key", help="覆盖 LLM_API_KEY")
    ap.add_argument("--timeout", type=int, help="覆盖 LLM_TIMEOUT_SECONDS")
    args = ap.parse_args()

    st = load_settings()
    model = args.model or st.llm_model
    base_url = (args.base_url or st.llm_base_url).strip()
    api_key = args.api_key or st.llm_api_key
    timeout = args.timeout or st.llm_timeout_seconds
    max_tokens = st.llm_max_tokens

    print(f"配置：backend={st.llm_backend} | model={model} | base_url={base_url or '(默认官方)'} "
          f"| key={mask(api_key)} | timeout={timeout}s")
    if st.llm_backend != "litellm":
        print("  提示：当前 LLM_BACKEND != litellm（服务会用 mock），本脚本仍直测上述 LLM_* 目标。")
    if not base_url:
        print("  提示：未配置 LLM_BASE_URL，将按模型前缀路由到官方 API（内网环境会失败）。")

    # 1) litellm 依赖
    try:
        client = LiteLLMClient(model, base_url, api_key, timeout, max_tokens=max_tokens)
        check(True, "1. litellm 依赖可导入", getattr(client._litellm, "__version__", "ok"))
    except Exception as exc:
        check(False, "1. litellm 依赖可导入", brief(exc))
        print("\n结果：litellm 不可用，后续检查无法进行")
        return 1

    # 2) 网关连通性
    if base_url:
        ok, detail = await probe_http(base_url, timeout)
        check(ok, "2. 网关连通性", detail)
    else:
        check(True, "2. 网关连通性", "跳过（无 base_url，走官方 API）")

    # 3) 文本补全
    t0 = time.time()
    try:
        text = await client.chat_text("你是接口可用性检测助手。", "请原样回复两个字符：OK")
        check(bool(text.strip()), "3. 文本补全",
              f"{time.time()-t0:.1f}s，回包 {text.strip()[:30]!r}")
    except Exception as exc:
        check(False, "3. 文本补全", f"{time.time()-t0:.1f}s，{brief(exc)}")

    # 4) 结构化 JSON 输出（管线依赖）
    t0 = time.time()
    try:
        data = await client.chat_json(
            "只输出一个 JSON 对象，不要输出任何其他文字、代码块标记或解释。",
            '请输出 {"status": "ok", "value": 42}')
        ok = isinstance(data, dict) and "status" in data
        check(ok, "4. 结构化 JSON 输出",
              f"{time.time()-t0:.1f}s，解析结果 {str(data)[:60]}")
    except Exception as exc:
        check(False, "4. 结构化 JSON 输出", f"{time.time()-t0:.1f}s，{brief(exc)}")

    print("\n" + "=" * 46)
    if failures:
        print(f"结果：{len(failures)} 项失败 → {failures}")
        return 1
    print("结果：大模型 API 可用 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
