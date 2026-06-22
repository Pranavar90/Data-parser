# Phased Migration Plan: Local Parser → AWS Production Pipeline

**Project**: Planet Materials Labs PDF Bulk Parser
**Date**: 2026-06-22
**Branch**: Dev/Dhara
**Corpus**: ~100,000 TDS + Research Papers (growing via Google Drive → S3)

---

## VLM+LLM Extraction Strategy

### The Problem

The current parser is **text-only**. It uses pdfplumber to extract the text layer from PDFs and feeds that raw text to an LLM. This fails silently on:
- Scanned PDFs (no text layer at all)
- Image-embedded tables (common in manufacturer TDS documents)
- Multi-column layouts where text extraction scrambles column order
- PDFs with CID-encoded fonts or non-standard encodings

For a 100K document corpus, an estimated 20-30% of documents fall into these failure categories.

### Chosen Strategy: Text-First with VLM Fallback (Strategy A)

Three strategies were evaluated:

| Strategy | Cost (100K docs) | Accuracy | Complexity |
|---|---|---|---|
| **A: Text-first + VLM fallback** | ~$9,600 | High (with re-queue) | Medium |
| B: Pure VLM (all pages as images) | ~$30,000 | Uniformly high | Low |
| C: Parallel text+image fusion | ~$32,000 | Highest | High |

**Strategy A selected** because:
1. 70-80% of TDS documents are clean digital PDFs — paying VLM rates for all of them is wasteful
2. Existing text extraction code (`parser.py`, chunking, merge) is proven and reusable
3. LiteLLM makes routing between text-LLM and VLM models a config change
4. Natural upgrade path to Strategy C per-document if needed
5. Confidence-based re-queue catches quality gate misses automatically

### How Strategy A Works

```
PDF arrives from S3
  │
  ▼
Step 1: Try pdfplumber text extraction (existing parser.py)
  │
  ▼
Step 2: Quality Gate — assess extracted text
  │
  ├─ PASS (chars_per_page > 50, garbled_ratio < 15%, sufficient content)
  │   │
  │   ▼
  │   TEXT PATH (existing pipeline)
  │   → chunk text (4000 chars, 300 overlap)
  │   → send each chunk to LLM via LiteLLM (model="extraction-primary")
  │   → merge chunk results
  │   → if extraction_confidence < 0.3 → RE-QUEUE for VLM
  │
  └─ FAIL (low chars, scanned, garbled, or S3 metadata says scanned)
      │
      ▼
      VLM PATH (new)
      → render pages as images (pymupdf, 200 DPI, JPEG, max 1536px)
      → send page images to VLM via LiteLLM (model="extraction-vlm")
      → merge page-level results
```

### Quality Gate Logic

```
assess_text_quality(chunks, pdf_path) → "text" | "vlm"

Hard rules (instant VLM):
  - total extracted chars < 100            → VLM (basically empty)
  - chars per page < 50                    → VLM (scanned document)
  - S3 metadata x-amz-meta-scanned: true  → VLM (upstream override)

Soft rules (scoring):
  - garbled character ratio > 15%          → VLM (encoding problems)
  - pdfplumber raised exception            → VLM (already using pymupdf fallback)

Default: TEXT (trust the text extraction)
```

### Image Rendering Pipeline

New function in `parser.py`:

```
extract_as_images(pdf_path, dpi=200, max_dim=1536, fmt="jpeg", quality=85)
→ List[{page: int, image_b64: str, width: int, height: int, mime: str}]
```

- **DPI 200**: Optimal for TDS tables. 150 too blurry for small text, 300 wastes tokens.
- **Max dimension 1536px**: Larger images get downscaled. VLMs resize internally anyway.
- **JPEG quality 85**: 3-5x smaller than PNG, visually lossless for text documents.
- **Memory**: ~200KB per page at 200 DPI JPEG. 50-page doc = ~10MB. Fine for EC2.

### System Prompts for VLM

The existing SYSTEM_PROMPT_TDS and SYSTEM_PROMPT_PAPER are text-oriented. VLM variants are needed:

**VLM prompt additions:**
- "You are looking at a page image from a [Technical Data Sheet / Research Paper]."
- "Read all tables carefully, including column headers, row labels, and units."
- "Extract every numerical value visible in the image."
- Output JSON schema remains **identical** to text prompts (critical for merge compatibility)

