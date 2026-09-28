"""env 驱动的服务配置。所有可变参数集中于此，业务代码不直接读环境变量。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str = ".env") -> None:
    """轻量 .env 加载（不引入 python-dotenv 依赖）：KEY=VALUE 逐行注入，不覆盖已有环境变量。"""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = val


@dataclass(frozen=True)
class Settings:
    host: str = "0.0.0.0"
    port: int = 8600
    db_path: str = "data/tasks.db"
    workers: int = 4
    task_ttl_hours: int = 24
    api_token: str = ""  # 空 = 不鉴权（仅开发）
    llm_backend: str = "mock"  # mock | litellm
    llm_model: str = "gpt-4o-mini"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_timeout_seconds: int = 120
    max_file_mb: int = 50


def load_settings() -> Settings:
    load_dotenv()

    def s(key: str, default: str) -> str:
        v = os.environ.get(key, "").strip()
        return v if v else default

    return Settings(
        host=s("AGENT_HOST", "0.0.0.0"),
        port=int(s("AGENT_PORT", "8600")),
        db_path=s("AGENT_DB_PATH", "data/tasks.db"),
        workers=int(s("AGENT_WORKERS", "4")),
        task_ttl_hours=int(s("AGENT_TASK_TTL_HOURS", "24")),
        api_token=os.environ.get("AGENT_API_TOKEN", "").strip(),
        llm_backend=s("LLM_BACKEND", "mock"),
        llm_model=s("LLM_MODEL", "gpt-4o-mini"),
        llm_base_url=os.environ.get("LLM_BASE_URL", "").strip(),
        llm_api_key=os.environ.get("LLM_API_KEY", "").strip(),
        llm_timeout_seconds=int(s("LLM_TIMEOUT_SECONDS", "120")),
        max_file_mb=int(s("AGENT_MAX_FILE_MB", "50")),
    )
