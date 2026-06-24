# AWS Migration — Key Changes Required

**Project**: Planet Materials Labs PDF Bulk Parser
**Date**: 2026-06-22 (updated)
**Branch**: Dev/Dhara
**Author**: Applied AI / AWS Engineering

---

## 1. Executive Summary

The current system is a local-only FastAPI monolith that extracts structured material property data from PDFs using a local Ollama LLM (qwen2.5:3b). It uses **text-only extraction** (pdfplumber/pymupdf) — no vision model is involved. Migrating to AWS requires deploying on EC2, replacing Ollama with production-grade LLMs via LiteLLM (routing to Bedrock, Qwen3-VL, or other models), introducing event-driven processing via S3 + SQS, enforcing a strict Pydantic output schema, and writing enriched results (JSON + S3 metadata) to a persistent database.

The corpus is approximately **1 lakh (100,000) TDS/papers combined**, with continuous growth as new documents arrive via Google Drive → S3.

This document catalogs every component that must change, what replaces it, and open decisions that block implementation.

---

## 2. Current Architecture (As-Is)

```
Browser Upload → FastAPI (uvicorn, localhost:8000)
  → save PDF to local temp/
  → ThreadPoolExecutor (2 workers)
    → pdfplumber + pymupdf fallback (TEXT-ONLY extraction, no VLM)
    → Ollama qwen2.5:3b-instruct-q4_K_S (text-only LLM, localhost:11434)
    → in-memory cache (OrderedDict, 500 entries)
    → in-memory job store (dict, 20 jobs max)
  → SSE progress stream → browser
  → download ZIP or save to local folder
```

**Critical gap**: The current parser is **NOT a VLM pipeline**. It extracts text using pdfplumber (a text-layer parser), passes that raw text to a 3b parameter quantized model, and hopes for structured JSON. Scanned PDFs, image-embedded tables, and complex multi-column layouts produce garbage or empty output. This is the single biggest technical gap for production.

**Key files:**

| File | Role |
|---|---|
| `run.py` | Entrypoint, sys.path setup, uvicorn launch |
| `backend/main.py` | FastAPI routes, job orchestration, output normalization |
| `backend/parser.py` | PDF to text (pdfplumber primary, pymupdf fallback) — **text-only, no vision** |
| `backend/extractor.py` | LLM prompt construction, chunking, merge/dedup |
| `backend/llm.py` | Ollama HTTP client singleton, response caching |
| `backend/cache.py` | In-memory LRU cache (OrderedDict) |
| `backend/config.py` | YAML config loader, module-level constants |
| `config.yaml` | All runtime configuration |
| `templates/index.html` | Monolithic frontend (2,449 lines, inlined CSS/JS) |

---

## 3. Resolved Decisions

| Decision | Resolution |
|---|---|
| Compute platform | **EC2** (not Lambda, not ECS) — always-on instance hosting the consumer service |
| LLM gateway | **LiteLLM** — unified OpenAI-compatible proxy routing to Bedrock, Qwen3-VL, or other models |
| LLM model | **Bedrock Claude** (primary) / **Qwen3-VL via LiteLLM** (alternative) — 3b model confirmed unfit for production |
| Data source | **Google Drive → S3** → SQS event → EC2 consumer |
| Corpus scale | ~100,000 documents (TDS + papers combined), continuously growing |

---

## 4. Target Architecture (To-Be)

