"""Path-validated patch application behind an explicit developer approval."""

from pathlib import Path, PurePosixPath
import re
import subprocess
import uuid

from .settings import MAX_PATCH_CHARS, SOURCE_EXTENSIONS


class PatchError(RuntimeError):
    """Raised when a proposed patch is unsafe or cannot be applied."""


def validate_patch(root: Path, patch_text: str) -> str:
    """Accept only one-file text edits to an existing supported source file."""
    if not patch_text.strip() or len(patch_text) > MAX_PATCH_CHARS:
        raise PatchError("The proposed patch is empty or exceeds the size limit.")
    if "GIT binary patch" in patch_text or re.search(r"(?m)^(new|deleted) file mode ", patch_text):
        raise PatchError("Binary, file-creation, and file-deletion patches are not supported.")

    old_paths = re.findall(r"(?m)^--- (.+)$", patch_text)
    new_paths = re.findall(r"(?m)^\+\+\+ (.+)$", patch_text)
    if len(old_paths) != 1 or len(new_paths) != 1:
        raise PatchError("Only a single existing source file may be changed per patch.")
    old_path = old_paths[0].split("\t", 1)[0].strip()
    new_path = new_paths[0].split("\t", 1)[0].strip()
    if old_path.startswith("a/"):
        old_path = old_path[2:]
    if new_path.startswith("b/"):
        new_path = new_path[2:]
    if old_path == "/dev/null" or new_path == "/dev/null" or old_path != new_path:
        raise PatchError("The patch must modify one existing file without renaming it.")

    relative = PurePosixPath(old_path)
    if relative.is_absolute() or ".." in relative.parts or "\\" in old_path or ":" in old_path:
        raise PatchError("The patch targets a path outside the selected project.")
    source = root.joinpath(*relative.parts)
    try:
        source.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as error:
        raise PatchError("The patch file is not an existing file inside the selected project.") from error
    if not source.is_file() or source.suffix.lower() not in SOURCE_EXTENSIONS:
        raise PatchError("The patch target must be an eligible source-code file.")
    return relative.as_posix()


def _git(root: Path, arguments: list[str], patch_text: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            input=patch_text,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PatchError(f"Could not run Git safely: {error}") from error


def apply_approved_patch(root: Path, patch_text: str) -> str:
    """Check a clean repository, create a new branch, then apply approved text."""
    validate_patch(root, patch_text)
    top_level = _git(root, ["rev-parse", "--show-toplevel"])
    if top_level.returncode != 0:
        raise PatchError("The selected project must be a Git repository.")
    repository = Path(top_level.stdout.strip()).resolve()
    if repository != root.resolve():
        raise PatchError("Select the repository root before applying a patch.")

    status = _git(root, ["status", "--porcelain"])
    if status.returncode != 0:
        raise PatchError("Could not inspect the Git working tree.")
    if status.stdout.strip():
        raise PatchError("Commit or stash existing changes first. SentinEX only patches a clean working tree.")

    check = _git(root, ["apply", "--check"], patch_text)
    if check.returncode != 0:
        detail = (check.stderr or check.stdout or "Git rejected the patch.").strip()
        raise PatchError(detail[-2_000:])

    branch = f"sentinex/fix-{uuid.uuid4().hex[:8]}"
    switch = _git(root, ["switch", "-c", branch])
    if switch.returncode != 0:
        raise PatchError((switch.stderr or "Could not create a new branch.").strip()[-2_000:])
    applied = _git(root, ["apply"], patch_text)
    if applied.returncode != 0:
        detail = (applied.stderr or applied.stdout or "Git could not apply the patch.").strip()
        raise PatchError(detail[-2_000:])
    return branch
