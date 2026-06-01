"""
extractor.py — LLM-based property extraction from TDS and research papers.
Adapted from Rlresearchassistant for standalone use.

Schema returned by extract_from_text():
  TDS:   {"document_type": "tds", "material_name": str, "product_description": str,
          "extraction_confidence": float, "applications": [...], "certifications": [...],
          "properties": [...], "processing_conditions": [...]}
  Paper: {"document_type": "paper", "material_name": str, "materials_studied": [...],
          "extraction_confidence": float, "research_objective": str, "methodology": str,
          "material_properties_mentioned": [...], "key_findings": [...],
          "limitations": [...], "conclusions": str, "applications": [...]}
"""

import hashlib
from typing import Any, Dict, List, Optional

import config as cfg
from cache import cached_llm_response, store_llm_response
from llm import get_client

# ── Prompts ───────────────────────────────────────────────────────────────────

SYSTEM_PROMPT_TDS = """\
You extract material properties from Technical Data Sheets.
Return ONLY valid JSON. No markdown, no explanation.

Output format:
{
  "material_name": "<product/grade name>",
  "product_description": "<one-sentence description of what this material is>",
  "extraction_confidence": <0.0-1.0>,
  "applications": ["<intended use case 1>", "<intended use case 2>"],
  "certifications": ["<e.g. UL94 V-0>", "<e.g. RoHS compliant>", "<e.g. ISO 9001>"],
  "properties": [
    {"name": "<property name>", "value": <number or string>, "unit": "<unit>", "confidence": <0.0-1.0>, "context": "<test standard or note>"}
  ],
  "processing_conditions": [
    {"name": "<condition>", "value": "<value>", "confidence": <0.0-1.0>}
  ]
}

Extract ALL numerical properties. This includes but is not limited to:

MECHANICAL: tensile strength, tensile modulus (Young's modulus), flexural strength, flexural modulus, elongation at break, elongation at yield, impact strength (Charpy/Izod), compressive strength, shear strength, hardness (Shore A/D/Rockwell), fatigue strength, interlaminar shear strength (ILSS), fracture toughness (KIC)

THERMAL: heat deflection temperature (HDT), Vicat softening point, glass transition temperature (Tg), melting point (Tm), coefficient of thermal expansion (CTE), thermal conductivity, specific heat capacity, flammability (UL94, LOI), melt flow index (MFI/MFR)

ELECTRICAL & EMI: volume resistivity, surface resistivity, dielectric constant (permittivity), dielectric strength, loss tangent (tan delta), electrical conductivity, EMI shielding effectiveness (SE), shielding effectiveness at frequency

PHYSICAL: density, water absorption, moisture uptake, shrinkage, colour, transparency/haze, refractive index, porosity, specific surface area (BET)

FILLER / COMPOSITE SPECIFIC: filler content (wt%, vol%, phr), fibre length, aspect ratio, fibre volume fraction, matrix/filler ratio, cure ratio, degree of cure, crosslink density

Rules:
- Extract ANY numerical value that has a unit — do not skip a property just because it is not in the list above
- value must be a number when the raw value is numeric
- Include test standard in context field (ISO 527, ASTM D638, IEC 61000, etc.)
- For ranges (e.g. 120-150 MPa) use the midpoint as value and note range in context
- Extract applications mentioned (e.g. "automotive", "electronics", "structural")
- Extract any certifications, compliance standards, or ratings mentioned
- Return empty arrays if nothing found, never omit keys"""