**VLM prompt removals:**
- CID glyph handling notes (irrelevant for image input)
- Text-specific extraction artifacts

### Merge Strategy for Page-Level VLM Results

The existing `_merge()` function works for page-level results with one enhancement:

When the same property appears on multiple pages with **different values**:
- Keep the higher-confidence version
- If confidence is similar, keep both with page annotation in the context field
- Dedup key remains `name-value` (existing logic)

### Confidence-Based Re-Queue

Safety net for quality gate misses:

```
if extraction_result.extraction_confidence < 0.3 and extraction_path == "text":
    → push SQS message back with attribute force_vlm=true
    → VLM path processes on next consumption
    → prevents silent low-quality extractions
```

---

## Migration Phases

### Phase 0: Pydantic Schema (no AWS dependency)

**Goal**: Define strict output schema before any infrastructure changes.

**Deliverables:**
- `backend/schemas.py` — Pydantic BaseModel classes
- Models: `MaterialProperty`, `ProcessingCondition`, `KeyFinding`, `Limitation`, `TDSExtraction`, `PaperExtraction`, `MaterialExtraction` (discriminated union), `S3ObjectMetadata`, `ExtractionRecord`
- Validation integrated into `_process_file()` output

**Key decisions in this phase:**
- `value` field: `float | str` (to handle ranges like "12.5 - 15.0")
- Property names: open `str` (not enum — too many edge cases in materials science)
- All doc-type-specific fields: `Optional` with defaults (LLM may not extract everything)
- Add `schema_version: str = "1.0"` for forward compatibility

**Files:**
- New: `backend/schemas.py`
- Modified: `backend/main.py` (`_process_file()` returns Pydantic model)
- Modified: `backend/extractor.py` (validate LLM output against schema)

**Verification:** Existing PDF upload still works, output is now validated Pydantic JSON.

---

### Phase 1: LLM Client Rewrite (Ollama → LiteLLM)

**Goal**: Replace Ollama with LiteLLM proxy, keeping text-only extraction for now.

**Deliverables:**
- Rewrite `llm.py` from Ollama HTTP client to OpenAI SDK client
- Update `extractor.py` to use chat completion format (`messages[]` instead of `prompt` + `system`)
- Update `config.py` to replace Ollama config with LiteLLM settings
- LiteLLM proxy config file (`litellm_config.yaml`)

**Architecture:**

```
extractor.py
  → llm.py (new OpenAI SDK client)
    → LiteLLM proxy (localhost:4000)
      → Bedrock Claude 3.5 Sonnet (extraction-primary)
```

**Key changes in `llm.py`:**

| Before (Ollama) | After (LiteLLM/OpenAI SDK) |
|---|---|
| `httpx.Client` | `openai.OpenAI(base_url=litellm_proxy)` |
| `POST /api/generate` | `client.chat.completions.create()` |
| `prompt` + `system` params | `messages=[{role: system, ...}, {role: user, ...}]` |
| `format: "json"` | `response_format={"type": "json_object"}` |
| `_extract_json()` regex fallback | May still be needed; structured output preferred |

**Key changes in `extractor.py`:**
- `extract_from_text()` constructs `messages` array instead of passing `prompt` and `system` separately
- System prompt goes in `messages[0]` with `role: "system"`
- Text chunk goes in `messages[1]` with `role: "user"`
- Cache key adapts to `model:messages_hash`

**Config changes:**
```yaml
# config.yaml — new section replacing ollama.*
litellm:
  proxy_url: "http://localhost:4000"
  model_text: "extraction-primary"
  model_vlm: "extraction-vlm"
  timeout: 300
  max_tokens: 4096
```

**Files:**
- Rewritten: `backend/llm.py`
- Modified: `backend/extractor.py` (chat completion format)
- Modified: `backend/config.py` (LiteLLM config)
- Modified: `config.yaml`
- New: `litellm_config.yaml` (LiteLLM proxy config)

**Verification:** Run locally with LiteLLM proxy pointing at Bedrock or Ollama. Parse a batch of test PDFs. Compare extraction quality against old Ollama 3b output.

---

### Phase 2: VLM Extraction Path

**Goal**: Add image-based extraction alongside existing text path.