```
Google Drive (source of truth)
  │
  ▼ (sync / upload automation)
S3 Bucket (PDFs land here)
  │
  ├─ S3 Event Notification (ObjectCreated)
  │
  ▼
SQS Queue (buffering, retry, DLQ)
  │
  ▼
EC2 Instance (always-on consumer service)
  ├─ SQS Poller (long-polling, receives messages)
  ├─ 1. Parse SQS message → extract S3 bucket/key
  ├─ 2. Fetch PDF from S3 (boto3)
  ├─ 3. Retrieve S3 object metadata (HEAD request / user-defined headers)
  ├─ 4. Extract content from PDF
  │     Path A: pdfplumber/pymupdf (clean, text-heavy digital PDFs)
  │     Path B: VLM — render pages as images → send to Qwen3-VL or Claude Vision
  │             via LiteLLM (scanned, complex layout, image-heavy PDFs)
  ├─ 5. Detect document type (TDS vs Paper)
  ├─ 6. Chunk text → send to LLM via LiteLLM proxy
  │     LiteLLM routes to: Bedrock Claude / Qwen3-VL / other configured models
  ├─ 7. Merge chunk results → validate against strict Pydantic schema
  ├─ 8. Enrich: combine structured JSON + S3 metadata
  └─ 9. Write to database (DynamoDB or RDS Aurora)
           │
           ▼
      Downstream AI Agents consume structured records
```

### LiteLLM Integration Layer

```
EC2 Consumer Code
  │
  ▼
LiteLLM Proxy (OpenAI-compatible API, runs on same EC2 or as sidecar)
  ├─ bedrock/claude-3-5-sonnet    → AWS Bedrock
  ├─ ollama/qwen3-vl              → Self-hosted Ollama (if GPU available)
  ├─ openai/gpt-4o                → OpenAI (fallback)
  └─ any future model             → Zero code change in consumer
```

**Why LiteLLM matters**: The current `llm.py` is tightly coupled to Ollama's `/api/generate` endpoint. Replacing it with LiteLLM means `llm.py` becomes a thin OpenAI SDK client. Model switching becomes a config change, not a code change. This is critical for a 100K document pipeline where you may need to:
- Start with Claude for accuracy, switch to a cheaper model for bulk re-processing
- Use Qwen3-VL for image-heavy PDFs, Claude for text extraction
- A/B test model performance on extraction quality

---

## 5. Key Changes — Component by Component

### 5.1 PDF Ingestion: Local Upload → S3 + SQS

**What changes:**
- Remove browser upload endpoint (`POST /api/parse/upload` in `main.py:100-118`)
- Remove local `temp/` directory usage entirely
- Replace with SQS long-polling consumer on EC2
- PDF bytes fetched directly from S3 via `boto3.client('s3').get_object()`
- Local `/tmp` or in-memory processing on EC2 (no persistent temp files)

**Files affected:**
- `main.py` — remove upload route, `_start_job()`, `_run_job()`, `TEMP_DIR`
- New file needed: `backend/sqs_consumer.py` (SQS poller + orchestration)

**Blocked by:**
- SQS message schema definition (what fields, what format)
- S3 bucket name, prefix conventions, event filter rules
- Google Drive → S3 sync mechanism (manual, AWS Transfer Family, or custom)

---

### 5.2 PDF Content Extraction: Doc-Type Routed — TDS via LLM, Papers via VLM

**Current state (what exists today):**
- `parser.py` uses pdfplumber to extract the **text layer** from PDFs
- pymupdf is used as a fallback — also text-only
- **No vision capability whatsoever**
- Works well for TDS documents (clean, digital manufacturer PDFs)
- Fails on research papers (scanned pages, multi-column, complex figures)

**What must change:**

Two extraction paths routed by **document type**:

**TDS → Text path (existing pipeline, upgraded model):**
- pdfplumber text extraction works reliably for manufacturer TDS documents
- Existing `parser.py:extract_text()` and `clean_pdf_text()` are reusable as-is
- Chunking, merge, and system prompts all stay the same
- Just needs a better model (Bedrock Claude via LiteLLM instead of 3b)

**Paper → VLM path (new):**
- Render PDF pages as images using pymupdf (`page.get_pixmap()` — already in dependencies)
- Send page images to a vision-capable model via LiteLLM:
  - Qwen3-VL (if self-hosted via Ollama on GPU instance)
  - Claude 3.5 Sonnet Vision via Bedrock
- VLM performs **both** text extraction AND property extraction in one pass
- New `SYSTEM_PROMPT_PAPER_VLM` adapted for image input (same output schema)

**Routing decision:**

