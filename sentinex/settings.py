"""Shared settings and source-scan limits."""

from pathlib import Path
import os

from dotenv import load_dotenv


APP_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(APP_ROOT / ".env")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
SOURCE_EXTENSIONS = frozenset({".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".php", ".rb"})
EXCLUDED_DIRECTORIES = frozenset({
    ".git", ".venv", "venv", "node_modules", "vendor", "dist", "build",
    "target", "__pycache__", ".next", ".tox", ".mypy_cache",
})
MAX_SOURCE_FILES = 500
MAX_SOURCE_FILE_BYTES = 1_500_000
MAX_FINDINGS_FOR_AI = 20
MAX_FULL_REVIEW_CHARS = 240_000
AI_REVIEW_CHUNK_CHARS = 30_000
MAX_PATCH_CHARS = 50_000