**Deliverables:**
- `parser.py` — new `extract_as_images()` and `assess_text_quality()` functions
- `extractor.py` — new `extract_from_images()` function and VLM prompt variants
- `extractor.py` — enhanced `_merge()` for page-level dedup with confidence resolution
- Routing logic in `_process_file()` (or its successor)

**New functions in `parser.py`:**

```python
extract_as_images(pdf_path, dpi=200, max_dim=1536, fmt="jpeg", quality=85)
  → List[{page, image_b64, width, height, mime}]

assess_text_quality(chunks, pdf_path)
  → "text" | "vlm"
```

**New in `extractor.py`:**

```python
SYSTEM_PROMPT_TDS_VLM = "..."   # VLM variant of TDS prompt
SYSTEM_PROMPT_PAPER_VLM = "..." # VLM variant of paper prompt

extract_from_images(images: List[dict], doc_type: str) -> dict
  # Sends page images to VLM via LiteLLM
  # Returns same schema as extract_from_text()
```

**New in `llm.py`:**

```python
generate_with_images(model, messages_with_images, ...)
  # Handles multimodal content blocks (text + image_url)
  # Uses same OpenAI SDK, just different content format
```

**Updated routing in `_process_file()` (or successor):**

```python
def process_document(pdf_path):
    # Step 1: Try text extraction
    chunks = extract_text(pdf_path)
    full_text = join(chunks)

    # Step 2: Quality gate
    path = assess_text_quality(chunks, pdf_path)

    # Step 3: Route
    if path == "text":
        raw = extract_from_text(full_text)
        if raw.get("extraction_confidence", 0) < 0.3:
            # Re-queue for VLM (in SQS consumer, this pushes back to queue)
            raise LowConfidenceError(raw)
    else:
        images = extract_as_images(pdf_path)
        raw = extract_from_images(images)

    # Step 4: Normalize + validate against Pydantic schema
    return build_extraction_record(raw, pdf_path)
```

**Files:**
- Modified: `backend/parser.py` (add image rendering + quality assessment)
- Modified: `backend/extractor.py` (add VLM extraction + prompt variants + merge enhancement)
- Modified: `backend/llm.py` (add multimodal message support)
- Modified: `backend/main.py` or new `backend/processor.py` (routing logic)

**Verification:**
1. Test with clean digital TDS → should take text path, same quality as Phase 1
2. Test with scanned PDF → should route to VLM, produce valid extraction
3. Test with complex-table PDF → quality gate should detect and route to VLM
4. Test merge of page-level VLM results → dedup works correctly

---

### Phase 3: SQS Consumer + S3 Integration

**Goal**: Replace browser upload with event-driven S3/SQS processing.

**Deliverables:**
- `backend/sqs_consumer.py` — long-polling SQS consumer
- S3 PDF fetching via boto3
- S3 metadata retrieval (HEAD object)
- Confidence-based re-queue mechanism
- Remove browser upload endpoints from `main.py`

**SQS Consumer Architecture:**

```python
# sqs_consumer.py — main loop

while True:
    messages = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=5, WaitTimeSeconds=20)

    for msg in messages:
        body = json.loads(msg["Body"])
        s3_bucket = body["Records"][0]["s3"]["bucket"]["name"]
        s3_key = body["Records"][0]["s3"]["object"]["key"]

        # 1. Fetch PDF from S3
        pdf_bytes = s3.get_object(Bucket=s3_bucket, Key=s3_key)["Body"].read()

        # 2. Get S3 metadata
        head = s3.head_object(Bucket=s3_bucket, Key=s3_key)
        s3_meta = S3ObjectMetadata(
            bucket=s3_bucket, key=s3_key,
            content_type=head["ContentType"],
            size_bytes=head["ContentLength"],
            last_modified=head["LastModified"],
            etag=head["ETag"],
            custom=head.get("Metadata", {}),
        )

        # 3. Check for force_vlm attribute (from re-queue)
        force_vlm = msg.get("MessageAttributes", {}).get("force_vlm", {}).get("StringValue") == "true"

        # 4. Write PDF to temp file (needed for pymupdf/pdfplumber)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = tmp.name

        try:
            # 5. Process document
            result = process_document(tmp_path, force_vlm=force_vlm)

            # 6. Enrich with S3 metadata
            record = ExtractionRecord(extraction=result, s3_metadata=s3_meta, ...)

            # 7. Write to database
            write_to_db(record)

            # 8. Delete SQS message (success)
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])

        except LowConfidenceError as e:
            # Re-queue with force_vlm=true
            sqs.send_message(QueueUrl=queue_url, MessageBody=msg["Body"],
                             MessageAttributes={"force_vlm": {"StringValue": "true", "DataType": "String"}})
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])

        except Exception as e:
            logger.error("Failed to process %s: %s", s3_key, e)
            # Message returns to queue after visibility timeout expires

        finally:
            os.unlink(tmp_path)
```