| Document Type | Extraction Path | Detection Method |
|---|---|---|
| **TDS** | Text (pdfplumber + LLM) | S3 key prefix `/tds/` or `detect_document_type()` |
| **Paper** | VLM (page images + vision model) | S3 key prefix `/papers/` or `detect_document_type()` |
| **TDS with confidence < 0.3** | VLM (re-queued) | Automatic re-queue on low extraction confidence |

**Cost implications at 100K documents (~50/50 split):**
- TDS text path: 50K docs x ~$0.03/doc = ~$1,500
- Paper VLM path: 50K docs x ~$0.25/doc = ~$12,500
- **Total: ~$14,000** for initial backlog

**Files affected:**
- `parser.py` — add `extract_as_images()` function alongside existing `extract_text()`
- `parser.py` — add routing logic to decide Path A vs Path B
- `extractor.py` — add VLM prompt variant that accepts image input
- `llm.py` — complete rewrite (see 5.3)

---

### 5.3 LLM Backend: Ollama → LiteLLM (COMPLETE REWRITE)

**What changes:**
- Remove `OllamaClient` class and all Ollama-specific code in `llm.py`
- Replace with OpenAI SDK client pointing at LiteLLM proxy
- LiteLLM runs on the same EC2 instance (or as a sidecar container)

**Current `llm.py` → New `llm.py`:**

| Current (Ollama) | New (LiteLLM) |
|---|---|
| `httpx.Client` → `POST /api/generate` | `openai.Client` → `POST /v1/chat/completions` |
| Ollama payload format (`prompt`, `system`, `format`) | OpenAI payload format (`messages`, `response_format`) |
| `is_running()` → `GET /api/tags` | Health check → `GET /health` on LiteLLM |
| `list_models()` → parse Ollama response | `client.models.list()` via OpenAI SDK |
| `_extract_json()` — regex JSON extraction | May still be needed; or use structured output / tool_use |
| Cache key: `model:system:prompt` | Cache key: `model:messages_hash` (adapt for chat format) |

**LiteLLM config (litellm_config.yaml on EC2):**
```yaml
model_list:
  - model_name: "extraction-primary"
    litellm_params:
      model: "bedrock/anthropic.claude-3-5-sonnet"
      aws_region_name: "us-east-1"

  - model_name: "extraction-vlm"
    litellm_params:
      model: "ollama/qwen3-vl"
      api_base: "http://localhost:11434"

  - model_name: "extraction-fallback"
    litellm_params:
      model: "openai/gpt-4o"
```

**Key benefit**: Consumer code calls `model="extraction-primary"` — LiteLLM handles routing, retries, and failover. Swapping models is a config change on EC2, not a code deployment.

**Files affected:**
- `llm.py` — complete rewrite to OpenAI SDK client
- `extractor.py` — update `extract_from_text()` to use chat completion format instead of raw generate
- `config.py` — replace Ollama config with LiteLLM config

**Config changes:**
- Remove: `ollama.base_url`, `ollama.timeout`, `ollama.num_gpu`, `ollama.num_ctx`, `ollama.keep_alive`
- Add: `litellm.proxy_url` (e.g., `http://localhost:4000`), `litellm.model_text`, `litellm.model_vlm`, `litellm.timeout`, `litellm.max_tokens`

---

### 5.4 Output Schema: Implicit Dict → Strict Pydantic

**What changes:**
- Current output is a plain `dict` constructed in `main.py:_process_file()` (lines 367-388)
- No validation — if the LLM returns garbage, it flows through silently
- Replace with Pydantic `BaseModel` classes that validate and enforce types

**Schema models to create:**

```
MaterialProperty        — name, value, unit, confidence, context
ProcessingCondition     — name, value, confidence (TDS only)
KeyFinding              — finding, confidence (Paper only)
Limitation              — limitation, confidence (Paper only)
TDSExtraction           — TDS-specific fields + properties list
PaperExtraction         — Paper-specific fields + properties list
MaterialExtraction      — discriminated union on doc_type
S3ObjectMetadata        — bucket, key, content_type, size, last_modified, custom metadata
ExtractionRecord        — MaterialExtraction + S3ObjectMetadata + processing metadata
```

