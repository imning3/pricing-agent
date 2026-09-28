"""S-前置：获取文件。

- http(s):// —— 生产走 MinIO 预签名 URL 下载（流式、大小上限、超时）；
- 其余视为本地路径 —— 开发联调期代替 MinIO（联调后端不便搭 MinIO 时直接传本地盘路径）。
"""
from __future__ import annotations

from pathlib import Path

import httpx

from app.core.errors import FileDownloadError


async def download(url: str, max_mb: int, timeout_seconds: int = 30) -> bytes:
    limit = max_mb * 1024 * 1024
    if not url.lower().startswith(("http://", "https://")):
        p = Path(url)
        if not p.is_file():
            raise FileDownloadError(f"文件不存在：{url}（本地路径模式）")
        data = p.read_bytes()
        if len(data) > limit:
            raise FileDownloadError(f"文件超过大小上限 {max_mb}MB")
        return data
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise FileDownloadError(f"文件下载失败 HTTP {resp.status_code}（URL 过期或不可达）")
                buf = bytearray()
                async for chunk in resp.aiter_bytes(65536):
                    buf.extend(chunk)
                    if len(buf) > limit:
                        raise FileDownloadError(f"文件超过大小上限 {max_mb}MB")
                return bytes(buf)
    except FileDownloadError:
        raise
    except httpx.HTTPError as exc:
        raise FileDownloadError(f"文件下载异常：{exc}") from exc
