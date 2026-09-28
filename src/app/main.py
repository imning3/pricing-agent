"""FastAPI 入口。启动顺序：建库 → 重启恢复 → 起 worker 池 → 挂路由。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.agents.orchestrator import make_executor
from app.api.routes import router
from app.core.config import load_settings
from app.core.logging import get_logger, setup_logging
from app.tasks.manager import TaskManager

logger = get_logger(__name__)


def create_app() -> FastAPI:
    settings = load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging()
        manager = TaskManager(
            db_path=settings.db_path,
            workers=settings.workers,
            ttl_hours=settings.task_ttl_hours,
            executor=make_executor(settings),
        )
        manager.startup()
        await manager.start_workers()
        app.state.settings = settings
        app.state.task_manager = manager
        logger.info(
            "智能体服务就绪 port=%s workers=%s llm=%s db=%s",
            settings.port, settings.workers, settings.llm_backend, settings.db_path,
        )
        yield
        logger.info("智能体服务关闭")

    app = FastAPI(title="智能计价·大模型智能体服务", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def _validation_to_400(request: Request, exc: RequestValidationError):
        # 契约约定：参数校验失败统一 HTTP 400 + errorMsg（而非 FastAPI 默认 422/detail）
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", []) if x != "body")
        msg = first.get("msg", "非法请求")
        # 只记字段位置与错误类型，不落字段值（敏感性约定）
        get_logger("api").warning("参数校验失败 %s %s：路径=%s 原因=%s",
                                  request.method, request.url.path, loc or "(body)", msg)
        return JSONResponse(status_code=400, content={"errorMsg": f"参数校验失败：{loc} {msg}"})

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        # 统一错误形态 {"errorMsg": ...}（如 401），并落日志便于服务端排查后端调用失败
        if isinstance(exc.detail, dict):
            msg = exc.detail.get("errorMsg", str(exc.detail))
        else:
            msg = str(exc.detail)
        get_logger("api").warning("HTTP %s %s：%s", exc.status_code, request.url.path, msg)
        return JSONResponse(status_code=exc.status_code, content={"errorMsg": msg})

    app.include_router(router)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = load_settings()
    uvicorn.run(app, host=settings.host, port=settings.port)
