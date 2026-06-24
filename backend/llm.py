"""
llm.py — LLM client via LiteLLM SDK (routes to any provider automatically).

LiteLLM reads API keys from environment (OPENAI_API_KEY, etc.).
Config values (model, timeout) are read from the config module
at call time so that saving Settings takes effect without a restart.
"""

import hashlib
import json
import logging
import re
import threading
from typing import Any, Dict, List, Optional

import litellm

import config as cfg
from cache import InMemoryCache, cached_llm_response, store_llm_response

logger = logging.getLogger(__name__)

# Short TTL cache for health/model-list results (auto-expires every 30s)
_model_list_cache = InMemoryCache(maxsize=4, ttl=30)


class LLMClient:

    def is_running(self) -> bool:
        # No LLM call — just check that model and API key are configured.
        # Actual connectivity is verified on the first real parse job.
        import os
        has_model = bool(cfg.LLM_MODEL)
        has_key = bool(os.environ.get("OPENAI_API_KEY"))
        return has_model and has_key

    def list_models(self) -> List[str]:
        cached = _model_list_cache.get("models")
        if cached is not None:
            return cached
        # LiteLLM doesn't have a universal model list endpoint —
        # return the configured model as the available model.
        models = [cfg.LLM_MODEL]
        _model_list_cache.set("models", models)
        return models

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

        messages: list = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            resp = litellm.completion(**kwargs)
            text = resp.choices[0].message.content or ""

            if json_mode:
                parsed = _extract_json(text)
                result = parsed if parsed is not None else {"raw_text": text}
                if cache_key and result and "raw_text" not in result:
                    store_llm_response(cache_key, result)
                return result
            return {"raw_text": text}
        except Exception as e:
            logger.error("Generation error: %s", e)
        return None

    def close(self) -> None:
        pass


# ── Singleton ─────────────────────────────────────────────────────────────────

_client_instance: Optional[LLMClient] = None
_client_lock = threading.Lock()


def get_client() -> LLMClient:
    global _client_instance
    if _client_instance is None:
        with _client_lock:
            if _client_instance is None:
                _client_instance = LLMClient()
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
