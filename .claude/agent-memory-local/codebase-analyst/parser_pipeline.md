---
name: Document Parser Pipeline — Step-by-Step
description: How PDFs are converted to structured property data in Qdrant
type: project
---

## Parsing Pipeline (Document Input → Storage)

**File Locations**:
- Entry point: `E:/Rlresearchassistant/backend/main.py:105-132` (upload endpoint)
- PDF text extraction: `E:/Rlresearchassistant/backend/parser.py:7-36`
- Document type detection: `E:/Rlresearchassistant/backend/parser.py:103-129`
- LLM property extraction: `E:/Rlresearchassistant/backend/extractor.py:1-200+`
- Job execution: `E:/Rlresearchassistant/backend/job_queue.py` (line 100+)
- Storage: `E:/Rlresearchassistant/backend/qdrant_store.py`

### Step 1: Upload → Job Queue
**Location**: `main.py:105-132` (`POST /api/documents/upload`)

```
PDF file uploaded → temp file written to data/uploads/
Job created with priority (size-based: <1MB=HIGH, 1-10MB=MEDIUM, >10MB=LOW)
Job queued for background processing
Returns job_id to frontend for progress tracking
```

**Job Structure** (`job_queue.py:51-72`):
- `job_id`: UUID
- `filename`, `file_path`, `file_size`
- `status`: PENDING → QUEUED → RUNNING → COMPLETED/FAILED
- `doc_type`: initially "pending", set during parsing
- `progress`, `current_step`, `error_message`
- `confidence`: extraction confidence score
- `properties_count`: number of properties extracted

### Step 2: Background Processing (Job Worker)
**Location**: `job_queue.py:130+` (worker thread)

The job worker processes jobs in priority order:

```
FOR each job in queue:
  1. Set status → RUNNING
  2. Call _process_job()
     a. extract_text(pdf_path)          [parser.py]
     b. detect_doc_type(text)           [parser.py]
     c. extract_from_text(text, type)   [extractor.py]
     d. store_document() in Qdrant      [qdrant_store.py]
  3. On success: status → COMPLETED
  3. On error: status → FAILED (retry up to 3 times)
```

### Step 3: PDF Text Extraction
**Location**: `parser.py:7-55`

**Process**:
1. Try `pdfplumber.open(pdf_path)` - extracts text page-by-page
2. For each page:
   - Extract plain text via `page.extract_text()`
   - Clean text: remove control chars, normalize quotes/dashes
   - Extract tables via `page.extract_tables()` → `table_to_string()`
   - Chunk per-page output
3. **Fallback**: If pdfplumber fails, use `pymupdf (fitz)` as backup
4. **Output**: List of chunks with metadata:
   ```json
   [
     {"type": "text", "content": "cleaned_text...", "page": 1},
     {"type": "table", "content": "col1 | col2 | ...", "page": 2, "raw_table": [...]}
   ]
   ```

**Cleaning** (`parser.py:57-90`):
- Replace Unicode corruption (\\x00, \\ufffd, etc.)
- Normalize quotes/dashes
- Collapse multiple whitespace
- Filter spam lines (e.g., "050323")
- Collapse >2 consecutive newlines

### Step 4: Document Type Detection
**Location**: `parser.py:103-129` and `extractor.py:119-170+`

**Strategy**: Keyword scoring (TDS vs Paper)

**TDS Indicators** (`parser.py:104-113`):
- "technical data sheet", "datasheet", "typical properties", "mechanical properties"
- Test standards: "iso ", "astm ", "ul ", "iec "
- Properties: "tensile strength", "density", "shore hardness", "melt temperature"
- Processing terms: "injection molding", "cure temperature", "drying conditions"

**Paper Indicators** (`parser.py:115-122`):
- "abstract", "introduction", "methodology", "conclusion", "references"
- "doi:", "journal"

**Enhanced Detection** (`extractor.py:6A`, line 126+):
- Expanded nanocomposite/functional-material vocabulary
- TDS bias (+2 weight) to prevent misclassification of advanced TDS docs
- Returns: "tds" or "paper"

### Step 5: LLM-Based Property Extraction
**Location**: `extractor.py:19-200+`

**Two LLM Prompts** (tailored by doc type):

**TDS Extraction** (`extractor.py:22-56`):
```
System prompt instructs LLM to extract ONLY JSON with structure:
{
  "material_name": string,
  "extraction_confidence": 0.0-1.0,
  "properties": [
    {"name": string, "value": number|string, "unit": string, 
     "confidence": 0.0-1.0, "context": "test_standard"}
  ],
  "processing_conditions": [...]
}

Properties covered: mechanical, thermal, electrical, physical, filler-specific
Max context: 7000 chars (tables front-loaded in TDS)
```

