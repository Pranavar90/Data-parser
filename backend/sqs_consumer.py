"""
sqs_consumer.py — SQS message consumer for the PDF parsing pipeline.

Polls an SQS queue for S3 event notifications, fetches PDFs from S3,
runs them through the extraction pipeline, and writes result JSON back to S3.

Runs in standby mode if SQS_QUEUE_URL is not configured.
"""

import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

# Ensure print output is unbuffered for Docker logs
sys.stdout.reconfigure(line_buffering=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("sqs_consumer")

# Make backend modules importable (when run from /app in Docker)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as cfg
from main import _process_file


def _wait_for_config():
    """Block until SQS_QUEUE_URL is configured."""
    queue_url = os.environ.get("SQS_QUEUE_URL", "").strip()
    if queue_url and queue_url != "placeholder":
        return queue_url

    logger.info("SQS_QUEUE_URL not configured - running in standby mode")
    while True:
        queue_url = os.environ.get("SQS_QUEUE_URL", "").strip()
        if queue_url and queue_url != "placeholder":
            logger.info("SQS_QUEUE_URL detected: %s", queue_url)
            return queue_url
        time.sleep(30)


def main():
    print("SQS consumer starting")

    queue_url = _wait_for_config()
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    output_prefix = os.environ.get("S3_OUTPUT_PREFIX", "parsed-json/")

    # Import boto3 here so the module loads even without it installed (standby mode)
    import boto3

    sqs = boto3.client("sqs", region_name=region)
    s3 = boto3.client("s3", region_name=region)

    logger.info("SQS consumer ready - queue: %s", queue_url)
    logger.info("Output prefix: %s", output_prefix)

    processed = 0
    failed = 0

    while True:
        try:
            resp = sqs.receive_message(
                QueueUrl=queue_url,
                MaxNumberOfMessages=5,
                WaitTimeSeconds=20,
                VisibilityTimeout=600,  # 10 min per PDF
            )
        except Exception as e:
            logger.error("SQS receive_message failed: %s", e)
            time.sleep(10)
            continue

        messages = resp.get("Messages", [])
        if not messages:
            continue

        for msg in messages:
            receipt = msg["ReceiptHandle"]

            try:
                body = json.loads(msg["Body"])
                # S3 event notification format
                records = body.get("Records", [])
                if not records:
                    logger.warning("No Records in SQS message, skipping")
                    sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
                    continue

                bucket = records[0]["s3"]["bucket"]["name"]
                key = records[0]["s3"]["object"]["key"]

                # Skip non-PDF files
                if not key.lower().endswith(".pdf"):
                    logger.info("Skipping non-PDF: %s", key)
                    sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
                    continue

                logger.info("Processing: s3://%s/%s", bucket, key)

                # Fetch PDF from S3
                pdf_obj = s3.get_object(Bucket=bucket, Key=key)
                pdf_bytes = pdf_obj["Body"].read()

                # Write to temp file (pdfplumber needs a file path)
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    tmp.write(pdf_bytes)
                    tmp_path = tmp.name

                try:
                    # Run extraction pipeline
                    t0 = time.time()
                    result = _process_file(tmp_path)
                    elapsed = round(time.time() - t0, 1)

                    # Write JSON to S3 output
                    stem = Path(key).stem
                    output_key = f"{output_prefix}{stem}.json"
                    s3.put_object(
                        Bucket=bucket,
                        Key=output_key,
                        Body=json.dumps(result, indent=cfg.JSON_INDENT, ensure_ascii=False),
                        ContentType="application/json",
                        Metadata={
                            "source-key": key,
                            "doc-type": result.get("doc_type", "unknown"),
                            "material-name": result.get("material_name", ""),
                            "properties-count": str(result.get("properties_count", 0)),
                        },
                    )

                    # Success - delete SQS message
                    sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
                    processed += 1
                    logger.info(
                        "Done: %s -> %s (%ss, %d props) [total: %d ok, %d fail]",
                        key, output_key, elapsed,
                        result.get("properties_count", 0),
                        processed, failed,
                    )

                except Exception as e:
                    failed += 1
                    logger.error("Extraction failed for %s: %s [total: %d ok, %d fail]", key, e, processed, failed)
                    # Message returns to queue after VisibilityTimeout expires

                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

            except Exception as e:
                logger.error("Failed to process SQS message: %s", e)
                # Don't delete — let it retry or go to DLQ


if __name__ == "__main__":
    main()
