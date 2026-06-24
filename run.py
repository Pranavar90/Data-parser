"""
run.py — Entry point for the Planet Materials Labs PDF Bulk Parser.

Usage:
    python run.py
"""

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent
BACKEND = ROOT / "backend"

# Make backend modules importable
sys.path.insert(0, str(BACKEND))

# Set working directory so uvicorn resolves imports correctly
os.chdir(BACKEND)

import config as cfg
import uvicorn

# Configure logging for all backend modules
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)

if __name__ == "__main__":
    print()
    print("=" * 58)
    print("  Planet Materials Labs - PDF Bulk Parser")
    print("=" * 58)
    print(f"  URL    :  http://{cfg.APP_HOST}:{cfg.APP_PORT}")
    print(f"  Model  :  {cfg.LLM_MODEL}")
    print("=" * 58)
    print("  Press Ctrl+C to stop")
    print()

    uvicorn.run(
        "main:app",
        host=cfg.APP_HOST,
        port=cfg.APP_PORT,
        reload=False,
        log_level="warning",
    )
