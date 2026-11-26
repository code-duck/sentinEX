"""Run Semgrep CE and fail closed when a scan is incomplete."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys

from .docker_runner import DockerTestError, docker_executable
from .project import eligible_source_files
from .settings import APP_ROOT, EXCLUDED_DIRECTORIES, SOURCE_EXTENSIONS


SEMGREP_IMAGE = "semgrep/semgrep:1.186.0"
RULES_FILE = APP_ROOT / "sentinex" / "security_rules.yml"


class ScanError(RuntimeError):
    """Raised when a Semgrep scan cannot be completed reliably."""


@dataclass(frozen=True)
class ScanReport:
    """Findings and coverage details for one completed Semgrep scan."""

    findings: list[dict]
    scanned_files: list[str]
    skipped_files: list[str]
    engine: str


def _semgrep_executable() -> Path:
    executable = Path(sys.executable).parent / "semgrep.exe"
    if not executable.is_file():
        raise ScanError("Semgrep is missing from this Python environment.")
    return executable


def _scan_arguments(target: str, rules_path: str) -> list[str]:
    arguments = [
        "scan",
        "--config",
        "p/security-audit",
        "--config",
        rules_path,
        "--json",
        "--metrics=off",
        "--no-git-ignore",
    ]
    for extension in sorted(SOURCE_EXTENSIONS):
        arguments.extend(["--include", f"*{extension}"])
    for directory in sorted(EXCLUDED_DIRECTORIES):
        arguments.extend(["--exclude", f"**/{directory}/**"])
    arguments.append(target)
    return arguments


def _parse_report(
    output: str,
    root: Path,
    engine: str,
    eligible_files: list[Path],
) -> ScanReport:
    try:
        payload = json.loads(output)
    except (TypeError, json.JSONDecodeError) as error:
        raise ScanError("Semgrep did not return valid JSON results.") from error

    errors = payload.get("errors") or []
    if errors:
        details = []
        for item in errors[:5]:
            if isinstance(item, dict):
                message = str(item.get("message") or item.get("type") or item)
            else:
                message = str(item)
            details.append(message)
        raise ScanError("Semgrep reported scan errors: " + " | ".join(details))

    paths = payload.get("paths") or {}
    scanned = paths.get("scanned") or []
    if not scanned:
        skipped = paths.get("skipped") or []
        detail = ""
        if skipped:
            reasons = [str(item.get("reason", "unknown reason")) for item in skipped[:3]]
            detail = " Skipped: " + "; ".join(reasons)
        raise ScanError(
            "Semgrep completed without scanning any eligible source files."
            + detail
        )

    scanned_paths = _normalize_paths(scanned, root)
    if not scanned_paths:
        raise ScanError("Semgrep scanned paths outside the selected project folder.")

    skipped_paths = _normalize_paths(
        [item.get("path", "") for item in (paths.get("skipped") or []) if isinstance(item, dict)],
        root,
    )
    scanned_keys = {path.casefold() for path in scanned_paths}
    missing = [
        source.relative_to(root).as_posix()
        for source in eligible_files
        if source.relative_to(root).as_posix().casefold() not in scanned_keys
    ]
    if missing:
        preview = ", ".join(missing[:5])
        if len(missing) > 5:
            preview += f", and {len(missing) - 5} more"
        raise ScanError(
            f"Semgrep did not scan {len(missing)} eligible source file(s): {preview}. "
            "SentinEX will not report this as a clean scan."
        )

    findings = _normalize_findings(payload.get("results") or [], root)
    findings = _prefer_registry_rules_over_local_duplicates(findings)
    return ScanReport(
        findings=findings,
        scanned_files=scanned_paths,
        skipped_files=skipped_paths,
        engine=engine,
    )


def _resolve_report_path(raw_path: str, root: Path) -> Path | None:
    raw_path = str(raw_path).strip()
    if not raw_path:
        return None
    if raw_path == "/src":
        candidate = root
    elif raw_path.startswith("/src/"):
        candidate = root / raw_path.removeprefix("/src/")
    else:
        path = Path(raw_path)
        candidate = path if path.is_absolute() else root / path
    try:
        return candidate.resolve().relative_to(root)
    except (OSError, ValueError):
        return None


def _normalize_paths(paths: list[str], root: Path) -> list[str]:
    relative_paths = []
    for raw_path in paths:
        relative_path = _resolve_report_path(raw_path, root)
        if relative_path is not None and relative_path.as_posix() not in relative_paths:
            relative_paths.append(relative_path.as_posix())
    return relative_paths


def _normalize_findings(findings: list[dict], root: Path) -> list[dict]:
    normalized: list[dict] = []
    for finding in findings:
        relative_path = _resolve_report_path(finding.get("path", ""), root)
        if relative_path is None:
            continue
        finding["path"] = relative_path.as_posix()
        normalized.append(finding)
    return normalized


def _prefer_registry_rules_over_local_duplicates(findings: list[dict]) -> list[dict]:
    """Keep the curated rule ID when both it and a local fallback flag one issue."""
    local_to_curated = {
        "sentinex.python.eval-detected":
            "python.lang.security.audit.eval-detected.eval-detected",
        "sentinex.python.unverified-ssl-context":
            "python.lang.security.unverified-ssl-context.unverified-ssl-context",
    }
    curated_locations = {
        (
            finding.get("path"),
            finding.get("start", {}).get("line"),
            finding.get("check_id"),
        )
        for finding in findings
    }
    kept = []
    for finding in findings:
        rule_id = finding.get("check_id")
        curated_id = local_to_curated.get(rule_id)
        if curated_id and (
            finding.get("path"), finding.get("start", {}).get("line"), curated_id
        ) in curated_locations:
            continue
        kept.append(finding)
    return kept


def _scan_with_docker(root: Path, eligible_files: list[Path]) -> ScanReport:
    if not RULES_FILE.is_file():
        raise ScanError(f"SentinEX's local Semgrep rules are missing: {RULES_FILE.name}.")
    try:
        docker = docker_executable()
    except DockerTestError as error:
        raise ScanError(
            "SentinEX needs Docker Desktop's Linux engine to run Semgrep on Windows. "
            f"{error}"
        ) from error

    rules_mount = RULES_FILE.resolve().as_posix()
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
        "--mount",
        f"type=bind,source={rules_mount},target=/tmp/sentinex_security_rules.yml,readonly",
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
        SEMGREP_IMAGE,
        "semgrep",
        *_scan_arguments("/src", "/tmp/sentinex_security_rules.yml"),
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
    return _parse_report(result.stdout, root, "Docker", eligible_files)


def scan_project(root: Path) -> ScanReport:
    """Scan eligible files and include evidence that Semgrep actually read them."""
    root = root.resolve(strict=True)
    eligible_files = eligible_source_files(root)
    if not eligible_files:
        raise ScanError(
            "No eligible source files were found. SentinEX scans Python, JavaScript, "
            "TypeScript, Java, Go, PHP, and Ruby files."
        )

    # The Windows Semgrep wheel in this environment has a broken OpenTelemetry import.
    # Prefer the official, isolated image there rather than silently treating a failed
    # local executable as a clean project.
    if sys.platform == "win32":
        return _scan_with_docker(root, eligible_files)

    try:
        executable = _semgrep_executable()
        environment = os.environ.copy()
        environment["SEMGREP_SEND_METRICS"] = "off"
        result = subprocess.run(
            [str(executable), *_scan_arguments(str(root), str(RULES_FILE))],
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
        raise ScanError(f"Could not start Semgrep: {error}") from error

    if result.returncode not in (0, 1):
        detail = (result.stderr or result.stdout or "Semgrep returned an error.").strip()
        raise ScanError(detail[-4_000:])
    return _parse_report(result.stdout, root, "local Semgrep", eligible_files)
