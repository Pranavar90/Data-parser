"""
parser.py — PDF text/image extraction using pdfplumber (primary) and pymupdf (fallback).
Includes VLM image rendering for PDFs with insufficient text content.
"""

import base64
import logging
import re
from typing import Any, Dict, List

import pdfplumber
import fitz  # pymupdf

logger = logging.getLogger(__name__)

# Minimum chars from text extraction before falling back to VLM
VLM_TEXT_THRESHOLD = 500


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


def detect_figure_pages(pdf_path: str) -> List[int]:
    """Return 0-based page indices that contain embedded images (figures)."""
    figure_pages = []
    try:
        doc = fitz.open(pdf_path)
        for i in range(len(doc)):
            if doc[i].get_images():
                figure_pages.append(i)
        doc.close()
    except Exception as e:
        logger.warning("Figure detection failed for %s: %s", pdf_path, e)
    return figure_pages


def extract_as_images(pdf_path: str, dpi: int = 200, max_dim: int = 1536, page_indices: List[int] | None = None) -> List[Dict[str, Any]]:
    """Render PDF pages as base64 JPEG images for VLM consumption.

    Args:
        page_indices: If provided, render only these 0-based page indices.
                      If None, render all pages.
    """
    pages = []
    try:
        doc = fitz.open(pdf_path)
        indices = page_indices if page_indices is not None else range(len(doc))
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        for i in indices:
            if i < 0 or i >= len(doc):
                continue
            pix = doc[i].get_pixmap(matrix=matrix)
            if max(pix.width, pix.height) > max_dim:
                scale = max_dim / max(pix.width, pix.height)
                pix = doc[i].get_pixmap(matrix=fitz.Matrix(zoom * scale, zoom * scale))
            img_bytes = pix.tobytes("jpeg")
            pages.append({
                "page": i + 1,
                "image_b64": base64.b64encode(img_bytes).decode("ascii"),
                "mime": "image/jpeg",
            })
        doc.close()
        logger.info("Rendered %d pages as images from %s", len(pages), pdf_path)
    except Exception as e:
        logger.error("Image extraction failed for %s: %s", pdf_path, e)
    return pages


def _table_to_string(table: List[List]) -> str:
    rows = []
    for row in table:
        cells = [str(c).strip() if c else "" for c in row]
        rows.append(" | ".join(cells))
    return "\n".join(rows)
