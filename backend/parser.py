"""
parser.py — PDF text extraction using pdfplumber (primary) and pymupdf (fallback).
Adapted from Rlresearchassistant for standalone use.
"""

import logging
import re
from typing import Any, Dict, List

import pdfplumber
import fitz  # pymupdf

logger = logging.getLogger(__name__)


def extract_text(pdf_path: str) -> List[Dict[str, Any]]:
    """Extract text and table chunks from a PDF. Returns list of chunk dicts."""
    chunks = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages, 1):
                text = page.extract_text()
                if text:
                    chunks.append({
                        "type": "text",
                        "content": clean_pdf_text(text),
                        "page": page_num,
                    })
                for table in page.extract_tables() or []:
                    if table and len(table) > 1:
                        chunks.append({
                            "type": "table",
                            "content": _table_to_string(table),
                            "page": page_num,
                        })
    except Exception as e:
        logger.warning("pdfplumber error on %s: %s", pdf_path, e)
        chunks = _fallback_pymupdf(pdf_path)
    return chunks


def _fallback_pymupdf(pdf_path: str) -> List[Dict[str, Any]]:
    chunks = []
    try:
        doc = fitz.open(pdf_path)
        for i in range(len(doc)):
            text = doc[i].get_text()
            if text.strip():
                chunks.append({
                    "type": "text",
                    "content": clean_pdf_text(text),
                    "page": i + 1,
                })
        doc.close()
    except Exception as e:
        logger.warning("pymupdf error on %s: %s", pdf_path, e)
    return chunks


def clean_pdf_text(text: str) -> str:
    replacements = {
        "\x00": "",
        "\ufffd": "",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "--",
        "\u00a0": " ",
        "(cid:153)": "",   # CID-encoded glyph — no printable equivalent
        "(cid:176)": "deg ",
        ":176)": "deg ",
        "( fi": "",
        "fi)": "",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)

    text = re.sub(r"\s+", " ", text)
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    result = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", result)


def _table_to_string(table: List[List]) -> str:
    rows = []
    for row in table:
        cells = [str(c).strip() if c else "" for c in row]
        rows.append(" | ".join(cells))
    return "\n".join(rows)