**What gets removed from `main.py`:**
- `POST /api/parse/upload` endpoint
- `_start_job()`, `_run_job()`, `_evict_old_jobs()`
- `_jobs` dict, `_job_queues` dict
- `GET /api/progress/{job_id}` SSE endpoint
- `GET /api/result/{job_id}`
- `GET /api/download/{job_id}`
- `POST /api/save/{job_id}`
- `TEMP_DIR` usage

**What stays in `main.py` (optional health/config API):**
- `GET /api/health` (modified for LiteLLM health check)
- `GET /api/config` / `POST /api/config` (if runtime config changes needed)

**Files:**
- New: `backend/sqs_consumer.py`
- Modified: `backend/main.py` (strip to health/config API only, or remove entirely)
- Modified: `backend/config.py` (add S3_BUCKET, SQS_QUEUE_URL config)

**Blocked by:**
- SQS message schema confirmation
- S3 bucket name and prefix conventions
- S3 custom metadata field definitions

**Verification:**
1. Upload a test PDF to S3 manually
2. Verify SQS receives the event notification
3. Consumer picks up message, fetches PDF, processes it
4. Extraction result + S3 metadata written to database
5. SQS message deleted on success
6. Test failure: kill LLM mid-extraction, verify message returns to queue

---

### Phase 4: Database + Config + Cache

**Goal**: Persistent storage, cloud-native configuration, durable cache.

**Deliverables:**

**4a. Database write:**
- DynamoDB table: `material-extractions`
  - Partition key: `doc_id` (UUID)
  - GSI on `s3_key` (for deduplication and lookup by source file)
  - GSI on `material_name` (for downstream agent queries)
- Write logic in `sqs_consumer.py` using `boto3.resource('dynamodb')`
- Idempotency: check S3 ETag before processing to skip already-processed files

**4b. Configuration migration:**
- `config.py` — add env var override layer: `os.environ.get("LITELLM_PROXY_URL", yaml_value)`
- SSM Parameter Store for parser tuning params (chunk_size, max_chars, etc.)
- Secrets Manager for database credentials (if using RDS)
- `config.yaml` remains as local dev defaults

