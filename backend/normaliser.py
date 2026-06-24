"""
normaliser.py — Canonical property name resolution for material properties.

Four-stage lookup:
  0. Translation: detect non-English property names, translate via LLM
  1. Exact alias match (O(1) dict lookup)
  2. Fuzzy match via rapidfuzz (token_sort_ratio >= 88)
  3. Novel entry (auto-created, persisted to registry)

The canonical registry is loaded once per process and persisted back
whenever a fuzzy match adds an alias or a novel entry is created.
"""

import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional

import litellm
from rapidfuzz import fuzz

import config as cfg

logger = logging.getLogger(__name__)

# ── Translation cache (in-memory, avoids repeated LLM calls) ─────────────────

_translation_cache: Dict[str, str] = {}
_translation_lock = threading.Lock()


def _is_non_english(name: str) -> bool:
    """Heuristic: if >30% of alpha chars are outside ASCII, likely non-English."""
    alpha = [c for c in name if c.isalpha()]
    if not alpha:
        return False
    non_ascii = sum(1 for c in alpha if ord(c) > 127)
    return non_ascii / len(alpha) > 0.3


def _translate_property_name(raw_name: str) -> str:
    """Translate a non-English property name to English via LLM. Returns English name."""
    with _translation_lock:
        cached = _translation_cache.get(raw_name)
    if cached is not None:
        return cached

    try:
        resp = litellm.completion(
            model=cfg.LLM_MODEL,
            messages=[
                {"role": "system", "content": "You are a materials science translator. Translate the given material property name to English. Return ONLY the English property name, nothing else. No explanation."},
                {"role": "user", "content": raw_name},
            ],
            max_tokens=30,
            temperature=0.0,
        )
        translated = (resp.choices[0].message.content or "").strip()
        # Clean up: remove quotes, periods, extra whitespace
        translated = re.sub(r'^["\']|["\']$', '', translated).strip()
        if translated:
            logger.info("Translated: '%s' -> '%s'", raw_name, translated)
            with _translation_lock:
                _translation_cache[raw_name] = translated
            return translated
    except Exception as e:
        logger.warning("Translation failed for '%s': %s", raw_name, e)

    return raw_name  # fallback: return original

_REGISTRY_PATH = Path(__file__).parent / "canonical_registry.json"
_FUZZY_THRESHOLD = 88


