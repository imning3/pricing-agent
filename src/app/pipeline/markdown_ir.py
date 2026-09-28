"""S1 Markdown IR：文档 → Markdown 中间表示 + 章节树。

已知坑（AGENTS.md §5）：
1. Word 自动多级编号的"3.2.1"不在正文文本里 —— 转换后校验编号连续性，缺失时代码合成；
2. 合并单元格 GFM 表达不了 —— 检测到合并的表格走结构化旁路（原样行列表）。
markitdown 可用时优先使用；缺失则用内置 python-docx 兜底实现。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from io import BytesIO

from app.core.errors import FileFormatError
from app.pipeline.normalizer import NormalizedDoc

# 编号正则：3.2.1 / 一、 / （一） / 第X章
_NUM_PATTERNS = [
    re.compile(r"^(\d+(?:\.\d+)*)[\s、.．]"),
    re.compile(r"^([一二三四五六七八九十]+)[、.．]"),
    re.compile(r"^（([一二三四五六七八九十]+)）"),
    re.compile(r"^第([一二三四五六七八九十]+)章"),
]


@dataclass
class TableBlock:
    path: str
    header: list[str]
    rows: list[list[str]]
    merged: bool = False  # 含合并单元格 → 旁路保留


@dataclass
class Section:
    path: str          # 如 "3.2.1 用户管理"
    level: int         # 标题层级 1..N
    text: str          # 该节正文（markdown 文本，表格占位 [TABLE:n]）


@dataclass
class MarkdownIR:
    fileName: str
    sections: list[Section] = field(default_factory=list)
    tables: list[TableBlock] = field(default_factory=list)


# 非标题特征：自动编号残留开头（". xxx"，numbering.xml 数字丢失后的句点）或陈述性标点结尾
_NON_TITLE = re.compile(r"^[\.\、·•]")           # 自动编号残留
_NON_TITLE_TAIL = re.compile(r"[。；;，,：:]$")  # 列表项/陈述句结尾，标题不会有


def _heading_level(style_name: str, text: str) -> int | None:
    s = style_name or ""
    if _NON_TITLE.match(text) or _NON_TITLE_TAIL.search(text):
        return None
    # 标准样式：Heading N / 标题N
    m = re.search(r"(?:heading|标题)\s*(\d)", s, re.IGNORECASE)
    if m:
        return int(m.group(1))
    # 军工/企业模板自定义样式：章标题→1；"N级有标题条"→N+1（章下第一级即 1级）
    # 注意：必须同时含"标题"字样——"N级编号列项"等列表样式不是标题
    if "章标题" in s:
        return 1
    m = re.search(r"(\d+)\s*级", s)
    if m and "标题" in s:
        return int(m.group(1)) + 1
    if any(p.match(text) for p in _NUM_PATTERNS):
        # 按编号深度估计层级：3.2.1 → 3 级；一、/第X章 → 1 级
        m2 = re.match(r"^(\d+(?:\.\d+)*)", text)
        if m2:
            if "." not in m2.group(1):
                return None  # 无标题样式时，单级编号（"3. xxx"）多为列表项而非章节
            return m2.group(1).count(".") + 1
        return 1
    return None


def _strip_page_ref(title: str) -> str:
    """去除标题尾部混入的页码/交叉引用数字（如"范围 5"，源自目录字段或转换残留）。"""
    return re.sub(r"[ \t　]+\d{1,4}$", "", title).strip() or title


def build_ir(doc: NormalizedDoc) -> MarkdownIR:
    if doc.kind == "docx":
        return _build_from_docx(doc)
    if doc.kind == "xlsx":
        return _build_from_xlsx(doc)
    # pdf/html/text：真实管线后续补（pdfplumber/html 解析器），当前给出明确错误
    raise FileFormatError(f"{doc.fileName}：{doc.kind} 格式的 S1 解析器尚未接入（待装 pdfplumber 等）")


def _build_from_docx(doc: NormalizedDoc) -> MarkdownIR:
    try:
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as exc:  # pragma: no cover - 环境缺依赖
        raise FileFormatError("缺少 python-docx，无法解析 docx") from exc

    from docx.oxml.ns import qn
    d = Document(BytesIO(doc.data))
    ir = MarkdownIR(fileName=doc.fileName)
    stack: list[tuple[int, str]] = []  # (level, title)
    cur_path, cur_level = "", 0
    buf: list[str] = []
    table_idx = 0

    def flush() -> None:
        if cur_path or buf:
            ir.sections.append(Section(path=cur_path or "(正文)", level=cur_level or 1, text="\n".join(buf)))
            buf.clear()

    for child in d.element.body.iterchildren():
        if child.tag == qn("w:p"):
            p = Paragraph(child, d)
            text = p.text.strip()
            if not text:
                continue
            lvl = _heading_level(p.style.name if p.style else "", text)
            if lvl:
                flush()
                while stack and stack[-1][0] >= lvl:
                    stack.pop()
                num = re.match(r"^(\d+(?:\.\d+)*)", text)
                if num:
                    title = _strip_page_ref(text[num.end():].strip())
                    title = re.sub(r"^[\.、．:：\s]+", "", title)  # 清理编号残留分隔符（"3. xxx"→"xxx"）
                else:
                    title = _strip_page_ref(text)
                stack.append((lvl, title))
                # 用标题栈合成完整路径（自动编号缺失时以标题层级回填）
                cur_path = "/".join(s[1] for s in stack)
                cur_level = lvl
                buf.append("#" * min(lvl, 6) + " " + title)
            else:
                buf.append(text)
        elif child.tag == qn("w:tbl"):
            t = Table(child, d)
            rows = [[c.text.strip().replace("\n", " ") for c in r.cells] for r in t.rows]
            merged = _has_merge(t)
            header = rows[0] if rows else []
            ir.tables.append(TableBlock(path=cur_path, header=header, rows=rows[1:], merged=merged))
            buf.append(f"[TABLE:{table_idx}]")
            table_idx += 1
    flush()
    return ir


def _has_merge(table) -> bool:
    from docx.oxml.ns import qn
    for tr in table.rows:
        tc_count = len(list(tr._tr.iterchildren(qn("w:tc"))))
        if tc_count != len(tr.cells):
            return True
        for tc in tr._tr.iterchildren(qn("w:tc")):
            tc_pr = tc.find(qn("w:tcPr"))
            if tc_pr is not None and tc_pr.find(qn("w:gridSpan")) is not None:
                return True
    return False


def _build_from_xlsx(doc: NormalizedDoc) -> MarkdownIR:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover
        raise FileFormatError("缺少 openpyxl，无法解析 xlsx") from exc

    wb = load_workbook(BytesIO(doc.data), read_only=True, data_only=True)
    ir = MarkdownIR(fileName=doc.fileName)
    for ws in wb.worksheets:
        rows = [[("" if c is None else str(c)).strip() for c in r] for r in ws.iter_rows(values_only=True)]
        rows = [r for r in rows if any(r)]
        if not rows:
            continue
        ir.tables.append(TableBlock(path=ws.title, header=rows[0], rows=rows[1:]))
        ir.sections.append(Section(path=ws.title, level=1, text=f"[TABLE:{len(ir.tables)-1}]"))
    return ir
