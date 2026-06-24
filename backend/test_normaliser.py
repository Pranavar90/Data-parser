"""test_normaliser.py — Validate normaliser against 25 sample property names."""

import sys
import io

# Handle Windows console encoding
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from rapidfuzz import fuzz
from normaliser import normalise_property_name, _get_instance

# Force reload so any registry edits are picked up
inst = _get_instance()
inst._load()

RAW_NAMES = [
    "UTS", "tensile strength (MPa)", "σ_UTS", "Tg", "glass transition temp",
    "Tg (DSC)", "EMI SE", "shielding effectiveness", "MFI", "melt flow index (g/10min)",
    "HDT", "heat deflection temp", "E-modulus", "Young's Modulus", "rho",
    "bulk density", "CTE", "thermal expansion coefficient",
    "peel strength", "lap shear strength",
    # Fuzzy test cases — misspelled / abbreviated, NOT in aliases
    "tensille strength", "glass transition temp.", "electical conductivity",
    "compresive strength", "thermal conductivty",
]

counts = {"alias": 0, "fuzzy": 0, "novel": 0}

print(f"{'Raw Name':<35} {'Canonical Name':<35} {'ID':<12} {'Method':<8} {'Score'}")
print("-" * 105)

for raw in RAW_NAMES:
    r = normalise_property_name(raw)
    counts[r["normalisation_method"]] += 1

    # Show fuzzy score for fuzzy matches
    score_str = ""
    if r["normalisation_method"] == "fuzzy":
        score = fuzz.token_sort_ratio(raw.lower(), r["property_name"].lower())
        score_str = f"{score:.1f}"

    print(f"{r['raw_name']:<35} {r['property_name']:<35} {r['canonical_id']:<12} {r['normalisation_method']:<8} {score_str}")

print("-" * 105)
print(f"\nResolution summary: alias={counts['alias']}  fuzzy={counts['fuzzy']}  novel={counts['novel']}")
print(f"Total canonical entries in registry: {len(inst._registry)}")
