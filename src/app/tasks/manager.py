"""任务管理器：asyncio worker 池 + SQLite 运行态存储。

设计要点（决议见 AGENTS.md §2.1）：
- 幂等：同一 calculationId 重复提交 → 返回该任务当前状态，不报错；
- TTL：终态任务超过 ttl 后视为"任务不存在或已过期"；
- 重启恢复：遗留 PROCESSING 置 FAILED（可重试），由后端重提。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from app.core.logging import get_logger
from app.schemas.contract import LlmRequest, LlmResponse, TaskStatus

logger = get_logger(__name__)

_EXPIRED_MSG = "任务不存在或已过期"


@dataclass
class TaskRow:
    calculationId: str
    status: TaskStatus
    result: Optional[LlmResponse]
    errorMsg: Optional[str]


class TaskManager:
    def __init__(self, db_path: str, workers: int, ttl_hours: int,
                 executor: Callable[[LlmRequest], Awaitable[LlmResponse]]):
        self._db_path = db_path
        self._workers = workers
        self._ttl_seconds = ttl_hours * 3600
        self._executor = executor
        self._queue: asyncio.Queue[LlmRequest] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    # ---------- 生命周期 ----------

    def startup(self) -> None:
        d = os.path.dirname(self._db_path)
        if d:
            os.makedirs(d, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS tasks (
                calculation_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                result_json TEXT,
                error_msg TEXT,
                payload_hash TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )"""
        )
        self._conn.commit()
        self._recover_on_boot()

    def _recover_on_boot(self) -> None:
        cur = self._conn.execute(
            "UPDATE tasks SET status=?, error_msg=?, updated_at=? WHERE status=?",
            (TaskStatus.FAILED.value, "服务重启，任务中断，请重新提交", time.time(), TaskStatus.PROCESSING.value),
        )
        self._conn.commit()
        if cur.rowcount:
            logger.warning("重启恢复：%d 个 PROCESSING 任务置为 FAILED", cur.rowcount)

    async def start_workers(self) -> None:
        for i in range(self._workers):
            asyncio.create_task(self._worker(i), name=f"agent-worker-{i}")

    # ---------- 对外操作 ----------

    async def submit(self, req: LlmRequest) -> TaskStatus:
        """幂等语义（2026-09-28 完善）：
        - PROCESSING 中重复提交 → 回 PROCESSING，不重复执行；
        - SUCCESS 后重复提交 → 回 SUCCESS（查询可取原结果），不重复执行；
        - **FAILED 后重复提交 → 视为重试，重新入队执行**（契约：失败由后端重提，同 id 重提即重试）；
        - 载荷不一致：仅记 warning，不改变上述行为。
        """
        payload_hash = hashlib.sha256(req.model_dump_json().encode("utf-8")).hexdigest()
        async with self._lock:
            row = self._conn.execute(
                "SELECT status, payload_hash FROM tasks WHERE calculation_id=?",
                (req.calculationId,),
            ).fetchone()
            if row:
                status = TaskStatus(row[0])
                if row[1] != payload_hash:
                    logger.warning("幂等命中但载荷不一致：calculationId=%s，按既有语义处理", req.calculationId)
                if status in (TaskStatus.PROCESSING, TaskStatus.SUCCESS):
                    return status
                # FAILED → 重试：状态回到 PROCESSING 并重新入队
                self._conn.execute(
                    "UPDATE tasks SET status=?, error_msg=NULL, result_json=NULL, "
                    "payload_hash=?, updated_at=? WHERE calculation_id=?",
                    (TaskStatus.PROCESSING.value, payload_hash, time.time(), req.calculationId),
                )
                self._conn.commit()
                logger.info("FAILED 任务重试重新入队：%s", req.calculationId)
            else:
                self._conn.execute(
                    "INSERT INTO tasks (calculation_id, status, created_at, updated_at, payload_hash) VALUES (?,?,?,?,?)",
                    (req.calculationId, TaskStatus.PROCESSING.value, time.time(), time.time(), payload_hash),
                )
                self._conn.commit()
        await self._queue.put(req)
        return TaskStatus.PROCESSING

    def query(self, calculation_id: str) -> Optional[TaskRow]:
        row = self._conn.execute(
            "SELECT status, result_json, error_msg, updated_at FROM tasks WHERE calculation_id=?",
            (calculation_id,),
        ).fetchone()
        if not row:
            return None
        status = TaskStatus(row[0])
        if status in (TaskStatus.SUCCESS, TaskStatus.FAILED) and time.time() - row[3] > self._ttl_seconds:
            self._purge(calculation_id)
            return None
        result = LlmResponse.model_validate_json(row[1]) if row[1] else None
        return TaskRow(calculation_id, status, result, row[2])

    # ---------- 内部 ----------

    def _purge(self, calculation_id: str) -> None:
        self._conn.execute("DELETE FROM tasks WHERE calculation_id=?", (calculation_id,))
        self._conn.commit()

    def _set_terminal(self, calculation_id: str, status: TaskStatus,
                      result: Optional[LlmResponse], error_msg: Optional[str]) -> None:
        self._conn.execute(
            "UPDATE tasks SET status=?, result_json=?, error_msg=?, updated_at=? WHERE calculation_id=?",
            (
                status.value,
                result.model_dump_json() if result else None,
                error_msg,
                time.time(),
                calculation_id,
            ),
        )
        self._conn.commit()

    async def _worker(self, idx: int) -> None:
        while True:
            req = await self._queue.get()
            try:
                result = await self._executor(req)
                self._set_terminal(req.calculationId, TaskStatus.SUCCESS, result, None)
                logger.info("[%s] SUCCESS type=%s tool=%s", req.calculationId, req.type, req.toolCode.value)
            except Exception as exc:  # noqa: BLE001 —— 任何异常都收敛为 FAILED，不让 worker 挂掉
                msg = getattr(exc, "message", None) or str(exc)
                code = getattr(exc, "code", "E_INTERNAL")
                self._set_terminal(req.calculationId, TaskStatus.FAILED, None, msg)
                logger.warning("[%s] FAILED %s: %s", req.calculationId, code, msg)
            finally:
                self._queue.task_done()
                self._sweep_expired()

    def _sweep_expired(self) -> None:
        threshold = time.time() - self._ttl_seconds
        cur = self._conn.execute(
            "DELETE FROM tasks WHERE status != ? AND updated_at < ?",
            (TaskStatus.PROCESSING.value, threshold),
        )
        if cur.rowcount:
            self._conn.commit()
            logger.info("清理过期终态任务 %d 条", cur.rowcount)
