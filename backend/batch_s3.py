"""
batch_s3.py — One-time resumable batch: parse legacy PDFs from S3, write JSON back.

Usage:  python batch_s3.py
Config: S3_BUCKET, INPUT_PREFIX, OUTPUT_PREFIX env vars (all have defaults).
"""

import json
import logging
import os
import sys
import tempfile
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


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    pdfs = list_pdfs()
    total = len(pdfs)
    log.info("Found %d PDFs under %s", total, INPUT_PREFIX)

    done = skipped = failed = 0

    for i, key in enumerate(pdfs, 1):
        out_key = output_key_for(key)

        if already_exists(out_key):
            skipped += 1
            log.info("SKIP: %s", key)
            continue

        real_name = key.rsplit("/", 1)[-1]
        # ponytail: index prefix keeps temp files collision-safe across folders
        local_path = str(TMP_DIR / f"{i}_{real_name}")
        try:
            s3.download_file(BUCKET, key, local_path)
            result = _process_file(local_path, source_s3_key=key, filename_override=real_name)
            body = json.dumps(result, indent=2, ensure_ascii=False)
            s3.put_object(Bucket=BUCKET, Key=out_key, Body=body, ContentType="application/json")
            done += 1
            log.info("[%d/%d] DONE: %s -> %s", i, total, key, out_key)
        except Exception as e:
            failed += 1
            log.error("FAILED: %s | %s", key, e)
            with open("failed_keys.txt", "a") as f:
                f.write(key + "\n")
        finally:
            try:
                os.remove(local_path)
            except OSError:
                pass

    log.info("=== SUMMARY: total=%d done=%d skipped=%d failed=%d ===", total, done, skipped, failed)


if __name__ == "__main__":
    main()
