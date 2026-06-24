# Phased Migration Plan: Local Parser → AWS Production Pipeline

**Project**: Planet Materials Labs PDF Bulk Parser
**Date**: 2026-06-22
**Branch**: Dev/Dhara
**Corpus**: ~100,000 TDS + Research Papers (growing via Google Drive → S3)

---

## VLM+LLM Extraction Strategy

### The Problem

The current parser is **text-only**. It uses pdfplumber to extract the text layer from PDFs and feeds that raw text to an LLM. This works well for TDS documents (clean, digital, well-structured manufacturer PDFs) but fails on research papers which commonly have:
- Scanned pages (no text layer at all)
- Multi-column layouts where text extraction scrambles column order
- Complex figures, charts, and image-embedded tables
- Non-standard encodings from academic publishers

### Chosen Strategy: Doc-Type Routed — TDS via LLM, Papers via VLM

The routing decision is based on **document type**, not text quality heuristics:

| Document Type | Extraction Path | Reason |
|---|---|---|
| **TDS** | Text path (pdfplumber + LLM) | Clean digital PDFs from manufacturers. Text extraction works reliably. Cheaper. |
| **Paper** | VLM path (page images + vision model) | Complex layouts, scanned pages, multi-column. VLM handles these natively. |

**Why not VLM for everything?**
- At ~50K TDS documents, VLM would cost ~$12,500 extra for no quality gain
- pdfplumber + a good LLM (Bedrock Claude) already extracts TDS properties accurately
- TDS documents are standardized by manufacturers — predictable structure

**Why not text-only for everything?**
- Research papers have diverse layouts that break text extraction
- Multi-column text gets concatenated incorrectly
- Figures and embedded tables are invisible to text parsers
- Academic PDFs from different publishers have wildly different encoding quality

**Cost estimate at 100K documents (assuming ~50/50 TDS/Paper split):**
- TDS text path: 50,000 docs x ~$0.03/doc = ~$1,500
- Paper VLM path: 50,000 docs x ~$0.25/doc = ~$12,500
- **Total: ~$14,000** for initial backlog processing

### How It Works

```
PDF arrives from S3
  │
  ▼
Step 1: Detect document type
  │     Uses existing detect_document_type() — keyword-based classifier
  │     Or: S3 metadata prefix/tag (s3://bucket/tds/... vs s3://bucket/papers/...)
  │
  ├─ TDS
  │   │
  │   ▼
  │   TEXT PATH (existing pipeline, upgraded model)
  │   → pdfplumber text extraction (parser.py — as-is)
  │   → chunk text (4000 chars, 300 overlap)
  │   → send each chunk to LLM via LiteLLM (model="extraction-primary")
  │   → merge chunk results
  │   → validate against Pydantic TDS schema
  │
  └─ Paper
      │
      ▼
      VLM PATH (new)
      → render pages as images (pymupdf, 200 DPI, JPEG, max 1536px)
      → send page images to VLM via LiteLLM (model="extraction-vlm")
      → merge page-level results
      → validate against Pydantic Paper schema
```

### Document Type Detection

Two methods, in priority order:

1. **S3 key prefix** (most reliable): If the upstream Drive → S3 sync organizes files by type:
   - `s3://bucket/tds/*.pdf` → TDS
   - `s3://bucket/papers/*.pdf` → Paper
   - Zero ambiguity, no classification needed

2. **Existing `detect_document_type()`** (fallback): The keyword classifier in `extractor.py` (90+ TDS keywords, 60+ paper keywords, TDS_BIAS=2). Works when S3 prefix is not available or mixed.

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

### System Prompts

**TDS prompt (SYSTEM_PROMPT_TDS)** — unchanged. Already works well for text-based extraction. Will benefit from the model upgrade (3b → Bedrock Claude) without prompt changes.

**Paper VLM prompt (SYSTEM_PROMPT_PAPER_VLM)** — new variant of SYSTEM_PROMPT_PAPER adapted for image input:

**VLM prompt additions:**
- "You are looking at a page image from a research paper."
- "Read all tables carefully, including column headers, row labels, and units."
- "Pay attention to multi-column layouts — extract from all columns."
- "Extract data from figures and charts where numerical values are visible."
- Output JSON schema remains **identical** to text SYSTEM_PROMPT_PAPER (critical for merge compatibility)

**VLM prompt removals (vs text version):**
- CID glyph handling notes (irrelevant for image input)
- Text-specific extraction artifacts

**Note:** SYSTEM_PROMPT_TDS_VLM is NOT needed — TDS documents always use the text path.

### Merge Strategy for Page-Level VLM Results

The existing `_merge()` function works for page-level results with one enhancement:

When the same property appears on multiple pages with **different values**:
- Keep the higher-confidence version
- If confidence is similar, keep both with page annotation in the context field
- Dedup key remains `name-value` (existing logic)

