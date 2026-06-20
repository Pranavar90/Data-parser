"""
llm.py — Ollama client with connection pooling and response caching.
Adapted from Rlresearchassistant for standalone use.

Config values (URL, timeout, model options) are read from the config module
at call time so that saving Settings takes effect without a restart.
"""

import hashlib
import json
import logging
import re
import threading
from typing import Any, Dict, List, Optional

import httpx

import config as cfg
from cache import InMemoryCache, cached_llm_response, store_llm_response

logger = logging.getLogger(__name__)

# Short TTL cache for health/model-list results (auto-expires every 30s)
_model_list_cache = InMemoryCache(maxsize=4, ttl=30)


class OllamaClient:
    def __init__(self):
        # Large client-level timeout; individual calls override with cfg values.
        self.client = httpx.Client(
            timeout=600.0,
            limits=httpx.Limits(
                max_keepalive_connections=5,
                max_connections=10,
                keepalive_expiry=120.0,
            ),
        )

    def is_running(self) -> bool:
        cached = _model_list_cache.get("ollama_running")
        if cached is not None:
            return cached
        base = cfg.OLLAMA_BASE.rstrip("/")
        try:
            r = self.client.get(f"{base}/api/tags", timeout=5.0)
            ok = r.status_code == 200
        except Exception:
            ok = False
        # Only cache success — failures are re-checked on every poll.
        if ok:
            _model_list_cache.set("ollama_running", ok)
        return ok

    def list_models(self) -> List[str]:
        cached = _model_list_cache.get("models")
        if cached is not None:
            return cached
        base = cfg.OLLAMA_BASE.rstrip("/")
        try:
            r = self.client.get(f"{base}/api/tags", timeout=5.0)
            if r.status_code == 200:
                models = [m["name"] for m in r.json().get("models", [])]
                _model_list_cache.set("models", models)
                return models
        except Exception:
            pass
        return []

    def generate(
        self,
        model: str,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.0,
        json_mode: bool = True,
        use_cache: bool = True,
    ) -> Optional[Dict[str, Any]]:
        cache_key = None
        if use_cache and temperature == 0.0 and json_mode:
            cache_key = hashlib.sha256(f"{model}:{system}:{prompt}".encode()).hexdigest()
            cached = cached_llm_response(cache_key)
            if cached is not None:
                return cached

        # Read Ollama options from config at call time so Settings changes
        # take effect immediately without recreating the singleton.
        payload: Dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_gpu": cfg.NUM_GPU,
                "num_ctx": cfg.NUM_CTX,
            },
            "keep_alive": cfg.KEEP_ALIVE,
        }
        if system:
            payload["system"] = system
        if json_mode:
            payload["format"] = "json"

        base = cfg.OLLAMA_BASE.rstrip("/")
        try:
            r = self.client.post(
                f"{base}/api/generate",
                json=payload,
                timeout=float(cfg.OLLAMA_TIMEOUT),
            )
            if r.status_code == 200:
                data = r.json()
                if json_mode and "response" in data:
                    parsed = _extract_json(data["response"])
                    result = parsed if parsed is not None else {"raw_text": data["response"]}
                    if cache_key and result and "raw_text" not in result:
                        store_llm_response(cache_key, result)
                    return result
                return data
        except Exception as e:
            logger.error("Generation error: %s", e)
        return None

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass


# ── Singleton ─────────────────────────────────────────────────────────────────

_client_instance: Optional[OllamaClient] = None
_client_lock = threading.Lock()


def get_client() -> OllamaClient:
    global _client_instance
    if _client_instance is None:
        with _client_lock:
            if _client_instance is None:
                _client_instance = OllamaClient()
    return _client_instance


def shutdown_client() -> None:
    global _client_instance
    if _client_instance is not None:
        _client_instance.close()
        _client_instance = None


def invalidate_health_cache() -> None:
    """Clear cached health/model-list results (call after config URL change)."""
    _model_list_cache.clear()


# ── JSON extraction helper ────────────────────────────────────────────────────

def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    text = text.strip()

    # Markdown code block
    for marker in ("```json", "```"):
        if marker in text:
            try:
                inner = text.split(marker, 1)[1].split("```")[0].strip()
                return json.loads(inner)
            except Exception:
                pass

    # Direct JSON
    if text.startswith("{") and text.endswith("}"):
        try:
            return json.loads(text)
        except Exception:
            pass

    # Regex fallback
    try:
        m = re.search(r"(\{.*\})", text, re.DOTALL)
        if m:
            return json.loads(m.group(1))
    except Exception:
        pass

    return None