class PropertyNormaliser:
    def __init__(self, registry_path: Path = _REGISTRY_PATH):
        self._path = registry_path
        self._lock = threading.Lock()
        self._registry: Dict[str, Dict[str, Any]] = {}
        self._alias_map: Dict[str, str] = {}  # lowercased alias/name → canonical name
        self._novel_counter = 0
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            with open(self._path, "r", encoding="utf-8") as f:
                self._registry = json.load(f)
        else:
            self._registry = {}
        self._rebuild_alias_map()
        # Set novel counter from existing novel entries
        for entry in self._registry.values():
            cid = entry.get("canonical_id", "")
            if cid.startswith("novel_"):
                try:
                    num = int(cid.split("_", 1)[1])
                    self._novel_counter = max(self._novel_counter, num)
                except ValueError:
                    pass

    def _rebuild_alias_map(self) -> None:
        self._alias_map = {}
        for canonical_name, entry in self._registry.items():
            self._alias_map[canonical_name.lower()] = canonical_name
            for alias in entry.get("aliases", []):
                self._alias_map[alias.lower()] = canonical_name

    def _persist(self) -> None:
        """Atomic write: write to temp file then rename."""
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._path.parent), suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._registry, f, indent=2, ensure_ascii=False)
            # On Windows, remove target first if it exists
            if self._path.exists():
                self._path.unlink()
            Path(tmp_path).rename(self._path)
        except Exception as e:
            logger.error("Failed to persist canonical registry: %s", e)

    def normalise(self, raw_name: str) -> Dict[str, str]:
        if not raw_name or not raw_name.strip():
            return {
                "property_name": raw_name,
                "raw_name": raw_name,
                "canonical_id": "",
                "category": "unknown",
                "unit_family": "unknown",
                "normalisation_method": "novel",
            }

        lookup_name = raw_name.strip()
        key = lookup_name.lower()

        # Stage 0: check alias first, then translate if no match
        with self._lock:
            quick_match = self._alias_map.get(key)
        if quick_match is None:
            # Not in registry — might be non-English. Try translating.
            translated = _translate_property_name(lookup_name)
            if translated.lower() != key:
                lookup_name = translated
                key = lookup_name.lower()

        # Stage 1: exact alias match
        with self._lock:
            canonical = self._alias_map.get(key)
        if canonical is not None:
            entry = self._registry[canonical]
            # Auto-add original foreign name as alias if it was translated
            if raw_name.strip() != lookup_name:
                self._add_alias(canonical, raw_name.strip())
            return {
                "property_name": canonical,
                "raw_name": raw_name,
                "canonical_id": entry["canonical_id"],
                "category": entry["category"],
                "unit_family": entry["unit_family"],
                "normalisation_method": "alias",
            }

        # Stage 2: fuzzy match (against canonical names)
        best_score = 0.0
        best_canonical: Optional[str] = None
        with self._lock:
            names = list(self._registry.keys())
        for name in names:
            score = fuzz.token_sort_ratio(key, name.lower())
            if score > best_score:
                best_score = score
                best_canonical = name

        if best_score >= _FUZZY_THRESHOLD and best_canonical is not None:
            with self._lock:
                entry = self._registry[best_canonical]
                # Add translated name as alias
                if lookup_name not in entry["aliases"]:
                    entry["aliases"].append(lookup_name)
                    self._alias_map[key] = best_canonical
                # Also add original foreign name as alias
                if raw_name.strip() != lookup_name and raw_name.strip() not in entry["aliases"]:
                    entry["aliases"].append(raw_name.strip())
                    self._alias_map[raw_name.strip().lower()] = best_canonical
                self._persist()
            logger.warning(
                "Fuzzy match: '%s' -> '%s' (score=%.1f)",
                lookup_name, best_canonical, best_score,
            )
            return {
                "property_name": best_canonical,
                "raw_name": raw_name,
                "canonical_id": entry["canonical_id"],
                "category": entry["category"],
                "unit_family": entry["unit_family"],
                "normalisation_method": "fuzzy",
            }

        # Stage 3: novel entry
        with self._lock:
            self._novel_counter += 1
            canonical_name = lookup_name.strip().title()
            # Avoid collision with existing canonical names
            if canonical_name in self._registry:
                canonical_name = f"{canonical_name} ({self._novel_counter})"
            canonical_id = f"novel_{self._novel_counter:04d}"
            aliases = [lookup_name]
            # Also store original foreign name
            if raw_name.strip() != lookup_name:
                aliases.append(raw_name.strip())
            self._registry[canonical_name] = {
                "aliases": aliases,
                "category": "unknown",
                "unit_family": "unknown",
                "canonical_id": canonical_id,
            }
            self._alias_map[key] = canonical_name
            self._alias_map[canonical_name.lower()] = canonical_name
            if raw_name.strip() != lookup_name:
                self._alias_map[raw_name.strip().lower()] = canonical_name
            self._persist()

        logger.info("Novel property registered: '%s' -> '%s' (%s)", lookup_name, canonical_name, canonical_id)
        return {
            "property_name": canonical_name,
            "raw_name": raw_name,
            "canonical_id": canonical_id,
            "category": "unknown",
            "unit_family": "unknown",
            "normalisation_method": "novel",
        }

    def _add_alias(self, canonical_name: str, alias: str) -> None:
        """Add an alias to an existing canonical entry if not already present."""
        with self._lock:
            entry = self._registry.get(canonical_name)
            if entry and alias not in entry["aliases"]:
                entry["aliases"].append(alias)
                self._alias_map[alias.lower()] = canonical_name
                self._persist()


# ── Module-level singleton ────────────────────────────────────────────────────

_instance: Optional[PropertyNormaliser] = None
_init_lock = threading.Lock()


def _get_instance() -> PropertyNormaliser:
    global _instance
    if _instance is None:
        with _init_lock:
            if _instance is None:
                _instance = PropertyNormaliser()
    return _instance


def normalise_property_name(raw_name: str) -> Dict[str, str]:
    """Public API — resolve a raw property name to its canonical form."""
    return _get_instance().normalise(raw_name)