SYSTEM_PROMPT_PAPER = """\
You extract material properties and scientific findings from research papers.
Return ONLY valid JSON. No markdown, no explanation.

Output format:
{
  "extraction_confidence": <0.0-1.0>,
  "material_name": "<primary material or composite system studied>",
  "materials_studied": ["<all materials/composites/systems investigated>"],
  "research_objective": "<main goal of the study>",
  "methodology": "<experimental approach and characterisation techniques used>",
  "material_properties_mentioned": [
    {"property": "<name>", "value": <number or string>, "unit": "<unit>", "confidence": <0.0-1.0>, "context": "<sample conditions, filler loading, frequency, etc.>"}
  ],
  "key_findings": [
    {"finding": "<quantitative or qualitative finding>", "confidence": <0.0-1.0>}
  ],
  "limitations": [
    {"limitation": "<explicitly stated limitation or constraint of this study>", "confidence": <0.0-1.0>}
  ],
  "conclusions": "<overall conclusion of the study in 1-3 sentences>",
  "applications": ["<application domain or end-use mentioned>"]
}

Extract every quantitative property mentioned. This includes but is not limited to:

MECHANICAL: tensile strength, Young's modulus, flexural strength/modulus, elongation at break, impact strength, hardness, ILSS, fracture toughness (KIC), fatigue life

THERMAL: Tg, Tm, HDT, thermal conductivity, CTE, TGA onset temperature, char yield

ELECTRICAL & EMI: EMI shielding effectiveness (SE in dB — always include frequency if stated), electrical conductivity (S/m or S/cm), volume resistivity, dielectric constant, loss tangent

COMPOSITE & NANOCOMPOSITE SPECIFIC: filler loading (wt%, vol%, phr), dispersion quality, aspect ratio, percolation threshold, cure conditions, degree of cure

SURFACE & STRUCTURAL: BET surface area (m²/g), pore size, contact angle, roughness (Ra), XRD crystallinity %, d-spacing

Rules:
- Extract ANY quantitative measurement you find
- Always include conditions in context (e.g. "at 30 wt% filler", "measured at 1 GHz", "after annealing")
- For limitations: look for sentences containing "however", "limitation", "future work", "not investigated", "beyond the scope", "further study needed"
- For conclusions: summarise what the authors conclude, not just what they found
- Extract all application domains mentioned (EMI shielding, aerospace, biomedical, packaging, etc.)
- materials_studied should list every distinct material system tested
- Return empty arrays/strings if nothing found, never omit keys"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def detect_document_type(text: str) -> str:
    """Classify text as 'tds' (technical datasheet) or 'paper' (research article)."""
    lower = text.lower()

    tds_keywords = [
        "technical data sheet", "data sheet", "datasheet", "product description",
        "product name", "grade", "nominal", "typical properties", "typical value",
        "mechanical properties", "physical properties", "thermal properties",
        "electrical properties", "test method", "property value",
        "injection molding", "extrusion", "compression molding", "mold temperature",
        "melt temperature", "processing conditions", "drying conditions",
        "resin:hardener", "cure temperature", "post-cure", "pot life", "gel time",
        "mix ratio", "hardener ratio",
        "iso ", "astm ", "ul94", "iec ", "din ", "jis ",
        "conforms to", "complies with", "meets", "rated at",
        "tensile strength", "flexural modulus", "flexural strength",
        "elongation at break", "impact strength", "notched izod",
        "heat deflection", "vicat softening", "melt flow", "melt volume",
        "density", "shore hardness", "rockwell hardness",
        "emi shielding effectiveness", "shielding effectiveness (se)",
        "electrical conductivity", "thermal conductivity",
        "glass transition temperature", "tg ", "dielectric constant",
        "loss tangent", "tan δ", "filler loading", "vol%", "wt%", "phr",
        "aspect ratio", "bet surface area", "interlaminar shear", "ilss",
    ]

    paper_keywords = [
        "abstract", "introduction", "conclusion", "conclusions",
        "references", "bibliography", "doi:", "doi.org",
        "et al.", "figure ", "fig.", "table ", "equation ",
        "supplementary", "acknowledgement", "acknowledgment",
        "received:", "accepted:", "published:", "elsevier", "springer",
        "journal of", "polymer journal", "european polymer",
        "we investigated", "we report", "we fabricated", "we demonstrate",
        "results show", "results indicate", "this work", "in this study",
        "in this paper", "methodology", "sample preparation", "experimental",
        "experimental section", "characterization", "discussion", "synthesis route",
        "nanocomposite", "nanoparticle", "nanofiller", "nanosheet",
        "mxene", "graphene", "graphene oxide", "reduced graphene oxide",
        "carbon nanotube", "cnt ", "boron nitride", "hexagonal bn",
        "percolation", "percolation threshold", "agglomeration",
        "intercalation", "exfoliation", "polymer matrix", "epoxy matrix",
        "scanning electron microscopy", "sem ", "tem ", "xrd ", "ftir ",
        "differential scanning calorimetry", "dsc ", "tga ",
        "impedance spectroscopy", "vector network analyzer",
    ]

    tds_hits = sum(1 for w in tds_keywords if w in lower)
    paper_hits = sum(1 for w in paper_keywords if w in lower)
    return "tds" if (tds_hits + cfg.TDS_BIAS) >= paper_hits else "paper"


def _split_chunks(text: str) -> List[str]:
    if len(text) <= cfg.CHUNK_SIZE:
        return [text] if text.strip() else []
    chunks, start = [], 0
    while start < len(text):
        chunks.append(text[start: start + cfg.CHUNK_SIZE])
        start += cfg.CHUNK_SIZE - cfg.CHUNK_OVERLAP
    return chunks


def _empty_result(doc_type: str, error: str = "") -> Dict[str, Any]:
    return {
        "document_type": doc_type,
        "material_name": "",
        "extraction_confidence": 0.0,
        "error": error,
        # TDS fields
        "product_description": "",
        "applications": [],
        "certifications": [],
        "properties": [],
        "processing_conditions": [],
        # Paper fields
        "materials_studied": [],
        "research_objective": "",
        "methodology": "",
        "material_properties_mentioned": [],
        "key_findings": [],
        "limitations": [],
        "conclusions": "",
    }


def _merge(results: List[Dict], doc_type: str) -> Dict[str, Any]:
    merged = _empty_result(doc_type)
    seen: set = set()
    total_conf, n = 0.0, 0

    for r in results:
        if not isinstance(r, dict):
            continue
        n += 1
        total_conf += r.get("extraction_confidence", 0.5)

        if r.get("material_name") and not merged["material_name"]:
            merged["material_name"] = r["material_name"]

        if r.get("product_description") and not merged["product_description"]:
            merged["product_description"] = r["product_description"]

        if r.get("conclusions") and not merged["conclusions"]:
            merged["conclusions"] = r["conclusions"]

        if r.get("research_objective") and not merged["research_objective"]:
            merged["research_objective"] = r["research_objective"]

        if r.get("methodology") and not merged["methodology"]:
            merged["methodology"] = r["methodology"]

        # Deduplicate list fields
        for app in r.get("applications", []):
            if isinstance(app, str) and app and app not in merged["applications"]:
                merged["applications"].append(app)

        for cert in r.get("certifications", []):
            if isinstance(cert, str) and cert and cert not in merged["certifications"]:
                merged["certifications"].append(cert)

        for mat in r.get("materials_studied", []):
            if isinstance(mat, str) and mat and mat not in merged["materials_studied"]:
                merged["materials_studied"].append(mat)

        for p in r.get("properties", []):
            key = f"{p.get('name', '')}-{p.get('value', '')}"
            if key not in seen and p.get("name"):
                seen.add(key)
                merged["properties"].append(p)

        for c in r.get("processing_conditions", []):
            key = f"{c.get('name', '')}-{c.get('value', '')}"
            if key not in seen and c.get("name"):
                seen.add(key)
                merged["processing_conditions"].append(c)

        for p in r.get("material_properties_mentioned", []):
            key = f"{p.get('property', '')}-{p.get('value', '')}"
            if key not in seen and p.get("property"):
                seen.add(key)
                merged["material_properties_mentioned"].append(p)

        for kf in r.get("key_findings", []):
            if kf.get("finding"):
                merged["key_findings"].append(kf)

        for lim in r.get("limitations", []):
            if lim.get("limitation"):
                merged["limitations"].append(lim)

    merged["extraction_confidence"] = round(total_conf / n, 3) if n else 0.0
    merged["document_type"] = doc_type
    return merged


# ── Public API ────────────────────────────────────────────────────────────────

def extract_from_text(text: str, doc_type: Optional[str] = None) -> Dict[str, Any]:
    """
    Extract structured material data from raw text.
    Detects document type automatically if not provided.
    Results cached by content hash — re-running identical text returns instantly.
    All config values (chunk size, max chars, model) are read at call time so
    Settings changes take effect without a restart.
    """
    if not text or not text.strip():
        return _empty_result(doc_type or "paper", "Empty text")

    if not doc_type:
        doc_type = detect_document_type(text)

    max_chars = cfg.TDS_EXTRACT_CHARS if doc_type == "tds" else cfg.PAPER_EXTRACT_CHARS
    truncated = text[:max_chars]

    cache_key = hashlib.sha256(f"{doc_type}:{truncated}".encode()).hexdigest()
    cached = cached_llm_response(f"extract:{cache_key}")
    if cached is not None:
        print(f"[EXTRACTOR] Cache HIT ({doc_type}, {len(truncated)} chars)")
        return cached

    chunks = _split_chunks(truncated)
    if not chunks:
        return _empty_result(doc_type, "No processable text")

    print(f"[EXTRACTOR] {doc_type.upper()} | {len(text)} chars → {len(truncated)} chars | {len(chunks)} chunk(s)")

    system = SYSTEM_PROMPT_TDS if doc_type == "tds" else SYSTEM_PROMPT_PAPER
    client = get_client()
    results = []

    for i, chunk in enumerate(chunks):
        print(f"[EXTRACTOR] Chunk {i + 1}/{len(chunks)}...")
        parsed = None
        current = chunk
        for attempt in range(cfg.MAX_RETRIES + 1):
            if attempt > 0:
                current = current[: len(current) // 2]
            result = client.generate(
                model=cfg.LLM_MODEL,
                prompt=current,
                system=system,
                temperature=0.0,
                json_mode=True,
                use_cache=True,
            )
            if result and isinstance(result, dict) and "raw_text" not in result:
                parsed = result
                break
        if parsed:
            n = len(parsed.get("properties", [])) + len(parsed.get("material_properties_mentioned", []))
            print(f"[EXTRACTOR] Chunk {i + 1} OK — {n} props")
            results.append(parsed)
        else:
            print(f"[EXTRACTOR] Chunk {i + 1} FAILED")

    if not results:
        return _empty_result(doc_type, "All LLM calls failed — check Ollama is running")

    merged = _merge(results, doc_type)
    merged["chunks_processed"] = len(results)
    store_llm_response(f"extract:{cache_key}", merged)
    return merged
