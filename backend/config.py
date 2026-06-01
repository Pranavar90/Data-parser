"""
config.py — Loads config.yaml and exposes typed constants.

Constants are updated in-place by reload(), which is called automatically
by save_config(). All other modules import via `import config as cfg` so
they always read the current value, not a frozen copy.
"""

from pathlib import Path
from typing import Any
import yaml

BASE_DIR = Path(__file__).parent
ROOT_DIR = BASE_DIR.parent
CONFIG_PATH = ROOT_DIR / "config.yaml"


def _load() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def _get(cfg: dict, path: str, default: Any = None) -> Any:
    keys = path.split(".")
    val = cfg
    for k in keys:
        if isinstance(val, dict) and k in val:
            val = val[k]
        else:
            return default
    return val


def get_all() -> dict:
    """Return full config as a nested dict (for the /api/config endpoint)."""
    cfg = _load()
    return {
        "ollama": {
            "base_url":   _get(cfg, "ollama.base_url",        "http://127.0.0.1:11434"),
            "model":      _get(cfg, "ollama.model",            "qwen2.5:3b-instruct-q4_K_S"),
            "timeout":    _get(cfg, "ollama.timeout",          300),
            "num_gpu":    _get(cfg, "ollama.num_gpu",          99),
            "num_ctx":    _get(cfg, "ollama.num_ctx",          8192),
            "keep_alive": _get(cfg, "ollama.keep_alive",       "15m"),
        },
        "parser": {
            "tds_max_chars":   _get(cfg, "parser.tds_max_chars",   7000),
            "paper_max_chars": _get(cfg, "parser.paper_max_chars", 12000),
            "chunk_size":      _get(cfg, "parser.chunk_size",      4000),
            "chunk_overlap":   _get(cfg, "parser.chunk_overlap",   300),
            "max_retries":     _get(cfg, "parser.max_retries",     1),
            "tds_bias":        _get(cfg, "parser.tds_bias",        2),
        },
        "output": {
            "json_indent":      _get(cfg, "output.json_indent",      2),
            "include_raw_text": _get(cfg, "output.include_raw_text", False),
        },
        "app": {
            "host": _get(cfg, "app.host", "127.0.0.1"),
            "port": _get(cfg, "app.port", 8000),
        },
    }


def save_config(new_cfg: dict) -> None:
    """Write updated config back to config.yaml, then reload module constants."""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(new_cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    reload()


def reload() -> None:
    """Re-read config.yaml and update all module-level constants in-place.
    Called automatically by save_config(). Safe to call externally.
    Because other modules do `import config as cfg` and access `cfg.X`,
    they always see the current value after reload().
    """
    global OLLAMA_BASE, LLM_MODEL, OLLAMA_TIMEOUT, NUM_GPU, NUM_CTX, KEEP_ALIVE
    global TDS_EXTRACT_CHARS, PAPER_EXTRACT_CHARS, CHUNK_SIZE, CHUNK_OVERLAP
    global MAX_RETRIES, TDS_BIAS, JSON_INDENT, INCLUDE_RAW_TEXT, APP_HOST, APP_PORT
    new = _load()
    OLLAMA_BASE         = _get(new, "ollama.base_url",        "http://127.0.0.1:11434")
    LLM_MODEL           = _get(new, "ollama.model",            "qwen2.5:3b-instruct-q4_K_S")
    OLLAMA_TIMEOUT      = _get(new, "ollama.timeout",          300)
    NUM_GPU             = _get(new, "ollama.num_gpu",          99)
    NUM_CTX             = _get(new, "ollama.num_ctx",          8192)
    KEEP_ALIVE          = _get(new, "ollama.keep_alive",       "15m")
    TDS_EXTRACT_CHARS   = _get(new, "parser.tds_max_chars",   7000)
    PAPER_EXTRACT_CHARS = _get(new, "parser.paper_max_chars", 12000)
    CHUNK_SIZE          = _get(new, "parser.chunk_size",       4000)
    CHUNK_OVERLAP       = _get(new, "parser.chunk_overlap",    300)
    MAX_RETRIES         = _get(new, "parser.max_retries",      1)
    TDS_BIAS            = _get(new, "parser.tds_bias",         2)
    JSON_INDENT         = _get(new, "output.json_indent",      2)
    INCLUDE_RAW_TEXT    = _get(new, "output.include_raw_text", False)
    APP_HOST            = _get(new, "app.host",                "127.0.0.1")
    APP_PORT            = _get(new, "app.port",                8000)


# ── Module-level constants (loaded at import; live-updated by reload()) ───────
_cfg = _load()

OLLAMA_BASE: str         = _get(_cfg, "ollama.base_url",        "http://127.0.0.1:11434")
LLM_MODEL: str           = _get(_cfg, "ollama.model",            "qwen2.5:3b-instruct-q4_K_S")
OLLAMA_TIMEOUT: int      = _get(_cfg, "ollama.timeout",          300)
NUM_GPU: int             = _get(_cfg, "ollama.num_gpu",          99)
NUM_CTX: int             = _get(_cfg, "ollama.num_ctx",          8192)
KEEP_ALIVE: str          = _get(_cfg, "ollama.keep_alive",       "15m")

TDS_EXTRACT_CHARS: int   = _get(_cfg, "parser.tds_max_chars",   7000)
PAPER_EXTRACT_CHARS: int = _get(_cfg, "parser.paper_max_chars", 12000)
CHUNK_SIZE: int          = _get(_cfg, "parser.chunk_size",       4000)
CHUNK_OVERLAP: int       = _get(_cfg, "parser.chunk_overlap",    300)
MAX_RETRIES: int         = _get(_cfg, "parser.max_retries",      1)
TDS_BIAS: int            = _get(_cfg, "parser.tds_bias",         2)

JSON_INDENT: int         = _get(_cfg, "output.json_indent",      2)
INCLUDE_RAW_TEXT: bool   = _get(_cfg, "output.include_raw_text", False)

APP_HOST: str            = _get(_cfg, "app.host",                "127.0.0.1")
APP_PORT: int            = _get(_cfg, "app.port",                8000)