**Paper Extraction** (`extractor.py:58-93`):
```
Extracts:
{
  "extraction_confidence": 0.0-1.0,
  "material_properties_mentioned": [...],
  "key_findings": [{"finding": string, "confidence": float}],
  "methodology": string,
  "research_objective": string
}

Max context: 12000 chars (data scattered in results/discussion)
```

**Process**:
1. Take extracted text from Step 3
2. Chunk into 4000-char overlapping chunks (`extractor.py:109-116`)
3. For each chunk, prompt LLM (3b-7b model)
4. Parse JSON response (with retry on failure)
5. Merge multiple chunk results into unified extraction
6. **Caching**: Responses cached by text hash to avoid re-running (`cache.py`)

### Step 6: Storage in Qdrant
**Location**: `qdrant_store.py:150+` and `job_queue.py:_process_job()`

**Collections** (all stored as flat payloads, no nested metadata):

1. **documents** (manifest per file):
   ```json
   {
     "id": "doc-uuid",
     "filename": "paper.pdf",
     "file_hash": "sha256_hash",
     "doc_type": "tds" or "paper",
     "material_name": "extracted_name",
     "status": "indexed",
     "created_at": "ISO8601",
     "extraction_confidence": 0.85,
     "total_chunks": 12,
     "total_properties": 24
   }
   ```

2. **doc_chunks** (searchable text chunks with vectors):
   ```json
   {
     "id": "chunk-uuid",
     "doc_id": "doc-uuid",
     "chunk_index": 0,
     "content": "cleaned_text...",
     "page": 1,
     "chunk_type": "text" or "table",
     "material_name": "extracted_name",
     "vector": [0.12, -0.45, ...] (768-dim nomic-embed-text)
   }
   ```

3. **material_properties** (structured property rows):
   ```json
   {
     "id": "prop-uuid",
     "doc_id": "doc-uuid",
     "doc_type": "tds" or "paper",
     "material_name": "material_name",
     "property_name": "Tensile Strength",
     "value": 45.3,
     "unit": "MPa",
     "confidence": 0.91,
     "context": "ISO 527-1",
     "processing_conditions": "{}",
     "extracted_at": "ISO8601"
   }
   ```

4. **knowledge_edges** (knowledge graph: material → property → value relationships)

**Indexing** (`qdrant_store.py:128-144`):
- `documents.file_hash` (keyword index)
- `documents.doc_type`, `documents.status`, `documents.material_name`
- `doc_chunks.doc_id`, `doc_chunks.material_name`
- `material_properties.doc_id`, `material_properties.property_name`, `material_properties.material_name`

### Summary: Data Flow Diagram

```
Upload PDF
    ↓
[main.py:105] Create Job, enqueue
    ↓
[job_queue.py] Background worker dequeues
    ↓
[parser.py:extract_text] PDF → text chunks (pdfplumber → pymupdf fallback)
    ↓
[parser.py:detect_doc_type] TDS? Paper?
    ↓
[extractor.py:extract_from_text] Chunk via LLM, extract properties
    ↓
[qdrant_store.py] Store:
    - documents (manifest)
    - doc_chunks (vectors)
    - material_properties (structured rows)
    - knowledge_edges (graph)
    ↓
Job.status = COMPLETED
Return to frontend via /api/jobs/{job_id}
```

### Key Parameters

| Component | File | Parameter | Value |
|-----------|------|-----------|-------|
| Chunk size (parsing) | parser.py | (implicit) | ~page-based, then regex chunks |
| Chunk size (extraction) | extractor.py | CHUNK_SIZE | 4000 chars |
| Chunk overlap | extractor.py | CHUNK_OVERLAP | 300 chars |
| TDS extract limit | extractor.py | TDS_EXTRACT_CHARS | 7000 chars |
| Paper extract limit | extractor.py | PAPER_EXTRACT_CHARS | 12000 chars |
| LLM retries | extractor.py | MAX_RETRIES | 1 (fail fast) |
| Job priorities | job_queue.py | JobPriority | HIGH (<1MB), MEDIUM (1-10MB), LOW (>10MB) |
| Max job retries | job_queue.py | max_retries | 3 |
| Embedding dim | qdrant_store.py | EMBED_DIM | 768 |
| Embedding model | config.py | EMBED_MODEL | nomic-embed-text |
