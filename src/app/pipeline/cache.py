"""本地磁盘缓存：解析 IR 与段级抽取结果。

设计目标（方案 A）：
- 同文件重复分析/失败重试时，避免重复下载解析与重复 LLM 抽取；
- key 绑定内容 hash + tool/mode + skill 版本，skill 变更自动失效；
- 失败不拖垂主链路：读写异常一律降级为未命中。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def skill_version(skills_dir: str, names: tuple[str, ...] | list[str]) -> str:
    """skill 文件内容指纹；任一 skill 变更即换 key。"""
    h = hashlib.sha256()
    base = Path(skills_dir)
    for name in names:
        p = base / name / "SKILL.md"
        if p.exists():
            h.update(name.encode("utf-8"))
            h.update(b"\0")
            h.update(p.read_bytes())
            h.update(b"\0")
        else:
            h.update(name.encode("utf-8"))
            h.update(b"missing\0")
    return h.hexdigest()[:16]


class DiskCache:
    def __init__(self, root: str, enabled: bool = True, ttl_hours: int = 168):
        self.enabled = enabled
        self.ttl_seconds = max(0, int(ttl_hours)) * 3600
        self.root = Path(root)
        if self.enabled:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning("缓存目录不可用，已关闭缓存：%s", exc)
                self.enabled = False

    def _path(self, namespace: str, key: str) -> Path:
        digest = hashlib.sha256(f"{namespace}:{key}".encode("utf-8")).hexdigest()
        return self.root / namespace / digest[:2] / f"{digest}.json"

    def get(self, namespace: str, key: str) -> Any | None:
        if not self.enabled:
            return None
        path = self._path(namespace, key)
        try:
            if not path.exists():
                return None
            if self.ttl_seconds and path.stat().st_mtime + self.ttl_seconds < time.time():
                path.unlink(missing_ok=True)
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload.get("value")
        except Exception as exc:  # noqa: BLE001
            logger.debug("缓存读取失败 %s/%s: %s", namespace, key[:12], exc)
            return None

    def set(self, namespace: str, key: str, value: Any) -> None:
        if not self.enabled:
            return
        path = self._path(namespace, key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"value": value, "ts": time.time()}, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("缓存写入失败 %s/%s: %s", namespace, key[:12], exc)
