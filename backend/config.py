"""
config.py — Loads config.yaml and exposes typed constants.

Constants are updated in-place by reload(), which is called automatically
by save_config(). All other modules import via `import config as cfg` so
they always read the current value, not a frozen copy.
"""

import os
from pathlib import Path
from typing import Any
import yaml

BASE_DIR = Path(__file__).parent
# In Docker, backend/*.py are in /app/ directly, so .parent = /.
# Detect by checking if config.yaml exists at parent level.
_candidate = BASE_DIR.parent
ROOT_DIR = _candidate if (_candidate / "config.yaml").is_file() else BASE_DIR
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


def _env(name: str, default: Any = None) -> Any:
    """Read from environment variable, return default if not set or empty."""
    val = os.environ.get(name)
    return val if val else default


def get_all() -> dict:
    """Return full config as a nested dict (for the /api/config endpoint)."""
    cfg = _load()
    return {
        "llm": {
            "model":    LLM_MODEL,
            "timeout":  LLM_TIMEOUT,
        },
        "parser": {
            "tds_max_chars":   TDS_EXTRACT_CHARS,
            "paper_max_chars": PAPER_EXTRACT_CHARS,
            "chunk_size":      CHUNK_SIZE,
            "chunk_overlap":   CHUNK_OVERLAP,
            "max_retries":     MAX_RETRIES,
            "tds_bias":        TDS_BIAS,
        },
        "output": {
            "json_indent": JSON_INDENT,
        },
        "app": {
            "host": APP_HOST,
            "port": APP_PORT,
        },
    }


def save_config(new_cfg: dict) -> None:
    """Write updated config back to config.yaml, then reload module constants."""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(new_cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    reload()


def reload() -> None:
    """Re-read config.yaml and update all module-level constants in-place.
    Environment variables override YAML values when set.
    """
    global LLM_MODEL, LLM_TIMEOUT
    global TDS_EXTRACT_CHARS, PAPER_EXTRACT_CHARS, CHUNK_SIZE, CHUNK_OVERLAP
    global MAX_RETRIES, TDS_BIAS, JSON_INDENT, APP_HOST, APP_PORT
    global SQS_QUEUE_URL, S3_OUTPUT_PREFIX
    new = _load()
    LLM_MODEL           = _env("LLM_MODEL",     _get(new, "llm.model",      "qwen2.5:3b-instruct-q4_K_S"))
    LLM_TIMEOUT         = int(_env("LLM_TIMEOUT", _get(new, "llm.timeout",  300)))
    TDS_EXTRACT_CHARS   = _get(new, "parser.tds_max_chars",   7000)
    PAPER_EXTRACT_CHARS = _get(new, "parser.paper_max_chars", 12000)
    CHUNK_SIZE          = _get(new, "parser.chunk_size",       4000)
    CHUNK_OVERLAP       = _get(new, "parser.chunk_overlap",    300)
    MAX_RETRIES         = _get(new, "parser.max_retries",      1)
    TDS_BIAS            = _get(new, "parser.tds_bias",         2)
    JSON_INDENT         = _get(new, "output.json_indent",      2)
    APP_HOST            = _env("APP_HOST", _get(new, "app.host", "127.0.0.1"))
    APP_PORT            = int(_env("APP_PORT", _get(new, "app.port", 8000)))
    SQS_QUEUE_URL       = _env("SQS_QUEUE_URL", "")
    S3_OUTPUT_PREFIX    = _env("S3_OUTPUT_PREFIX", "parsed-json/")


# ── Module-level constants (loaded at import; live-updated by reload()) ───────
_cfg = _load()

LLM_MODEL: str       = _env("LLM_MODEL",     _get(_cfg, "llm.model",      "qwen2.5:3b-instruct-q4_K_S"))
LLM_TIMEOUT: int     = int(_env("LLM_TIMEOUT", _get(_cfg, "llm.timeout",  300)))

TDS_EXTRACT_CHARS: int   = _get(_cfg, "parser.tds_max_chars",   7000)
PAPER_EXTRACT_CHARS: int = _get(_cfg, "parser.paper_max_chars", 12000)
CHUNK_SIZE: int          = _get(_cfg, "parser.chunk_size",       4000)
CHUNK_OVERLAP: int       = _get(_cfg, "parser.chunk_overlap",    300)
MAX_RETRIES: int         = _get(_cfg, "parser.max_retries",      1)
TDS_BIAS: int            = _get(_cfg, "parser.tds_bias",         2)

JSON_INDENT: int         = _get(_cfg, "output.json_indent",      2)

APP_HOST: str            = _env("APP_HOST", _get(_cfg, "app.host", "127.0.0.1"))
APP_PORT: int            = int(_env("APP_PORT", _get(_cfg, "app.port", 8000)))

SQS_QUEUE_URL: str       = _env("SQS_QUEUE_URL", "")
S3_OUTPUT_PREFIX: str    = _env("S3_OUTPUT_PREFIX", "parsed-json/")