### Confidence-Based Re-Queue (TDS only)

Safety net for the rare TDS document where text extraction fails unexpectedly:

```
if extraction_result.extraction_confidence < 0.3 and doc_type == "tds":
    → push SQS message back with attribute force_vlm=true
    → VLM path processes on next consumption
    → prevents silent low-quality extractions
```

This should be rare (<5% of TDS documents). Papers always go through VLM, so no re-queue needed for them.

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

### Phase 2: VLM Extraction Path (Papers Only)

**Goal**: Add image-based extraction for research papers. TDS documents continue using the text path from Phase 1.

**Deliverables:**
- `parser.py` — new `extract_as_images()` function (renders PDF pages to base64 JPEG)
- `extractor.py` — new `SYSTEM_PROMPT_PAPER_VLM` and `extract_from_images()` function
- `extractor.py` — enhanced `_merge()` for page-level dedup with confidence resolution
- Doc-type routing logic in `_process_file()` (or its successor)

**New function in `parser.py`:**

```python
extract_as_images(pdf_path, dpi=200, max_dim=1536, fmt="jpeg", quality=85)
  → List[{page, image_b64, width, height, mime}]
```

**New in `extractor.py`:**

```python
SYSTEM_PROMPT_PAPER_VLM = "..."  # VLM variant of paper prompt (image-aware)
# Note: NO TDS VLM prompt needed — TDS always uses text path

extract_from_images(images: List[dict]) -> dict
  # Sends page images to VLM via LiteLLM (model="extraction-vlm")
  # Returns same schema as extract_from_text() for paper doc_type
```

**New in `llm.py`:**

```python
generate_with_images(model, messages_with_images, ...)
  # Handles multimodal content blocks (text + image_url)
  # Uses same OpenAI SDK, just different content format
```

**Updated routing in `_process_file()` (or successor):**

```python
def process_document(pdf_path, force_vlm=False):
    # Step 1: Detect document type
    #   Option A: from S3 key prefix (tds/ vs papers/)
    #   Option B: pdfplumber text → detect_document_type()
    chunks = extract_text(pdf_path)
    full_text = join(chunks)
    doc_type = detect_document_type(full_text)

    # Step 2: Route by document type
    if doc_type == "tds" and not force_vlm:
        # TEXT PATH — existing pipeline with better model
        raw = extract_from_text(full_text, doc_type="tds")
        if raw.get("extraction_confidence", 0) < 0.3:
            raise LowConfidenceError(raw)  # re-queue for VLM
    else:
        # VLM PATH — render pages as images, send to vision model
        images = extract_as_images(pdf_path)
        raw = extract_from_images(images)

    # Step 3: Normalize + validate against Pydantic schema
    return build_extraction_record(raw, pdf_path)
```

**Files:**
- Modified: `backend/parser.py` (add `extract_as_images()`)
- Modified: `backend/extractor.py` (add `SYSTEM_PROMPT_PAPER_VLM`, `extract_from_images()`, enhance `_merge()`)
- Modified: `backend/llm.py` (add multimodal message support)
- Modified: `backend/main.py` or new `backend/processor.py` (doc-type routing logic)

**Verification:**
1. Test with clean digital TDS → text path, same quality as Phase 1
2. Test with research paper → VLM path, pages rendered and sent as images
3. Test with multi-column paper → VLM extracts from all columns correctly
4. Test merge of page-level VLM results → dedup works, no duplicate properties
5. Test TDS with confidence < 0.3 → re-queued and processed via VLM

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

        # 3. Check for force_vlm attribute (from TDS re-queue on low confidence)
        force_vlm = msg.get("MessageAttributes", {}).get("force_vlm", {}).get("StringValue") == "true"

        # 4. Detect doc type from S3 key prefix (if available)
        doc_type_hint = "tds" if "/tds/" in s3_key.lower() else "paper" if "/paper" in s3_key.lower() else None

        # 5. Write PDF to temp file (needed for pymupdf/pdfplumber)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = tmp.name

        try:
            # 6. Process document — routes by doc type:
            #    TDS → text path (pdfplumber + LLM)
            #    Paper → VLM path (page images + vision model)
            result = process_document(tmp_path, doc_type_hint=doc_type_hint, force_vlm=force_vlm)

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
  - `TDSTextPathUsed`, `PaperVLMPathUsed`, `TDSRequeued` (count)
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
5. Upload a research paper PDF → VLM path triggered → correct extraction
6. Upload a TDS PDF → text path used → correct extraction
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
- Estimated time: depends on LLM throughput (assuming ~50/50 TDS/Paper split)
  - At 10 docs/min (TDS text path): ~83 hours for 50K TDS docs
  - At 3 docs/min (Paper VLM path): ~278 hours for 50K paper docs
  - Total: ~2-3 weeks with single consumer, ~5-7 days with increased concurrency

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