**Open decisions:**
- Should `value` in `MaterialProperty` be `float | str` or strictly numeric? Current data has ranges like `"12.5 - 15.0"`
- Should property names be an open `str` or a constrained `Enum` of known properties?
- Required vs Optional fields — fail hard or default gracefully?
- Should the schema include a version field for forward compatibility?

**Files affected:**
- New file: `backend/schemas.py` (Pydantic models)
- `main.py:_process_file()` — construct Pydantic model instead of raw dict
- `extractor.py` — validate LLM output against schema before returning

---

### 5.5 S3 Metadata Enrichment (New Capability)

**What's new:**
- After extracting material properties from PDF content, retrieve the S3 object's metadata
- Combine both into the final database record

**S3 metadata retrieval:**
```
boto3.client('s3').head_object(Bucket=bucket, Key=key)
→ ContentType, ContentLength, LastModified, ETag
→ response['Metadata']  # user-defined x-amz-meta-* headers
```

**Blocked by:**
- Which custom metadata fields will be attached to S3 objects by upstream systems?
- Examples: material-id, supplier, source-system, upload-timestamp, requestor

**Files affected:**
- New logic in `sqs_consumer.py`
- New Pydantic model `S3ObjectMetadata`

---

### 5.6 Caching: In-Memory → Persistent Cache

**What changes:**
- Current: `cache.py` — `InMemoryCache` (OrderedDict, 500 entries, no persistence)
- Lost on every EC2 restart, single-process only

**Options:**

| Option | Pros | Cons |
|---|---|---|
| **ElastiCache Redis** | Fast, TTL support, shared across processes | VPC cost, managed service |
| **DynamoDB** | Serverless, pay-per-use, persistent | Higher latency (~5-10ms) |
| **Local Redis on EC2** | Fast, simple, no extra AWS cost | Lost if instance replaced |
| **No cache** | Simplest | Re-processes identical PDFs every time |

**Recommendation for EC2 deployment:** Local Redis on same EC2 instance. Simple, fast, no extra AWS cost. At 100K documents the cache prevents expensive re-processing of duplicates. If the instance is replaced, cache rebuilds naturally. Upgrade to ElastiCache only if scaling to multiple instances.

**Files affected:**
- `cache.py` — replace `InMemoryCache` with Redis client (redis-py)
- `extractor.py` — cache lookup/store calls stay the same if interface preserved
- `llm.py` — same

---

### 5.7 Configuration: YAML File → Environment Variables + SSM

**What changes:**
- Current: `config.yaml` on disk, loaded at import, live-reloadable via API
- EC2 can still use config files, but secrets and environment-specific values should come from AWS

**Replace with:**
- Environment variables for EC2 instance config (loaded from SSM at boot or via systemd)
- AWS Secrets Manager for sensitive values (API keys, database credentials)
- `config.yaml` can remain as local defaults, overridden by env vars

**Config values to migrate:**

| Current Key | Target |
|---|---|
| `ollama.*` | Remove entirely |
| `parser.*` | Keep in `config.yaml` or env vars |
| `app.host`, `app.port` | Keep for EC2 service binding |
| LiteLLM proxy URL | Env var `LITELLM_PROXY_URL` |
| LiteLLM model names | Env var or LiteLLM config file |
| Database connection | Secrets Manager |
| S3 bucket name | Env var `S3_SOURCE_BUCKET` |
| SQS queue URL | Env var `SQS_QUEUE_URL` |

**Files affected:**
- `config.py` — add env var override layer on top of YAML loading
- `config.yaml` — remove Ollama section, add LiteLLM + AWS sections

---

### 5.8 Database Write (New Capability)

**What's new:**
- Current system has no database — results live in-memory and are downloaded as ZIP
- AWS version must persist every extraction record to a database

**Options:**

