"""Local Semgrep Community Edition scanning."""

import json
import os
from pathlib import Path
import subprocess
import sys

from .docker_runner import DockerTestError, docker_executable
from .project import eligible_source_files
from .settings import EXCLUDED_DIRECTORIES, SOURCE_EXTENSIONS


class ScanError(RuntimeError):
    """Raised when a local Semgrep scan cannot complete."""


def _semgrep_executable() -> Path:
    executable = Path(sys.executable).parent / "semgrep.exe"
    if not executable.is_file():
        raise ScanError("Semgrep is missing from this Python environment.")
    return executable


def _scan_arguments(target: str) -> list[str]:
    arguments = [
        "scan", "--config", "p/security-audit", "--json", "--metrics=off",
    ]
    for extension in sorted(SOURCE_EXTENSIONS):
        arguments.extend(["--include", f"*{extension}"])
    for directory in sorted(EXCLUDED_DIRECTORIES):
        arguments.extend(["--exclude", f"**/{directory}/**"])
    arguments.append(target)
    return arguments


def _parse_results(output: str) -> list[dict]:
    try:
        payload = json.loads(output or "{}")
    except json.JSONDecodeError as error:
        raise ScanError("Semgrep returned invalid JSON.") from error
    return payload.get("results", [])


def _scan_with_docker(root: Path) -> list[dict]:
    try:
        docker = docker_executable()
    except DockerTestError as error:
        raise ScanError(
            "Windows blocked the local Semgrep executable, and Docker CLI is unavailable. "
            f"{error}"
        ) from error

    command = [
        docker,
        "run",
        "--rm",
        "--pull=missing",
        "--network=bridge",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=128",
        "--memory=2g",
        "--cpus=2",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=256m",
        "--mount",
        f"type=bind,source={root.as_posix()},target=/src,readonly",
        "--workdir",
        "/src",
        "-e",
        "SEMGREP_SEND_METRICS=off",
        "-e",
        "HOME=/tmp",
        "-e",
        "SEMGREP_LOG_FILE=/tmp/semgrep.log",
        "-e",
        "SEMGREP_VERSION_CACHE_PATH=/tmp/semgrep-version-cache",
        "semgrep/semgrep:latest",
        "semgrep",
        *_scan_arguments("/src"),
    ]
    try:
        result = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ScanError("Docker-based Semgrep scan timed out after ten minutes.") from error
    except OSError as error:
        raise ScanError(f"Could not start Docker-based Semgrep: {error}") from error

    if result.returncode not in (0, 1):
        detail = (result.stderr or result.stdout or "Semgrep Docker scan failed.").strip()
        raise ScanError(detail[-4_000:])
    return _parse_results(result.stdout)


def scan_project(root: Path) -> list[dict]:
    """Run Semgrep against eligible source extensions in the selected folder."""
    if not eligible_source_files(root):
        return []

    environment = os.environ.copy()
    environment["SEMGREP_SEND_METRICS"] = "off"
    try:
        try:
            executable = _semgrep_executable()
        except ScanError:
            return _scan_with_docker(root)
        result = subprocess.run(
            [str(executable), *_scan_arguments(str(root))],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
            shell=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as error:
        raise ScanError("Semgrep scan timed out after five minutes.") from error
    except OSError as error:
        return _scan_with_docker(root)

    if sys.platform == "win32" and (
        result.returncode not in (0, 1) or not result.stdout.strip()
    ):
        return _scan_with_docker(root)

    if result.returncode not in (0, 1):
        detail = (result.stderr or result.stdout or "Semgrep returned an error.").strip()
        raise ScanError(detail[-4_000:])
    return _parse_results(result.stdout)