**4c. Cache migration:**
- Replace `InMemoryCache` in `cache.py` with Redis client (redis-py)
- Redis runs locally on same EC2 instance
- Cache key format unchanged
- TTL: 7 days (configurable) — prevents unbounded growth
- Fallback: if Redis unavailable, skip cache (don't block extraction)

**Files:**
- Modified: `backend/cache.py` (Redis backend)
- Modified: `backend/config.py` (env var + SSM override)
- Modified: `backend/sqs_consumer.py` (DynamoDB write)
- New: `backend/db.py` (DynamoDB client and write logic)

**Verification:**
1. Process a PDF → verify record appears in DynamoDB with correct schema
2. Re-process same PDF → verify idempotency (no duplicate record)
3. Restart EC2 → verify Redis cache rebuilds naturally on next extractions
4. Change SSM parameter → verify consumer picks up new value

---

### Phase 5: EC2 Deployment + Observability

**Goal**: Production-ready EC2 deployment with monitoring.

**Deliverables:**

**5a. EC2 setup:**
- Instance: `t3.xlarge` (4 vCPU, 16GB RAM) — upgrade to `g4dn.xlarge` only if self-hosting Qwen3-VL
- Services managed by systemd:
  - `jsonparser-consumer.service` — SQS consumer (auto-restart on failure)
  - `litellm-proxy.service` — LiteLLM proxy (port 4000)
  - `redis-server.service` — local Redis (port 6379)
- Instance profile (IAM role) with least-privilege permissions

**5b. Observability:**
- Structured JSON logging → CloudWatch Logs agent
- CloudWatch custom metrics:
  - `ExtractionSuccess`, `ExtractionFailure` (count)
  - `ExtractionDuration` (milliseconds)
  - `TextPathUsed`, `VLMPathUsed` (count)
  - `CacheHitRate` (percentage)
  - `SQSMessagesInFlight` (gauge)
- CloudWatch Alarms:
  - DLQ depth > 0 → SNS alert
  - Error rate > 10% over 5 minutes → SNS alert
  - Consumer heartbeat missing > 5 minutes → SNS alert
- CloudWatch Dashboard: single pane of glass for all metrics

**5c. Infrastructure as Code:**
- CDK (Python) or Terraform for:
  - EC2 instance + security group + instance profile
  - S3 bucket event notification → SQS
  - SQS queue + DLQ
  - DynamoDB table + GSIs
  - SSM parameters
  - CloudWatch log group + dashboard + alarms
  - SNS topic for alerts

**Files:**
- New: `infra/` directory (CDK or Terraform)
- New: `systemd/` directory (service unit files)
- New: `scripts/setup-ec2.sh` (bootstrap script)

**Verification:**
1. Deploy to EC2 via IaC
2. Upload 10 test PDFs to S3
3. Monitor CloudWatch dashboard — all 10 processed, metrics visible
4. Kill consumer process → systemd restarts it → messages resume processing
5. Upload a scanned PDF → VLM path triggered → correct extraction
6. Check DLQ is empty

---

### Phase 6: Google Drive → S3 Sync + Backlog Processing

**Goal**: Connect the data source and process the initial 100K document backlog.

**Deliverables:**

**6a. Drive → S3 sync:**
- Mechanism TBD (rclone cron, Drive API + Lambda, or third-party)
- New PDFs in Drive automatically sync to S3 → trigger pipeline
- Initial full sync of existing Drive contents

**6b. Backlog processing:**
- Upload all 100K existing documents to S3 (batch upload)
- SQS consumer processes the backlog at sustained throughput
- Monitor via CloudWatch dashboard
- Estimated time: depends on LLM throughput
  - At 10 docs/min (text path): ~116 hours for 70K text-path docs
  - At 3 docs/min (VLM path): ~166 hours for 30K VLM-path docs
  - Total: ~2 weeks with single consumer, ~3-4 days with increased concurrency

**6c. Steady-state monitoring:**
- Drive sync runs continuously
- New documents processed within minutes of appearing in Drive
- Alerting on sync failures, processing failures, queue depth

**Blocked by:**
- Google Workspace admin access for API credentials
- Drive folder structure / naming conventions
- Sync mechanism decision

---

## Timeline Estimate

| Phase | Dependencies | Estimate |
|---|---|---|
| Phase 0: Pydantic Schema | None | 1-2 days |
| Phase 1: LLM Client Rewrite | LiteLLM installed locally | 2-3 days |
| Phase 2: VLM Extraction Path | Phase 1 complete | 3-4 days |
| Phase 3: SQS Consumer | SQS message schema, Phase 2 | 2-3 days |
| Phase 4: DB + Config + Cache | Phase 3, DB decision | 2-3 days |
| Phase 5: EC2 Deployment | All prior phases, IaC tooling decision | 3-4 days |
| Phase 6: Drive Sync + Backlog | Phase 5, Drive access | 2-3 days + processing time |

**Total implementation**: ~3-4 weeks
**Backlog processing**: additional 1-2 weeks after deployment

---

## File Change Summary

| File | Phase | Change Type |
|---|---|---|
| `backend/schemas.py` | 0 | New |
| `backend/llm.py` | 1 | Complete rewrite |
| `backend/extractor.py` | 1, 2 | Major modification |
| `backend/config.py` | 1, 4 | Modification |
| `config.yaml` | 1 | Modification |
| `litellm_config.yaml` | 1 | New |
| `backend/parser.py` | 2 | Add VLM functions |
| `backend/processor.py` | 2 | New (routing logic) |
| `backend/sqs_consumer.py` | 3 | New |
| `backend/main.py` | 3 | Strip to health API |
| `backend/db.py` | 4 | New |
| `backend/cache.py` | 4 | Rewrite (Redis) |
| `infra/` | 5 | New (IaC) |
| `systemd/` | 5 | New (service units) |
| `scripts/setup-ec2.sh` | 5 | New |
| `requirements.txt` | 1 | Add openai, boto3, redis, pydantic |