| Option | Best For |
|---|---|
| **DynamoDB** | Document-oriented, auto-scaling, serverless, natural JSON storage |
| **RDS Aurora PostgreSQL** | Complex queries, joins, full-text search, analytics |
| **Both** | DynamoDB for fast lookup by material/doc_id, Aurora for analytics |

**Record structure (what gets written):**
```
{
  // Extraction result (from Pydantic schema)
  doc_id, filename, doc_type, material_name, properties, ...

  // S3 metadata
  s3_bucket, s3_key, content_type, file_size, upload_timestamp, custom_metadata,

  // Processing metadata
  extraction_timestamp, model_used, processing_duration_ms, schema_version,
  chunks_processed, extraction_confidence
}
```

**Blocked by:**
- Database choice (DynamoDB vs RDS vs both)
- Partition key design if DynamoDB (likely `doc_id` as PK, `material_name` as SK or GSI)
- Who/what queries this data downstream? (AI agents — what access pattern?)

---

### 5.9 Job Orchestration: In-Memory Dict → SQS-Driven

**What changes:**
- Current: `_jobs` dict in `main.py` (max 20, lost on restart)
- Current: SSE progress stream to browser

**On EC2:**
- SQS long-polling consumer replaces the in-memory job store
- Each SQS message = one PDF to process
- SQS visibility timeout = max processing time per PDF (set to ~10 minutes)
- Failed messages → DLQ after N retries (configurable)
- Progress tracking: CloudWatch metrics + optional SNS notifications

**Concurrency on EC2:**
- Current ThreadPoolExecutor(max_workers=2) can be increased
- Or use asyncio with concurrent SQS message processing
- Throttled by LLM inference speed (LiteLLM handles queueing to model endpoints)

**Files affected:**
- `main.py` — remove `_jobs`, `_job_queues`, `_run_job()`, `_start_job()`, `_evict_old_jobs()`
- Remove SSE progress endpoint (`/api/progress/{job_id}`)
- Remove result/download endpoints (results go to database, not browser)
- New: `sqs_consumer.py` handles all orchestration

---

### 5.10 Frontend: Remove or Decouple

**What changes:**
- Current: `templates/index.html` (2,449 lines) — monolithic SPA served by FastAPI
- In AWS pipeline mode: no browser UI needed for the extraction pipeline
- Frontend becomes a separate concern (dashboard for viewing extraction results from database)

**Recommendation:**
- Remove frontend from the extraction service entirely
- If a monitoring UI is needed later: separate app querying the database directly

**Files affected:**
- `templates/index.html` — remove from pipeline service
- `main.py` — remove `/` route, static file mount, Jinja2 templates
- `static/` — remove from pipeline service

---

### 5.11 Error Handling & Observability

**What changes:**
- Current: `logging` module (recently added), stdout only
- On EC2: structured logging for CloudWatch Logs agent

**Add:**
- Structured JSON logging (timestamp, request_id, s3_key, doc_type, duration)
- CloudWatch agent on EC2 to ship logs
- CloudWatch custom metrics: extraction_success, extraction_failure, llm_latency, cache_hit_rate
- CloudWatch Alarms on DLQ depth, error rate, LLM timeout rate
- SNS topic for critical failures (e.g., model unavailable, schema validation failure)

**Files affected:**
- All backend files — update logging format to JSON
- New: CloudWatch metrics publishing utility
- EC2: install and configure CloudWatch agent

---

### 5.12 IAM & Security

**New requirements:**
- EC2 instance role (instance profile) needs:
  - `s3:GetObject`, `s3:HeadObject` on source bucket
  - `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:ChangeMessageVisibility` on input queue
  - `bedrock:InvokeModel` on target model (if using Bedrock directly alongside LiteLLM)
  - `dynamodb:PutItem`, `dynamodb:GetItem` (or `rds-data:ExecuteStatement`) on target table
  - `ssm:GetParameter` for config
  - `secretsmanager:GetSecretValue` for credentials
  - `logs:CreateLogGroup`, `logs:PutLogEvents` for CloudWatch
  - `cloudwatch:PutMetricData` for custom metrics
