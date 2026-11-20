"""Local Semgrep Community Edition scanning."""

import json
import os
from pathlib import Path
import subprocess
import sys

from .project import eligible_source_files
from .settings import EXCLUDED_DIRECTORIES, SOURCE_EXTENSIONS


class ScanError(RuntimeError):
    """Raised when a local Semgrep scan cannot complete."""


def _semgrep_executable() -> Path:
    executable = Path(sys.executable).parent / "semgrep.exe"
    if not executable.is_file():
        raise ScanError("Semgrep is missing from this Python environment.")
    return executable


def scan_project(root: Path) -> list[dict]:
    """Run Semgrep against eligible source extensions in the selected folder."""
    if not eligible_source_files(root):
        return []

    command = [
        str(_semgrep_executable()), "scan", "--config", "p/security-audit",
        "--json", "--metrics=off",
    ]
    for extension in sorted(SOURCE_EXTENSIONS):
        command.extend(["--include", f"*{extension}"])
    for directory in sorted(EXCLUDED_DIRECTORIES):
        command.extend(["--exclude", f"**/{directory}/**"])
    command.append(str(root))

    environment = os.environ.copy()
    environment["SEMGREP_SEND_METRICS"] = "off"
    try:
        result = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            shell=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as error:
        raise ScanError("Semgrep scan timed out after five minutes.") from error
    except OSError as error:
        raise ScanError(f"Could not start Semgrep: {error}") from error

    if result.returncode not in (0, 1):
        detail = (result.stderr or result.stdout or "Semgrep returned an error.").strip()
        raise ScanError(detail[-4_000:])
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as error:
        raise ScanError("Semgrep returned invalid JSON.") from error
    return payload.get("results", [])
