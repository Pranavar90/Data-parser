# Planet Materials Labs — PDF Bulk Parser

A locally-run web application for extracting structured material property data from Technical Data Sheets (TDS) and scientific research papers at scale. All processing happens on your own machine — no data is sent to any cloud service.

---

## Table of Contents

1. [What it does](#what-it-does)
2. [How it works](#how-it-works)
3. [What is Ollama?](#what-is-ollama)
4. [Prerequisites](#prerequisites)
5. [Installation](#installation)
6. [Running the app](#running-the-app)
7. [Using the platform](#using-the-platform)
8. [Output format](#output-format)
9. [Folder structure preservation](#folder-structure-preservation)
10. [Configuration](#configuration)
11. [Choosing a model](#choosing-a-model)
12. [Troubleshooting](#troubleshooting)
13. [Project structure](#project-structure)

---

## What it does

- Accepts individual PDF files, multiple files, entire folders, and arbitrarily nested subfolder structures
- Automatically detects whether each PDF is a Technical Data Sheet or a research paper
- Extracts every quantitative material property, finding, limitation, certification, and application using a local LLM
- Outputs one structured JSON file per PDF, preserving the original folder hierarchy
- Streams real-time per-file progress to the browser while parsing runs

---

## How it works

```
PDF files  →  Text extraction (pdfplumber / PyMuPDF)
           →  Document classification (TDS vs. paper)
           →  Chunked LLM inference via Ollama (local)
           →  Structured JSON output
```

The parser uses **Ollama**, a tool that lets you run large language models (LLMs) entirely on your own computer. The LLM reads the text from each PDF and returns a structured JSON object containing all the material data it can identify.

---

## What is Ollama?

Ollama is a free, open-source tool that allows you to download and run LLMs (like Llama, Qwen, Gemma, Mistral, and many others) locally on your machine without an internet connection, without API keys, and without sending any data to external servers.

Think of it like a local version of ChatGPT that runs inside your own computer, accessible via a simple HTTP API.

- **Website:** https://ollama.com
- **Model library:** https://ollama.com/library (browse available models)
- Ollama runs as a background service on your machine and exposes an API at `http://127.0.0.1:11434`
- The parser sends each PDF's text to this API and gets structured data back

---

## Prerequisites

### 1. Python 3.10 or later

Download from [python.org](https://www.python.org/downloads/) and install.

Verify by opening a terminal and running:
```bash
python --version
```
You should see `Python 3.10.x` or higher. On some systems you may need `python3`:
```bash
python3 --version
```

---

### 2. Ollama

#### Install Ollama

**Windows / macOS:**
Go to [https://ollama.com](https://ollama.com), click **Download**, and run the installer. On Windows, Ollama installs as a background service and starts automatically.

**Linux:**
```bash
curl -fsSL https://ollama.com/install.sh | sh
```

Verify the installation:
```bash
ollama --version
```

---

#### Start the Ollama server

On Windows and macOS, Ollama starts automatically after installation. If it is not running, start it manually:
```bash
ollama serve
```

Leave this terminal open. Ollama must be running in the background whenever you use the parser.

To check that it is running, open a browser and go to:
```
http://127.0.0.1:11434
```
You should see: `Ollama is running`

---

#### Pull a model

Before the parser can work, you need to download at least one LLM. Run:
```bash
ollama pull qwen2.5:3b-instruct-q4_K_S
```

This downloads the default extraction model (~2 GB). The download happens once and the model is stored locally.

To verify the model is available:
```bash
ollama list
```
You should see `qwen2.5:3b-instruct-q4_K_S` in the list.

---

#### Ollama quick reference

| Command | What it does |
|---|---|
| `ollama serve` | Start the Ollama server (if not already running) |
| `ollama list` | Show all downloaded models |
| `ollama pull <model>` | Download a model |
| `ollama rm <model>` | Delete a model |
| `ollama run <model>` | Chat with a model interactively in the terminal |
| `ollama ps` | Show models currently loaded in memory |

---

## Installation

### Step 1 — Get the project

If you received the project as a ZIP file, extract it to a folder on your machine (e.g. `C:\tools\jsonparser` on Windows or `~/tools/jsonparser` on Mac/Linux).

If you are cloning from a repository:
```bash
git clone <repository-url>
cd jsonparser
```

---

### Step 2 — Install Python dependencies

Open a terminal in the project root folder (the folder that contains `run.py`) and run:
```bash
pip install -r requirements.txt
```

On some systems, use `pip3`:
```bash
pip3 install -r requirements.txt
```

This installs FastAPI, the PDF parsing libraries (pdfplumber, PyMuPDF), and other dependencies.

> **Tip:** If you want to keep the project's dependencies isolated from your system Python, create a virtual environment first:
> ```bash
> python -m venv venv
> # On Windows:
> venv\Scripts\activate
> # On Mac/Linux:
> source venv/bin/activate
> ```
> Then run `pip install -r requirements.txt` inside the activated environment.

---

## Running the app

Make sure Ollama is running (check `http://127.0.0.1:11434` in a browser), then start the server:

```bash
python run.py
```

You will see:
```
==========================================================
  Planet Materials Labs — PDF Bulk Parser
==========================================================
  URL    →  http://127.0.0.1:8000
  Model  →  qwen2.5:3b-instruct-q4_K_S
  Ollama →  http://127.0.0.1:11434
==========================================================
  Press Ctrl+C to stop
```

Open your browser and go to **http://127.0.0.1:8000**

Press `Ctrl+C` in the terminal to stop the server when you are done.

---

## Using the platform

### Status indicator

The top-right corner of the app shows an Ollama status badge:
- **Green dot** — Ollama is running and the model is ready. You can start parsing.
- **Red dot / Offline** — Ollama is not running. Start it with `ollama serve` in a terminal, then wait a few seconds for the badge to turn green.

---

### Step 1 — Add your PDF files

You have two options:

**Drag and Drop**
Drag PDF files or entire folders directly onto the drop zone. You can drag multiple folders at once. The app recursively finds all PDFs inside nested subfolders.

**Browse**
Click **Browse Files** to select individual PDF files, or **Browse Folder** to select an entire folder (all PDFs inside will be found, including subfolders).

The left panel shows a queue of all added files. The total count updates as you add more.

---

### Step 2 — Parse

Click **Parse X Files** in the queue panel to start.

The app switches to the parsing view, which shows:
- The current file being processed
- An overall progress bar with percentage
- A live log on the right showing each file's result as it completes (green tick = success, red cross = failed)

Each file is processed one at a time. The LLM is called once per file (or multiple times if the document is very long and needs to be split into chunks).

**To stop early:** Click the **Terminate** button in the top bar. The job will abort and you will return to the idle state.

---

### Step 3 — Review results

When all files are done, the app shows a results grid — one card per PDF. Each card displays:
- The filename and detected document type (TDS or Paper)
- The identified material name
- Extraction confidence score (colour-coded: green = high, amber = medium, red = low)
- Number of properties extracted

Click any card to open the **detail view**, which has tabs for:

| Tab | What you'll find |
|---|---|
| **Properties** | All extracted material properties — name, value, unit, confidence, test standard |
| **Processing** (TDS only) | Processing conditions such as mold temperature, cure time, drying conditions |
| **Applications** (TDS only) | Application domains, certifications, compliance standards, product description |
| **Overview** (Paper only) | Research objective, materials studied, conclusions, application domains |
| **Methodology** (Paper only) | Experimental approach and characterisation techniques |
| **Findings** (Paper only) | Key findings with individual confidence scores |
| **Limitations** (Paper only) | Explicitly stated study limitations |
| **Raw JSON** | The complete extracted JSON for this document |

---

### Step 4 — Export

After parsing completes, two export options appear in the results toolbar:

**Download ZIP**
Downloads a single ZIP file containing all extracted JSONs with the original folder structure preserved inside the archive. This is the easiest way to transfer results.

**Save to Folder**
Type an absolute folder path into the text box (e.g. `C:\Users\YourName\Documents\parsed_output`) and click **Save**. The app writes the JSON files directly to that location on your machine, preserving the original folder structure.

---

### Step 5 — Start a new batch

Click **New Batch** to clear the results and return to the idle state, ready to process another set of files.

---

## Output format

Each PDF produces one `.json` file. The fields present depend on the document type.

### Technical Data Sheet (TDS)

```json
{
  "doc_id": "a3f8c2e1-...",
  "filename": "material_x_tds.pdf",
  "doc_type": "tds",
  "material_name": "PEEK-CF30",
  "extraction_confidence": 0.91,
  "properties_count": 18,
  "product_description": "Carbon fibre reinforced PEEK compound for structural and tribological applications.",
  "applications": ["aerospace", "medical devices", "automotive"],
  "certifications": ["UL94 V-0", "RoHS compliant", "ISO 9001"],
  "properties": [
    {
      "property_name": "Tensile Strength",
      "value": 210,
      "unit": "MPa",
      "confidence": 0.97,
      "context": "ISO 527-1"
    },
    {
      "property_name": "Glass Transition Temperature",
      "value": 143,
      "unit": "°C",
      "confidence": 0.94,
      "context": "DSC, 10°C/min"
    }
  ],
  "processing_conditions": [
    {
      "property_name": "Melt Temperature",
      "value": "370-400",
      "unit": "°C",
      "confidence": 0.90,
      "context": ""
    }
  ],
  "source_text": "..."
}
```

### Research Paper

```json
{
  "doc_id": "b9d1e7f2-...",
  "filename": "mxene_emi_shielding.pdf",
  "doc_type": "paper",
  "material_name": "MXene/Epoxy Nanocomposite",
  "extraction_confidence": 0.85,
  "properties_count": 12,
  "research_objective": "Investigate the EMI shielding effectiveness of Ti3C2Tx MXene/epoxy composites at varying filler loadings.",
  "materials_studied": ["Ti3C2Tx MXene", "Epoxy resin", "MXene/Epoxy composite"],
  "methodology": "MXene was synthesised by selective etching of Ti3AlC2. Composites were prepared by solution mixing. SE measured by VNA in the X-band (8.2–12.4 GHz).",
  "properties": [
    {
      "property_name": "EMI Shielding Effectiveness",
      "value": 42.3,
      "unit": "dB",
      "confidence": 0.93,
      "context": "at 30 wt% MXene, measured at 10 GHz"
    }
  ],
  "key_findings": [
    {
      "finding": "SE increased from 8.1 dB to 42.3 dB as MXene loading increased from 5 wt% to 30 wt%.",
      "confidence": 0.95
    }
  ],
  "limitations": [
    {
      "limitation": "Measurements were limited to the X-band; performance at lower frequencies was not investigated.",
      "confidence": 0.88
    }
  ],
  "conclusions": "Ti3C2Tx MXene/epoxy composites achieve commercial-grade EMI shielding at loadings above 20 wt%, making them viable for lightweight electronic enclosures.",
  "applications": ["EMI shielding", "electronics", "aerospace"],
  "source_text": "..."
}
```

---

## Folder structure preservation

The original folder structure is always preserved in the output.

```
Input:
  project_A/
    tds/
      peek_cf30.pdf
      pa66_gf30.pdf
    papers/
      mxene_emi_2024.pdf
      graphene_thermal.pdf

Output (ZIP or saved folder):
  project_A/
    tds/
      peek_cf30.json
      pa66_gf30.json
    papers/
      mxene_emi_2024.json
      graphene_thermal.json
```

---

## Configuration

All settings can be changed in two ways:
1. Click the **Settings** button (gear icon) in the top-right of the app — changes take effect immediately without a restart
2. Edit `config.yaml` in the project root folder directly, then restart the app

```yaml
ollama:
  base_url: http://127.0.0.1:11434   # Ollama server address
  model: qwen2.5:3b-instruct-q4_K_S  # LLM to use for extraction
  timeout: 300                         # Seconds to wait per LLM call
  num_ctx: 8192                        # Context window size (tokens)
  keep_alive: 15m                      # How long to keep model in memory

parser:
  tds_max_chars: 7000                  # Max characters extracted from TDS docs
  paper_max_chars: 12000               # Max characters extracted from papers
  chunk_size: 4000                     # Characters per LLM chunk
  chunk_overlap: 300                   # Overlap between chunks
  max_retries: 1                       # Retry attempts if LLM returns invalid JSON
  tds_bias: 2                          # Scoring bias toward TDS classification

output:
  json_indent: 2                       # JSON indentation (0 = compact, 2 = readable)

app:
  host: 127.0.0.1
  port: 8000
```

---

## Choosing a model

The parser works with any model available in Ollama. Larger models produce better extractions but are slower and require more RAM.

### Recommended models

| Model | Pull command | RAM needed | Speed | Quality |
|---|---|---|---|---|
| `qwen2.5:3b-instruct-q4_K_S` | `ollama pull qwen2.5:3b-instruct-q4_K_S` | ~3 GB | Fast | Good (default) |
| `qwen2.5:7b-instruct` | `ollama pull qwen2.5:7b` | ~5 GB | Medium | Better |
| `qwen2.5:14b-instruct` | `ollama pull qwen2.5:14b` | ~9 GB | Slow | Best |
| `gemma3:4b` | `ollama pull gemma3:4b` | ~4 GB | Fast | Good |
| `llama3.1:8b` | `ollama pull llama3.1:8b` | ~6 GB | Medium | Good |

### Switching models

1. Pull the new model: `ollama pull <model-name>`
2. In the app, open **Settings** → select the model from the dropdown → click **Save Settings**
   — or —
   Edit `ollama.model` in `config.yaml` and restart the app

> **Note:** The model selector in Settings is populated from `ollama list`. If a model does not appear, make sure you have pulled it first.

### GPU acceleration

If your machine has an NVIDIA or AMD GPU, Ollama will use it automatically (dramatically faster). On Apple Silicon Macs, the GPU is always used. No additional setup is required.

To check if your GPU is being used:
```bash
ollama ps
```
When a model is loaded, the output will show how much is loaded onto the GPU vs. CPU.

---

## Troubleshooting

### "Ollama Offline" badge in the app

Ollama is not running. Fix:
```bash
ollama serve
```
Wait a few seconds, then the badge in the app will turn green automatically.

---

### Parsing takes a very long time

- The 3b model is the fastest. Use it unless extraction quality is insufficient.
- Check whether your GPU is being used: `ollama ps`. If everything is on CPU, extraction will be significantly slower.
- Very long PDFs (papers with 20+ pages) take longer because the text is split into multiple chunks, each requiring a separate LLM call.
- Increase `ollama.timeout` in Settings if you are getting timeout errors on large files.

---

### Low extraction confidence scores

- Try a larger model (7b or 14b) — larger models follow the JSON schema more accurately and extract more data.
- Reduce `parser.chunk_size` to `2000` in Settings for better per-chunk focus.
- Scanned PDFs (images of pages rather than real text) will always produce poor results because there is no readable text to extract from. These require OCR pre-processing before the parser can help.
- If a PDF is password-protected, the parser cannot read it.

---

### "All LLM calls failed" in the output JSON

This means the model returned something that was not valid JSON (or returned nothing). Causes:
- The model is not suitable for structured extraction — try `qwen2.5:3b-instruct-q4_K_S` or another instruct-tuned model.
- The file has almost no readable text (scanned image PDF).
- Ollama ran out of memory — try a smaller model or reduce `parser.chunk_size`.

---

### Port 8000 already in use

Change the port in `config.yaml`:
```yaml
app:
  port: 8080
```
Then open `http://127.0.0.1:8080` in your browser.

---

### Python dependency errors on install

If `pip install -r requirements.txt` fails:
- Make sure you are running Python 3.10 or later: `python --version`
- On Windows, try running the terminal as Administrator
- On Mac/Linux with permission errors: `pip install --user -r requirements.txt`
- If `pymupdf` fails to install, try: `pip install pymupdf --no-binary pymupdf`

---

### The app opens but shows a blank page

- Make sure the server is still running in the terminal (you should see no errors)
- Try a hard refresh: `Ctrl+Shift+R` (Windows/Linux) or `Cmd+Shift+R` (Mac)
- Check the terminal for Python error messages

---

## Project structure

```
jsonparser/
├── backend/
│   ├── main.py          FastAPI server — all HTTP endpoints and job management
│   ├── parser.py        PDF text extraction using pdfplumber (primary) and PyMuPDF (fallback)
│   ├── extractor.py     LLM-based extraction logic — prompts, chunking, merging
│   ├── llm.py           Ollama HTTP client with connection pooling and health checks
│   ├── cache.py         SHA256-keyed in-memory LLM response cache
│   └── config.py        Config loader — reads config.yaml, exposes typed values
├── static/
│   └── logo.jpg         Company logo (served at /static/logo.jpg)
├── templates/
│   └── index.html       Single-page web UI — all HTML, CSS, and JavaScript
├── temp/                Temporary upload directory (auto-cleaned after each job)
├── config.yaml          User-editable configuration file
├── requirements.txt     Python package dependencies
└── run.py               Entry point — run this to start the server
```

---

## How the LLM extraction works (brief technical overview)

For those who want to understand what is happening under the hood:

1. **Text extraction** — `pdfplumber` extracts text and tables page by page. If that fails (e.g. corrupted PDF), `PyMuPDF` is used as a fallback.

2. **Document classification** — The extracted text is scored against two keyword lists (TDS keywords vs. paper keywords). The type with the higher score wins. A small bias (+2) is applied toward TDS to reduce misclassification of manufacturer documents.

3. **Chunking** — The text is split into overlapping chunks (default: 4000 chars per chunk, 300-char overlap) so that large documents can be processed even if the model has a limited context window.

4. **LLM inference** — Each chunk is sent to Ollama with a detailed system prompt instructing the model to return only valid JSON in a specific schema. Temperature is set to 0 for deterministic output.

5. **Merging** — Results from multiple chunks are merged: scalar fields (material name, conclusions, etc.) take the first non-empty value; array fields (properties, findings, limitations) are deduplicated and combined.

6. **Caching** — Responses are cached by a SHA256 hash of the document type + text content. Re-running the same file is instant.

---

## Support

Open an issue in the project repository or contact the team lead if you encounter a problem not covered above.