- Security group: inbound SSH (restricted IP), outbound HTTPS (S3, SQS, Bedrock, LiteLLM models)
- No public-facing ports unless health check endpoint needed
- Principle of least privilege — no `*` resources

---

### 5.13 Infrastructure as Code

**New requirement:**
- All AWS resources defined in IaC
- Resources to provision:
  - EC2 instance (with instance profile, security group, user data script)
  - S3 bucket (or use existing) + event notification to SQS
  - SQS queue + DLQ
  - DynamoDB table (or RDS Aurora cluster)
  - SSM parameters for config
  - Secrets Manager entries
  - IAM roles and policies
  - CloudWatch log groups, dashboards, alarms
  - Optional: Redis (local install via user data, or ElastiCache)
  - Optional: LiteLLM proxy config deployed via user data or Docker

**Decision required:**
- CDK (Python, natural fit) vs Terraform vs CloudFormation?

---

### 5.14 Google Drive → S3 Sync (New Capability)

**What's new:**
- Documents originate in Google Drive
- Must automatically land in S3 to trigger the pipeline

**Options:**

| Option | Pros | Cons |
|---|---|---|
| **AWS Transfer Family** | Managed, SFTP/FTPS support | No native Google Drive connector |
| **Google Drive API + Lambda** | Event-driven, serverless | Must handle OAuth, pagination, rate limits |
| **rclone on EC2** | Simple, cron-based, supports Drive natively | Not event-driven, polling-based |
| **Third-party** (Zapier, n8n, etc.) | Fast to set up | External dependency, cost at scale |

**Blocked by:**
- How are documents currently added to Drive? (manual, automated, from suppliers?)
- Is near-real-time sync needed or is periodic (e.g., every 15 minutes) acceptable?
- Google Workspace admin access for API credentials

---

## 6. What's Reusable As-Is

| Component | File | Reusable? | Notes |
|---|---|---|---|
| PDF text extraction | `parser.py` | Yes | Keep for clean digital PDFs (Path A) |
| Text cleaning | `parser.py:clean_pdf_text()` | Yes | Unicode normalization, whitespace collapse |
| Document type detection | `extractor.py:detect_document_type()` | Yes | Keyword-based classifier, model-agnostic |
| Chunking | `extractor.py:_split_chunks()` | Yes | Configurable size/overlap |
| Merge/dedup | `extractor.py:_merge()` | Yes | Cross-chunk deduplication logic |
| System prompts | `extractor.py` | Yes | Model-agnostic, work with any LLM |
| Material name regex | `main.py:_extract_material_name_regex()` | Yes | Fallback extraction |
| Output normalization | `main.py:_process_file()` | Partial | Logic reusable, needs Pydantic wrapper |
| Cache interface | `cache.py` | Interface only | Backend changes to Redis/DynamoDB |
| Ollama client | `llm.py` | **No** | Complete rewrite to OpenAI SDK via LiteLLM |
| Job orchestration | `main.py` | **No** | Replaced by SQS consumer |
| Frontend | `templates/index.html` | **No** | Removed from pipeline service |
| Config loader | `config.py` | Partial | Add env var / SSM override layer |

---

## 7. Open Decisions (Remaining Blockers)

| # | Decision | Owner | Blocks |
|---|---|---|---|
| 1 | SQS message schema (fields, format) | Platform/Infra team | Consumer implementation |
| 2 | S3 custom metadata contract | Upstream data providers | Metadata enrichment |
| 3 | Target database (DynamoDB vs RDS vs both) | Architecture | Schema design, write logic |
| 4 | VLM routing strategy (text-first with fallback vs pure VLM) | AI Engineering | Parser refactor scope |
| 5 | Pydantic schema strictness (required vs optional, enum vs open string) | AI Engineering + Domain | Schema file |
| 6 | IaC tooling (CDK vs Terraform vs CloudFormation) | DevOps/Infra | Infrastructure provisioning |
| 7 | Downstream AI agent access pattern | AI Agents team | Database key design, GSIs |
| 8 | Google Drive → S3 sync mechanism | Platform/Infra team | Data ingestion |
| 9 | EC2 instance type (GPU needed for self-hosted Qwen3-VL?) | AI Engineering + Infra | Instance provisioning, cost |

