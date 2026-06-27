"""
main.py — FastAPI backend for the Planet Materials Labs PDF Bulk Parser.
"""

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"


def compute_md5(file_path: str) -> Optional[str]:
    """MD5 of a file's raw bytes, read in chunks. Returns None on failure."""
    try:
        h = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        logger.warning("Could not compute MD5 for %s", file_path, exc_info=True)
        return None

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

import config as cfg
from extractor import extract_from_text, extract_from_images
from llm import get_client, shutdown_client, invalidate_health_cache
from parser import extract_text, extract_as_images, VLM_TEXT_THRESHOLD

# ── App setup ─────────────────────────────────────────────────────────────────

# In Docker, backend/*.py files are copied directly into /app/, so
# __file__.parent.parent would resolve to / instead of /app.
# Detect by checking if the expected subdirs exist at parent.parent.
_candidate = Path(__file__).parent.parent
ROOT_DIR = _candidate if (_candidate / "static").is_dir() else Path(__file__).parent

# Thread pool for sync PDF parsing (avoids blocking the event loop)
_executor = ThreadPoolExecutor(max_workers=2)

# In-memory job store with bounded size (local tool; no persistence needed)
_jobs: Dict[str, Dict] = {}
_job_queues: Dict[str, asyncio.Queue] = {}
_MAX_JOBS = 20  # evict oldest completed jobs beyond this count

TEMP_DIR = ROOT_DIR / "temp"
TEMP_DIR.mkdir(exist_ok=True)


# ── Lifecycle ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    shutdown_client()
    _executor.shutdown(wait=False)


app = FastAPI(
    title="Planet Materials Labs — PDF Bulk Parser",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=str(ROOT_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(ROOT_DIR / "templates"))


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/health")
async def health():
    # Run blocking calls in the thread pool to avoid stalling the event loop.
    loop = asyncio.get_running_loop()
    client = get_client()
    ok = await loop.run_in_executor(None, client.is_running)
    models = await loop.run_in_executor(None, client.list_models) if ok else []
    return {
        "llm": ok,
        "models": models,
        "active_model": cfg.LLM_MODEL,
    }


@app.get("/api/config")
async def get_config():
    return cfg.get_all()


@app.post("/api/config")
async def update_config(body: dict):
    cfg.save_config(body)
    # Flush cached health/model results so the UI reflects the new URL immediately.
    invalidate_health_cache()
    return {"status": "saved"}


@app.post("/api/parse/upload")
async def parse_from_upload(files: List[UploadFile] = File(...), rel_paths: str = "[]"):
    """Start a parse job from browser-uploaded files."""
    rel_path_list: List[str] = json.loads(rel_paths)

    job_id = str(uuid.uuid4())
    tmp = TEMP_DIR / job_id
    tmp.mkdir(parents=True)

    entries: List[Dict[str, str]] = []
    for i, upload in enumerate(files):
        rel = rel_path_list[i] if i < len(rel_path_list) else upload.filename or f"file_{i}.pdf"
        rel = rel.replace("\\", "/")
        dest = tmp / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(await upload.read())
        entries.append({"abs_path": str(dest), "rel_path": rel})

    return await _start_job(entries, source="upload", job_id=job_id, tmp_dir=str(tmp))


@app.get("/api/progress/{job_id}")
async def progress_stream(job_id: str, request: Request):
    """SSE stream — emits real-time progress events for a running job."""
    async def _generate():
        if job_id not in _jobs:
            yield {"data": json.dumps({"type": "error", "message": "Job not found."})}
            return

        queue = _job_queues.get(job_id)
        if queue is None:
            # Job already finished before client connected
            job = _jobs[job_id]
            yield {"data": json.dumps({"type": "done", **job.get("summary", {})})}
            return

        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=25.0)
                yield {"data": json.dumps(event)}
                if event.get("type") in ("done", "error"):
                    break
            except asyncio.TimeoutError:
                yield {"data": json.dumps({"type": "ping"})}

    return EventSourceResponse(_generate())


