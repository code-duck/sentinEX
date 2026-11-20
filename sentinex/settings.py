"""Shared settings and source-scan limits."""

from pathlib import Path
import os

from dotenv import dotenv_values


APP_ROOT = Path(__file__).resolve().parent.parent


def get_setting(name: str, default: str = "") -> str:
    """Read process configuration first, then the project-local .env file."""
    value = os.getenv(name)
    if value:
        return value
    file_value = dotenv_values(APP_ROOT / ".env").get(name)
    return str(file_value) if file_value else default

GEMINI_MODEL = get_setting("GEMINI_MODEL", "gemini-3.8-flash")
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
