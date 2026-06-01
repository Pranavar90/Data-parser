---
name: RL Research Assistant Project Overview
description: Autonomous materials research & development optimization loop with document ingestion
type: project
---

## Project: RL Research Assistant (Materials R&D Automation)

**Purpose**: An autonomous research optimization system for materials science. Users define composition optimization goals (e.g., "maximize tensile strength >45 MPa while maintaining elongation >180%"). The system:
1. Ingests research papers and technical datasheets (PDFs)
2. Extracts material properties using LLM extraction
3. Seeds hypotheses from literature and prior knowledge
4. Uses a Bayesian Optimization surrogate model to suggest formulations
5. Generates experiment configurations
6. Iterates with human approval (decision loop)

**Tech Stack**:
- Frontend: React 18 + Vite (Tauri desktop app support)
- Backend: FastAPI (Python) + Uvicorn
- Vector DB: Qdrant (vector embeddings + payload storage)
- LLM: Ollama (local inference, pluggable models: Qwen 2.5, Llama, Gemma)
- Search/Embedding: nomic-embed-text (768-dim)
- Surrogates: scikit-learn GPs, Bayesian Optimization
- Data: JSON payloads in Qdrant, no traditional RDBMS

**Key Modules**:
- Frontend: `src/App.jsx` (main shell), components in `src/components/`
- Backend: `backend/main.py` (FastAPI server), `backend/orchestrator.py` (loop state machine)
- Parsing: `backend/parser.py` (PDF → text), `backend/extractor.py` (LLM extraction)
- Storage: `backend/qdrant_store.py` (vector DB layer)
- Jobs: `backend/job_queue.py` (background processing with priorities)
- Surrogates: `backend/surrogate/` (GP models, acquisition functions)
