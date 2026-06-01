---
name: System Architecture
description: High-level architecture: frontend, backend, APIs, data flow
type: project
---

## System Architecture: RL Research Assistant

### Deployment Model
- **Frontend**: React + Vite (runs on http://localhost:5173, desktop via Tauri)
- **Backend**: FastAPI + Uvicorn (runs on http://localhost:8000)
- **Vector DB**: Qdrant (runs on http://localhost:6333, in-memory or file-based)
- **LLM**: Ollama (runs on http://localhost:11434 for inference)

### Frontend (React 18, `E:/Rlresearchassistant/src/`)

**Main Components**:
- `App.jsx`: Shell with navigation, stats polling, loop state management
- `Sidebar.jsx`: Navigation between views (Research, Papers, Experiments, Results, Decisions, Chat)
- `GoalPanel.jsx`: Define optimization goal, weights, schema selection
- `KnowledgePanel.jsx`: Display extracted materials, properties, insights
- `ExperimentDashboard.jsx`: Bayesian Optimization results visualization
- `ResultsPanel.jsx`: Trend charts (iteration vs. metric scores)
- `DecisionPanel.jsx`: Reasoning + approval UI for best candidate
- `ChatPanel.jsx`: RAG Q&A interface over indexed documents
- `ExperimentsPanel.jsx`: Historical experiment log
- `PapersView.jsx`: Browsable document library with metadata

**Polling Intervals** (`App.jsx:38-46`):
- Stats: `/api/stats` every 30 seconds
- Loop status: `/api/loop/status` every 10 seconds

**State Management**: React hooks + localStorage (sidebar collapse state)

### Backend Architecture (FastAPI, `E:/Rlresearchassistant/backend/`)

#### HTTP Endpoints

**Core APIs** (`main.py`):
- `GET /` — health check
- `GET /health` — Ollama + Qdrant status
- `GET /api/stats` — doc counts, chunks, experiment count (cached 30s)
- `POST /api/documents/upload` — ingest PDF, create job
- `GET /api/jobs` — list all jobs with pagination
- `GET /api/jobs/{job_id}` — get single job status

**Loop APIs** (`twin_routes.py`):
- `POST /api/loop/start` — initialize new optimization loop
- `POST /api/loop/iterate` — run next iteration (generate candidates, evaluate, decide)
- `POST /api/loop/approve` — approve best candidate, trigger GP retrain
- `POST /api/loop/stop` — stop the loop
- `GET /api/loop/status` — fetch current loop state
- `PUT /api/loop/hypothesis` — edit next hypothesis before iteration
- `GET /api/loop/candidates` — fetch current iteration candidates
- `GET /api/loop/history` — fetch iteration history

**Chat/RAG APIs**:
- `POST /api/chat/query` — semantic search over documents + LLM synthesis

**Experiment APIs**:
- `GET /api/experiments` — all experiments
- `GET /api/experiments/{exp_id}` — single experiment with full details

#### Core Services

1. **Job Queue** (`job_queue.py`):
   - Priority queue for document processing
   - Background worker thread that processes jobs sequentially
   - Persists job status in Qdrant (`job_status` collection)
   - Retry logic (max 3 retries on failure)

2. **Parser** (`parser.py`):
   - Extracts text from PDFs using pdfplumber (with pymupdf fallback)
   - Cleans OCR artifacts, normalizes whitespace
   - Detects tables and converts to string format
   - Returns: list of page-indexed text chunks

3. **Extractor** (`extractor.py`):
   - LLM-based property extraction from text
   - Two specialized prompts: TDS vs. Paper
   - Chunks text (4000 chars with 300-char overlap)
   - Caches LLM responses by text hash to avoid redundant calls
   - Returns: structured extraction JSON (properties, findings, conditions)

4. **Qdrant Store** (`qdrant_store.py`):
   - Vector DB abstraction layer
   - Manages collections: documents, doc_chunks, material_properties, experiments, knowledge_edges
   - Embedding via OllamaEmbeddings (nomic-embed-text, 768-dim)
   - Implements search, count, and filtering operations
   - Payload indexing on metadata fields

5. **Orchestrator** (`orchestrator.py`):
   - State machine for autonomous research loop
   - States: IDLE → RUNNING → AWAITING_APPROVAL → (loop)
   - Per-iteration pipeline:
     1. **Retrieve**: semantic search over documents for relevant context
     2. **Seed/Acquire**: generate candidates (LLM or Bayesian Optimization)
     3. **Predict**: use surrogate model to predict properties
     4. **Decide**: rank candidates, generate reasoning
     5. **Await Approval**: present best candidate to human
   - Thread-safe state with locking

6. **Surrogate Model** (`surrogate/`):
   - Trains Gaussian Process (scikit-learn) on approved experiments
   - Acquisition function: Upper Confidence Bound (UCB)
   - Suggests next experiment configuration
   - Retrains when user approves (active learning)

7. **LLM Client** (`llm.py`):
   - Wraps Ollama API
   - Connection pooling
   - Models: 3b (fast extraction), 7b/14b (quality), embedding model

8. **Cache** (`cache.py`):
   - In-memory LLM response cache (keyed by prompt hash)
   - 60s TTL for document lookup cache
   - Stats cache (30s)
   - Reduces redundant LLM calls

9. **Knowledge Graph** (`knowledge_graph.py`):
   - Builds material → property → value graph from extractions
   - Supports reasoning over relationships

#### Database: Qdrant Collections

| Collection | Purpose | Vector? | Key Fields |
|------------|---------|---------|-----------|
| documents | File manifest | No | file_hash, doc_type, material_name, status |
| doc_chunks | Searchable text chunks | **Yes** (768-dim) | doc_id, content, page, material_name |
| material_properties | Structured properties | No | doc_id, material_name, property_name, value, unit |
| experiments | Loop iteration results | No | iteration, status, composite_score, candidates |
| knowledge_edges | Knowledge graph triples | No | source_node, target_node, edge_type |
| job_status | Background job tracking | No | job_id, status, progress, doc_id |
| chat_sessions | RAG session logs | No | session_id, turns, created_at |
| schemas | Experiment schemas (Phase 7) | No | schema_id, inputs, outputs |

### Data Flow: Full Request Lifecycle

#### Upload & Index a Document

```
Frontend:
  User selects PDF
  POST /api/documents/upload → FastAPI

Backend:
  1. Save file to data/uploads/
  2. Create Job object (priority based on size)
  3. Enqueue in job_queue
  4. Return {job_id, status: "queued"}

Background Worker (async):
  1. Dequeue high-priority jobs first
  2. extract_text(pdf_path) → chunks
  3. detect_doc_type(text) → "tds" or "paper"
  4. extract_from_text(text, type) → {properties, findings, ...}
  5. store_document() in Qdrant:
     - Add row to documents collection
     - Add each chunk + embedding to doc_chunks
     - Add each property to material_properties
     - Add edges to knowledge_edges
  6. Update job.status → COMPLETED

Frontend (polling):
  GET /api/jobs/{job_id} → status updates
  GET /api/stats → refresh document counts
```

#### Run Optimization Loop

```
Frontend:
  User sets goal, weights, schema_id
  POST /api/loop/start → {goal, weights, schema_id}

Backend Orchestrator:
  Iteration 1:
    1. Retrieve: search Qdrant for relevant papers/TDS
    2. Acquire: LLM generates candidate configs (or BO if schema_id)
    3. Predict: surrogate model scores each candidate
    4. Decide: rank by composite score, generate reasoning
    5. Store experiment in Qdrant
    Status → AWAITING_APPROVAL
    Return to frontend

Frontend:
  Display best candidate + reasoning + 3 top candidates
  User clicks Approve or Edit Hypothesis

Backend:
  If Approve:
    - Record in surrogate.registry
    - Trigger GP retrain
    - Status → RUNNING
    POST /api/loop/iterate (loop continues)
  
  If Edit:
    - PUT /api/loop/hypothesis {new_text}
    - User clicks Approve
    - Continue as above
```

#### Chat/RAG Query

```
Frontend:
  User: "What materials have high EMI shielding?"
  POST /api/chat/query {query}

Backend:
  1. Embed user query (nomic-embed-text)
  2. Vector search in doc_chunks (top-K=10)
  3. Retrieve material properties from search results
  4. Prompt LLM:
     - System: "Answer based on materials science knowledge"
     - Context: retrieved chunk text + extracted properties
     - User query
  5. Stream response back
```

### Configuration

**Central Config**: `config.yaml` (loaded by `config.py`)

```yaml
server:
  api_port: 8000
  host: 0.0.0.0

paths:
  data_dir: data
  parsed_dir: data/parsed
  qdrant_storage: data/qdrant_storage
  uploads_dir: data/uploads
  surrogates_dir: data/surrogates

llm:
  base_url: http://localhost:11434
  models:
    extraction: qwen2.5:3b-instruct-q4_K_S
    chat: qwen2.5:3b-instruct-q4_K_S
    orchestrator: qwen2.5:3b-instruct-q4_K_S
    embedding: nomic-embed-text

qdrant:
  url: http://localhost:6333
```

All paths become absolute via `Path(__file__).parent / config_value`.

### Key Design Patterns

1. **Qdrant-only storage**: No DuckDB, no SQL — all data lives in Qdrant collections as flat payloads with string/number fields
2. **Priority job queue**: Size-based prioritization prevents large PDFs from blocking fast TDS extractions
3. **Fail-fast extraction**: 3b model for speed; 1 retry max to reduce latency
4. **Caching at multiple levels**: LLM response cache, document lookup cache, stats cache
5. **Async background processing**: Job queue decouples slow parsing from HTTP request handling
6. **Thread-safe orchestrator**: Locking around state updates during iterations
7. **Active learning**: Surrogate model retrains when user approves, improves next iteration
