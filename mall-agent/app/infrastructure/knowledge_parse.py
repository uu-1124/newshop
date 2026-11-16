"""Bounded, subprocess-only extraction. Never executes macros, links or embedded files."""

import io
import json
import logging
import re
import sys
import zipfile

from defusedxml import ElementTree
from pypdf import PdfReader

MAX_BYTES = 20 * 1024 * 1024
MAX_TEXT = 500000


def normalize(text):
    if "\x00" in text:
        raise ValueError("Invalid text")
    return re.sub(r"\n{4,}", "\n\n\n", text.replace("\r\n", "\n").replace("\r", "\n")).strip()


def extract(raw, kind, max_pages=200):
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError("Invalid size")
    warnings, pages, page_map = [], None, []
    if kind == "pdf":
        if not raw.startswith(b"%PDF-"):
            raise ValueError("Invalid PDF")
        reader = PdfReader(io.BytesIO(raw), strict=True)
        if reader.is_encrypted or not 1 <= len(reader.pages) <= max_pages:
            raise ValueError("Encrypted PDF or page limit")
        pieces, length = [], 0
        for index, page in enumerate(reader.pages, 1):
            text = normalize(page.extract_text() or "")
            if not text.strip():
                warnings.append(f"第{index}页未提取到文本；可能需要OCR")
            start = length + (2 if pieces else 0)
            length = start + len(text)
            if length > MAX_TEXT:
                raise ValueError("Text limit")
            pieces.append(text)
            page_map.append({"page": index, "start": start, "end": length})
        text, pages = "\n\n".join(pieces), len(reader.pages)
        warnings.append("PDF阅读顺序与表格需要人工校对；当前不提供OCR")
    elif kind == "docx":
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > 2000 or sum(item.file_size for item in entries) > 60 * 1024 * 1024:
                raise ValueError("Archive limit")
            if any(item.flag_bits & 1 or item.file_size > 20 * 1024 * 1024 for item in entries):
                raise ValueError("Unsupported archive")
            if "word/document.xml" not in archive.namelist() or "word/vbaProject.bin" in archive.namelist():
                raise ValueError("Unsupported Word file")
            document = ElementTree.fromstring(archive.read("word/document.xml"))
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            text = "\n".join("".join(p.itertext()) for p in document.findall(".//w:p", ns))
            warnings.append("Word表格按段落展开；图片、页眉页脚与修订内容需要人工校对")
    elif kind in {"txt", "md"}:
        text = raw.decode("utf-8-sig")
    else:
        raise ValueError("Unsupported source")
    if kind != "pdf":
        text = normalize(text)
    if not text.strip() or len(text) > MAX_TEXT:
        raise ValueError("Empty or excessive text")
    return {"text": text, "warnings": warnings[:100], "page_count": pages, "page_map": page_map}


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    try:
        result = extract(
            sys.stdin.buffer.read(MAX_BYTES + 1), sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 200
        )
        sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    except Exception:
        sys.exit(1)