@app.get("/api/result/{job_id}")
async def get_results(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    return {
        "status": job["status"],
        "results": job.get("results", []),
        "errors": job.get("errors", []),
        "summary": job.get("summary", {}),
    }


@app.get("/api/download/{job_id}")
async def download_zip(job_id: str):
    """Download all parsed JSONs as a ZIP file preserving folder structure."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")

    results = job.get("results", [])
    if not results:
        raise HTTPException(400, "No results available for download.")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in results:
            json_rel = _swap_ext(r["rel_path"], ".json")
            zf.writestr(json_rel, json.dumps(r["data"], indent=cfg.JSON_INDENT, ensure_ascii=False))

    buf.seek(0)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="parsed_{timestamp}.zip"'},
    )


@app.post("/api/save/{job_id}")
async def save_to_folder(job_id: str, body: dict):
    """Write parsed JSONs directly to a local folder, preserving structure.
    NOTE: output_path is accepted as-is (local tool, 127.0.0.1 binding only).
    Do not expose this endpoint over a network without adding path restrictions.
    """
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")

    output_path = body.get("output_path", "").strip()
    if not output_path:
        raise HTTPException(400, "output_path is required.")

    out_dir = Path(output_path)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise HTTPException(400, f"Cannot create output directory: {e}")

    saved: List[str] = []
    for r in job.get("results", []):
        json_rel = _swap_ext(r["rel_path"], ".json")
        dest = out_dir / json_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            json.dumps(r["data"], indent=cfg.JSON_INDENT, ensure_ascii=False),
            encoding="utf-8",
        )
        saved.append(str(dest))

    return {"saved": len(saved), "output_path": str(out_dir.resolve())}


# ── Dashboard (read-only batch monitor) ──────────────────────────────────────

_DASHBOARD_BUCKET = os.environ.get("S3_BUCKET", "material-data-bucket")
_DASHBOARD_INPUT_PREFIX = os.environ.get("INPUT_PREFIX", "legacy-tds/")
_DASHBOARD_OUTPUT_PREFIX = os.environ.get("OUTPUT_PREFIX", "parsed-json/")
_BATCH_LOG = Path(os.environ.get("BATCH_LOG", "/opt/jsonparser/backend/batch.log"))
_FAILED_KEYS_FILE = Path(os.environ.get("FAILED_KEYS_FILE", "/opt/jsonparser/backend/failed_keys.txt"))

# ponytail: simple time-based cache, no cache lib needed
_dashboard_cache: Dict[str, Any] = {"data": None, "ts": 0.0}
_CACHE_TTL = 10  # seconds


def _s3_count_keys(prefix: str, suffix: str = "") -> Tuple[int, Dict[str, int]]:
    """Count objects under prefix (with optional suffix filter), also group by first subfolder."""
    import boto3
    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    total = 0
    by_folder: Dict[str, int] = defaultdict(int)
    for page in paginator.paginate(Bucket=_DASHBOARD_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if suffix and not key.lower().endswith(suffix):
                continue
            total += 1
            # Extract first subfolder after the prefix
            rel = key[len(prefix):]
            parts = rel.split("/")
            folder = parts[0] if len(parts) > 1 else "(root)"
            by_folder[folder] += 1
    return total, dict(by_folder)


def _read_tail(path: Path, n: int = 15) -> List[str]:
    """Last n lines of a file, most recent first. Empty list if file missing."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return list(reversed(lines[-n:]))
    except (FileNotFoundError, OSError):
        return []


def _count_lines(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except (FileNotFoundError, OSError):
        return 0


def _batch_is_running() -> bool:
    """Check if batch_s3.py is running via subprocess (no psutil dependency)."""
    try:
        # Works on Linux (EC2)
        result = subprocess.run(
            ["pgrep", "-f", "batch_s3.py"],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _get_dashboard_data() -> Dict[str, Any]:
    """Sync — runs inside thread pool via run_in_executor."""
    now = time.time()
    if _dashboard_cache["data"] and (now - _dashboard_cache["ts"]) < _CACHE_TTL:
        return _dashboard_cache["data"]

    src_total, src_folders = _s3_count_keys(_DASHBOARD_INPUT_PREFIX, ".pdf")
    out_total, out_folders = _s3_count_keys(_DASHBOARD_OUTPUT_PREFIX, ".json")

    folders = []
    for name in sorted(set(src_folders) | set(out_folders)):
        folders.append({
            "name": name,
            "total": src_folders.get(name, 0),
            "parsed": out_folders.get(name, 0),
        })

    failed_count = _count_lines(_FAILED_KEYS_FILE)
    failed_keys = _read_tail(_FAILED_KEYS_FILE, 5)

    data = {
        "source_total": src_total,
        "parsed_total": out_total,
        "pct": round(out_total / src_total * 100, 1) if src_total else 0,
        "folders": folders,
        "log_lines": _read_tail(_BATCH_LOG, 15),
        "failed_count": failed_count,
        "failed_keys": failed_keys,
        "running": _batch_is_running(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _dashboard_cache["data"] = data
    _dashboard_cache["ts"] = now
    return data


@app.get("/dashboard")
async def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html")


@app.get("/api/dashboard/status")
async def dashboard_status():
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(_executor, _get_dashboard_data)
    return data


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _start_job(
    files: List[Dict[str, str]],
    source: str,
    job_id: Optional[str] = None,
    tmp_dir: Optional[str] = None,
) -> dict:
    job_id = job_id or str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()

    _jobs[job_id] = {
        "id": job_id,
        "status": "running",
        "source": source,
        "files": files,
        "results": [],
        "errors": [],
        "tmp_dir": tmp_dir,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "summary": {},
    }
    _job_queues[job_id] = queue
    _evict_old_jobs()
    asyncio.create_task(_run_job(job_id, files, queue))
    return {"job_id": job_id, "total": len(files)}


def _evict_old_jobs() -> None:
    """Remove oldest completed jobs when the store exceeds _MAX_JOBS."""
    if len(_jobs) <= _MAX_JOBS:
        return
    completed = [
        jid for jid, j in _jobs.items()
        if j["status"] == "done" and jid not in _job_queues
    ]
    for jid in completed[: len(_jobs) - _MAX_JOBS]:
        _jobs.pop(jid, None)


async def _run_job(job_id: str, files: List[Dict[str, str]], queue: asyncio.Queue):
    job = _jobs[job_id]
    total = len(files)
    success = failed = 0

    try:
        await queue.put({"type": "start", "total": total, "files": [f["rel_path"] for f in files]})

        # ARCH-002: use get_running_loop() — correct inside a running coroutine.
        loop = asyncio.get_running_loop()

        for i, f in enumerate(files):
            await queue.put({
                "type": "progress",
                "index": i,
                "total": total,
                "file": f["rel_path"],
                "status": "processing",
            })

            try:
                t0 = time.time()
                data = await loop.run_in_executor(_executor, _process_file, f["abs_path"])
                elapsed = round(time.time() - t0, 1)

                job["results"].append({"rel_path": f["rel_path"], "data": data, "elapsed": elapsed})
                success += 1

                await queue.put({
                    "type": "complete",
                    "index": i,
                    "total": total,
                    "file": f["rel_path"],
                    "status": "success",
                    "confidence": data.get("extraction_confidence", 0.0),
                    "doc_type": data.get("doc_type", "unknown"),
                    "props": data.get("properties_count", 0),
                    "elapsed": elapsed,
                })
            except Exception as e:
                failed += 1
                job["errors"].append({"file": f["rel_path"], "error": str(e)})
                await queue.put({
                    "type": "complete",
                    "index": i,
                    "total": total,
                    "file": f["rel_path"],
                    "status": "failed",
                    "error": str(e),
                })

        summary = {"total": total, "success": success, "failed": failed, "job_id": job_id}
        job["status"] = "done"
        job["summary"] = summary
        await queue.put({"type": "done", **summary})
    finally:
        # Always clean up temp files, even if the job errors out.
        if job.get("tmp_dir"):
            shutil.rmtree(job["tmp_dir"], ignore_errors=True)
        _job_queues.pop(job_id, None)


def _process_file(abs_path: str, source_s3_key: Optional[str] = None, filename_override: Optional[str] = None) -> Dict[str, Any]:
    """Sync — extracts and parses a single PDF. Runs inside thread pool.
    Output schema mirrors Rlresearchassistant JSON exports exactly.
    """
    chunks = extract_text(abs_path)
    full_text = "\n".join(c["content"] for c in chunks if c.get("content"))

    if len(full_text) < VLM_TEXT_THRESHOLD:
        # VLM path — text extraction insufficient, use page images
        logger.info("VLM path for %s (%d chars < %d threshold)", abs_path, len(full_text), VLM_TEXT_THRESHOLD)
        images = extract_as_images(abs_path)
        raw = extract_from_images(images)
    else:
        # Text path — standard extraction
        raw = extract_from_text(full_text)

    doc_id   = str(uuid.uuid4())
    filename = filename_override or os.path.basename(abs_path)
    doc_type = raw.get("document_type", "unknown")

    # Material name — 3-tier fallback (same as Rlresearchassistant)
    material_name = raw.get("material_name", "").strip()
    if not material_name:
        material_name = _extract_material_name_regex(full_text[:2000])
    if not material_name:
        material_name = Path(abs_path).stem

    # Normalise to a single unified properties array regardless of doc type.
    # TDS uses raw["properties"][].name; papers use raw["material_properties_mentioned"][].property
    raw_props = (
        raw.get("properties", [])
        if doc_type == "tds"
        else raw.get("material_properties_mentioned", [])
    )
    properties = [
        {
            "property_name": p.get("name") or p.get("property", ""),
            "value":         p.get("value"),
            "unit":          p.get("unit", ""),
            "confidence":    p.get("confidence", 0.0),
            "context":       p.get("context", ""),
        }
        for p in raw_props
        if p.get("name") or p.get("property")
    ]

    output: Dict[str, Any] = {
        "doc_id":               doc_id,
        "filename":             filename,
        "schema_version":       SCHEMA_VERSION,
        "source_md5":           compute_md5(abs_path),
        "source_s3_key":        source_s3_key,
        "doc_type":             doc_type,
        "material_name":        material_name,
        "extraction_confidence": raw.get("extraction_confidence", 0.0),
        "properties_count":     len(properties),
        "properties":           properties,
        # TDS-specific
        "product_description":  raw.get("product_description", "") if doc_type == "tds" else "",
        "applications":         raw.get("applications", []),
        "certifications":       raw.get("certifications", []) if doc_type == "tds" else [],
        "processing_conditions": raw.get("processing_conditions", []) if doc_type == "tds" else [],
        # Paper-specific
        "materials_studied":    raw.get("materials_studied", []) if doc_type == "paper" else [],
        "research_objective":   raw.get("research_objective", "") if doc_type == "paper" else "",
        "methodology":          raw.get("methodology", "") if doc_type == "paper" else "",
        "key_findings":         raw.get("key_findings", []) if doc_type == "paper" else [],
        "limitations":          raw.get("limitations", []) if doc_type == "paper" else [],
        "conclusions":          raw.get("conclusions", "") if doc_type == "paper" else "",
        "source_text":          full_text[:500],
    }

    # Preserve error message on failed extractions
    if raw.get("error"):
        output["error"] = raw["error"]

    return output


def _extract_material_name_regex(text: str) -> str:
    """Regex fallback for material name — mirrors Rlresearchassistant's approach."""
    patterns = [
        r"(?:product\s*name|trade\s*name|material\s*name|grade)[:\s]+([A-Za-z0-9][A-Za-z0-9\s\-/®™+]{2,50}?)(?:\n|,|\.|;)",
        r"(?:^|\n)([A-Z][A-Z0-9\-]{3,30}(?:\s[A-Z0-9]{1,10})?)\s*(?:TDS|Data\s*Sheet|Technical\s*Data)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
        if m:
            name = m.group(1).strip()
            if 3 <= len(name) <= 60:
                return name
    return ""


def _swap_ext(path: str, new_ext: str) -> str:
    """Replace the file extension in a relative path string."""
    p = Path(path)
    return str(p.parent / (p.stem + new_ext))