**Previously resolved:**
- ~~Compute choice~~: **EC2** (confirmed)
- ~~LLM gateway~~: **LiteLLM** (confirmed)
- ~~LLM model~~: **Bedrock Claude / Qwen3-VL via LiteLLM** (confirmed, 3b model rejected)
- ~~Cache strategy~~: **Local Redis on EC2** (recommended, upgrade to ElastiCache if scaling)

---

## 8. Suggested Implementation Order

1. **Pydantic schema** (`schemas.py`) — no AWS dependency, can start immediately
2. **LLM client rewrite** — replace Ollama client with OpenAI SDK → LiteLLM proxy
3. **VLM extraction path** — add image-based extraction alongside text path in `parser.py`
4. **SQS consumer** (`sqs_consumer.py`) — once message schema is defined
5. **S3 integration** — fetch PDF + retrieve metadata
6. **Config migration** — add env var / SSM override layer to `config.py`
7. **Cache migration** — Redis backend for `cache.py`
8. **Database write** — once target DB is decided
9. **Observability** — structured JSON logging, CloudWatch agent, metrics, alarms
10. **IaC** — provision all EC2, SQS, S3, DynamoDB resources
11. **Drive → S3 sync** — once sync mechanism decided
12. **Integration testing** — end-to-end with real S3 events on EC2

---

## 9. Risk Register

| Risk | Impact | Mitigation |
|---|---|---|
| 100K document backlog overwhelms single EC2 | Days of processing backlog | Batch initial corpus with higher concurrency; steady-state handles new arrivals |
| Bedrock throttling under burst load | Processing delays | LiteLLM retry/fallback to Qwen3-VL; request Bedrock provisioned throughput |
| VLM cost escalation (image tokens at 100K docs) | Budget overrun | Hybrid routing: text-first, VLM only for low-confidence/failed extractions |
| LiteLLM proxy becomes single point of failure | Pipeline stops | Run LiteLLM as systemd service with auto-restart; health check endpoint |
| Schema migration breaks downstream agents | Data pipeline failure | Version the schema, maintain backward compatibility |
| EC2 instance failure loses in-flight work | Partial data loss | SQS visibility timeout returns message to queue; idempotent processing |
| Google Drive sync misses files | Data gaps | Periodic full reconciliation scan (compare Drive listing vs S3 inventory) |
| S3 event duplication (at-least-once delivery) | Duplicate database records | Idempotency via S3 ETag or content hash as dedup key |

---

## 10. EC2 Instance Planning

**If using Bedrock only (no self-hosted models):**
- Instance: `t3.xlarge` (4 vCPU, 16GB RAM) — sufficient for PDF parsing + SQS consumer
- Cost: ~$120/month (on-demand), ~$75/month (1-yr reserved)
- No GPU needed

**If self-hosting Qwen3-VL via Ollama alongside Bedrock:**
- Instance: `g4dn.xlarge` (4 vCPU, 16GB RAM, 1x NVIDIA T4 16GB VRAM)
- Cost: ~$380/month (on-demand), ~$230/month (1-yr reserved)
- Runs both LiteLLM proxy + Ollama with Qwen3-VL on same box

**Recommendation:** Start with `t3.xlarge` + Bedrock. Add GPU instance only if Qwen3-VL extraction quality justifies the cost. LiteLLM makes this a config swap, not a code change.

**Services running on EC2:**
- SQS consumer (Python service, systemd-managed)
- LiteLLM proxy (systemd or Docker, port 4000)
- Redis (local install, port 6379)
- Optional: Ollama (if GPU instance, port 11434)
- CloudWatch agent (log shipping)
