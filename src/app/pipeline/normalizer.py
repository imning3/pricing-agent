"""S0 格式归一化：内容嗅探（扩展名不可信——本项目资料中出现过 HTML 伪装的 .doc）。

输出统一的 NormalizedDoc；后续 S1 按 kind 分派解析器。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.core.errors import FileFormatError

Kind = Literal["docx", "xlsx", "pdf", "html", "text"]


@dataclass
class NormalizedDoc:
    fileName: str
    kind: Kind
    data: bytes


def sniff_magic(data: bytes) -> str:
    if data[:4] == b"PK\x03\x04":
        return "zip"
    if data[:4] == b"%PDF":
        return "pdf"
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "ole"  # Word97/Excel97 二进制
    head = data[:512].lstrip().lower()
    if head.startswith(b"<html") or head.startswith(b"<!doctype html") or b"<w:worddocument" in head:
        return "html"
    return "text"


def normalize(data: bytes, fileName: str, fileType: str) -> NormalizedDoc:
    magic = sniff_magic(data)
    ext = fileType.lower().lstrip(".")

    if magic == "zip" and ext in ("docx",):
        return NormalizedDoc(fileName, "docx", data)
    if magic == "zip" and ext in ("xlsx", "xlsm"):
        return NormalizedDoc(fileName, "xlsx", data)
    if magic == "pdf" or ext == "pdf":
        if magic != "pdf":
            raise FileFormatError(f"{fileName}：扩展名为 pdf 但内容不是 PDF")
        return NormalizedDoc(fileName, "pdf", data)
    if magic == "html":
        # WPS/Word "网页另存为" 导出的伪 .doc
        return NormalizedDoc(fileName, "html", data)
    if magic == "ole":
        # 决策（2026-09-28）：服务只支持 docx，Word97 .doc 由前端引导用户手动转换
        raise FileFormatError(
            f"{fileName}：仅支持 .docx 格式，请将 .doc 文档另存为 .docx 后重新上传"
        )

    if ext in ("docx", "xlsx"):
        raise FileFormatError(f"{fileName}：扩展名 {ext} 与实际内容不符（非 OOXML 包）")
    return NormalizedDoc(fileName, "text", data)
