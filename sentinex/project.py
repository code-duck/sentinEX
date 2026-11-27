"""Project-folder boundaries and safe source selection."""

from pathlib import Path
import os
import re

from .settings import (
    EXCLUDED_DIRECTORIES,
    MAX_SOURCE_FILE_BYTES,
    MAX_SOURCE_FILES,
    SOURCE_EXTENSIONS,
)


_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|passwd|authorization)"
    r"(\s*[:=]\s*)(['\"][^'\"]{6,}['\"])"
)
_GOOGLE_KEY = re.compile(r"AIza[0-9A-Za-z_-]{25,}")


def resolve_project_root(value: str) -> Path:
    """Return an existing directory selected by the user."""
    if not value.strip():
        raise ValueError("Enter the path to the project folder you want to scan.")
    root = Path(value).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Choose a project directory.")
    return root


def eligible_source_files(root: Path) -> list[Path]:
    """List source files under root, excluding generated and linked-out files."""
    files: list[Path] = []
    excluded_directories = {name.casefold() for name in EXCLUDED_DIRECTORIES}
    for current, directories, filenames in os.walk(root, topdown=True):
        directories[:] = sorted(
            name for name in directories if name.casefold() not in excluded_directories
        )
        for filename in sorted(filenames):
            candidate = Path(current) / filename
            if candidate.suffix.lower() not in SOURCE_EXTENSIONS:
                continue
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(root)
                if resolved.stat().st_size > MAX_SOURCE_FILE_BYTES:
                    continue
            except (OSError, ValueError):
                continue
            files.append(resolved)
            if len(files) >= MAX_SOURCE_FILES:
                return files
    return files


def redact_secrets(text: str) -> str:
    """Redact common inline credentials before sending code to an AI API."""
    text = _SECRET_ASSIGNMENT.sub(
        lambda match: f'{match.group(1)}{match.group(2)}"[REDACTED]"', text
    )
    return _GOOGLE_KEY.sub("[REDACTED_API_KEY]", text)


def finding_context(root: Path, finding: dict, radius: int = 14) -> str:
    """Read a small, redacted excerpt for a finding within the selected root."""
    source = Path(finding.get("path", ""))
    if not source.is_absolute():
        source = root / source
    try:
        source = source.resolve(strict=True)
        source.relative_to(root)
        lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, ValueError):
        return "[Code context unavailable inside the selected project.]"

    start = max(1, int(finding.get("start", {}).get("line", 1)))
    end = max(start, int(finding.get("end", {}).get("line", start)))
    first = max(1, start - radius)
    last = min(len(lines), end + radius)
    excerpt = "\n".join(f"{number}: {lines[number - 1]}" for number in range(first, last + 1))
    return redact_secrets(excerpt[:12_000])
