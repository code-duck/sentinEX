"""Run developer-approved test commands in a constrained Docker container."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid


MAX_TEST_COMMAND_CHARS = 500
MAX_TEST_OUTPUT_CHARS = 20_000
DEFAULT_TIMEOUT_SECONDS = 600
_IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$")


class DockerTestError(RuntimeError):
    """Raised when restricted test execution cannot be started safely."""


@dataclass(frozen=True)
class DockerTestResult:
    """Summary of one containerized test run."""

    image: str
    command: str
    exit_code: int
    output: str
    duration_seconds: float


def _docker_executable() -> str:
    """Find the Docker CLI without relying on a stale terminal PATH."""
    executable = shutil.which("docker")
    if executable:
        return executable

    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    common_install = program_files / "Docker" / "Docker" / "resources" / "bin" / "docker.exe"
    if common_install.is_file():
        return str(common_install)

    raise DockerTestError(
        "Docker Desktop may be open, but its Docker CLI is not available to this process. "
        "Restart VS Code/Codex after Docker installation and confirm Docker Desktop says "
        "‘Engine running’."
    )


def _validate_inputs(root: Path, image: str, command: str) -> Path:
    if not image or not _IMAGE_PATTERN.fullmatch(image):
        raise DockerTestError("Enter a valid Docker image name, such as python:3.14-slim.")
    if not command.strip() or len(command) > MAX_TEST_COMMAND_CHARS:
        raise DockerTestError("Enter a test command of 1–500 characters.")
    if "\0" in command or "\n" in command or "\r" in command:
        raise DockerTestError("The test command must fit on one line.")

    try:
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        raise DockerTestError("The selected project folder is no longer available.") from error
    if not resolved_root.is_dir():
        raise DockerTestError("The selected project path is not a directory.")
    return resolved_root


def run_restricted_tests(
    root: Path,
    image: str,
    command: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> DockerTestResult:
    """Run a one-line, user-approved command with no network and read-only source."""
    project_root = _validate_inputs(root, image.strip(), command.strip())
    docker = _docker_executable()

    try:
        info = subprocess.run(
            [docker, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DockerTestError("Could not contact the Docker engine. Start Docker Desktop and retry.") from error
    if info.returncode != 0:
        detail = (info.stderr or info.stdout or "Docker engine is unavailable.").strip()
        raise DockerTestError(detail[-2_000:])

    mount_path = project_root.as_posix()
    container_name = f"sentinex-tests-{uuid.uuid4().hex[:12]}"
    docker_command = [
        docker,
        "run",
        "--rm",
        "--name",
        container_name,
        "--pull=missing",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=128",
        "--memory=2g",
        "--cpus=2",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=256m",
        "--mount",
        f"type=bind,source={mount_path},target=/workspace,readonly",
        "--workdir",
        "/workspace",
        image.strip(),
        "sh",
        "-lc",
        command.strip(),
    ]

    started = time.monotonic()
    try:
        completed = subprocess.run(
            docker_command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as error:
        subprocess.run(
            [docker, "stop", "--time", "1", container_name],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            shell=False,
        )
        output = "\n".join(
            part for part in (error.stdout, error.stderr) if isinstance(part, str) and part
        )
        raise DockerTestError(
            f"Container test run exceeded {timeout_seconds} seconds. "
            f"Partial output:\n{output[-MAX_TEST_OUTPUT_CHARS:]}"
        ) from error
    except OSError as error:
        raise DockerTestError(f"Could not start Docker test container: {error}") from error

    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    if not output:
        output = "(The test command produced no output.)"
    if len(output) > MAX_TEST_OUTPUT_CHARS:
        output = "[Output truncated to the last 20,000 characters.]\n" + output[-MAX_TEST_OUTPUT_CHARS:]

    return DockerTestResult(
        image=image.strip(),
        command=command.strip(),
        exit_code=completed.returncode,
        output=output,
        duration_seconds=round(time.monotonic() - started, 1),
    )
