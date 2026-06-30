"""
batch_s3.py — One-time resumable batch: parse legacy PDFs from S3, write JSON back.

Usage:  python batch_s3.py
Config: S3_BUCKET, INPUT_PREFIX, OUTPUT_PREFIX, CONCURRENCY env vars (all have defaults).
"""

import json
import logging
import os
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

# backend/*.py sit in the same directory; ensure it's on sys.path so
# `import config` and the transitive imports inside main.py resolve.
sys.path.insert(0, os.path.dirname(__file__))

from main import _process_file  # noqa: E402

# ── Config ───────────────────────────────────────────────────────────────────

BUCKET = os.environ.get("S3_BUCKET", "material-data-bucket")
INPUT_PREFIX = os.environ.get("INPUT_PREFIX", "legacy-tds/")
OUTPUT_PREFIX = os.environ.get("OUTPUT_PREFIX", "parsed-json/")
CONCURRENCY = int(os.environ.get("CONCURRENCY", "5"))

TMP_DIR = Path(tempfile.gettempdir()) / "batch"
TMP_DIR.mkdir(parents=True, exist_ok=True)

# ── Logging (stdout + batch.log) ─────────────────────────────────────────────

log = logging.getLogger("batch_s3")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
_fh = logging.FileHandler("batch.log")
_fh.setFormatter(_fmt)
log.addHandler(_sh)
log.addHandler(_fh)

# ── Helpers ──────────────────────────────────────────────────────────────────

s3 = boto3.client("s3")

# Thread-safe counters
_lock = threading.Lock()
_counters = {"done": 0, "skipped": 0, "failed": 0}


def list_pdfs() -> list[str]:
    """List all .pdf keys under INPUT_PREFIX using a paginator."""
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix=INPUT_PREFIX):
        for obj in page.get("Contents", []):
            if obj["Key"].lower().endswith(".pdf"):
                keys.append(obj["Key"])
    return keys


def output_key_for(input_key: str) -> str:
    return OUTPUT_PREFIX + Path(input_key).with_suffix(".json").as_posix()


def already_exists(key: str) -> bool:
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
        return True
    except ClientError:
        return False


def _is_rate_limit_error(exc: Exception) -> bool:
    """Check if an exception is a rate-limit / 429 error."""
    msg = str(exc).lower()
    if "429" in msg or "rate" in msg or "too many" in msg or "throttl" in msg:
        return True
    # litellm wraps 429s
    if hasattr(exc, "status_code") and getattr(exc, "status_code", None) == 429:
        return True
    return False


# ── Worker ───────────────────────────────────────────────────────────────────

_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 2  # seconds


def _process_one(key: str, index: int, total: int) -> None:
    """Process a single PDF key. Skip-check + retry + thread-safe accounting."""
    out_key = output_key_for(key)

    if already_exists(out_key):
        with _lock:
            _counters["skipped"] += 1
        log.info("SKIP: %s", key)
        return

    real_name = key.rsplit("/", 1)[-1]
    # Unique temp filename: real name + uuid suffix to avoid collisions across workers
    suffix = uuid.uuid4().hex[:8]
    local_path = str(TMP_DIR / f"{suffix}_{real_name}")

    last_exc = None
    for attempt in range(_RETRY_ATTEMPTS + 1):
        try:
            s3.download_file(BUCKET, key, local_path)
            result = _process_file(local_path, source_s3_key=key, filename_override=real_name)
            body = json.dumps(result, indent=2, ensure_ascii=False)
            s3.put_object(Bucket=BUCKET, Key=out_key, Body=body, ContentType="application/json")
            with _lock:
                _counters["done"] += 1
            log.info("[%d/%d] DONE: %s -> %s", index, total, key, out_key)
            return
        except Exception as e:
            last_exc = e
            if attempt < _RETRY_ATTEMPTS and _is_rate_limit_error(e):
                delay = _RETRY_BASE_DELAY * (2 ** attempt)
                log.warning("Rate-limited on %s, retry %d/%d in %ds", key, attempt + 1, _RETRY_ATTEMPTS, delay)
                time.sleep(delay)
            elif attempt < _RETRY_ATTEMPTS and _is_rate_limit_error(e) is False:
                # Non-rate-limit error — don't retry
                break
        finally:
            if attempt == _RETRY_ATTEMPTS or not _is_rate_limit_error(last_exc or Exception()):
                try:
                    os.remove(local_path)
                except OSError:
                    pass

    # All retries exhausted or non-retryable error
    with _lock:
        _counters["failed"] += 1
    log.error("FAILED: %s | %s", key, last_exc)
    with _lock:
        with open("failed_keys.txt", "a") as f:
            f.write(key + "\n")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    pdfs = list_pdfs()
    total = len(pdfs)
    log.info("Starting batch: CONCURRENCY=%d, %d files to process", CONCURRENCY, total)

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = {
            pool.submit(_process_one, key, i, total): key
            for i, key in enumerate(pdfs, 1)
        }
        for future in as_completed(futures):
            # Exceptions are already handled inside _process_one,
            # but catch anything truly unexpected
            try:
                future.result()
            except Exception as e:
                log.error("Unexpected worker error for %s: %s", futures[future], e)

    log.info(
        "=== SUMMARY: total=%d done=%d skipped=%d failed=%d ===",
        total, _counters["done"], _counters["skipped"], _counters["failed"],
    )


if __name__ == "__main__":
    main()
