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


def detect_pending_patch(root: Path) -> tuple[str, str] | None:
    """Find a single uncommitted source edit on a SentinEX-created branch."""
    root = root.resolve(strict=True)
    top_level = _git(root, ["rev-parse", "--show-toplevel"])
    if top_level.returncode != 0 or Path(top_level.stdout.strip()).resolve() != root:
        return None
    branch = _git(root, ["branch", "--show-current"])
    if branch.returncode != 0 or not branch.stdout.strip().startswith("sentinex/fix-"):
        return None
    status = _git(root, ["status", "--porcelain", "-z"])
    if status.returncode != 0:
        return None
    entries = [entry for entry in status.stdout.split("\0") if entry]
    if len(entries) != 1 or entries[0][:2] != " M" or entries[0][2:3] != " ":
        return None

    raw_path = entries[0][3:]
    relative = PurePosixPath(raw_path)
    if relative.is_absolute() or ".." in relative.parts or "\\" in raw_path or ":" in raw_path:
        return None
    source = root.joinpath(*relative.parts)
    try:
        source.resolve(strict=True).relative_to(root)
    except (OSError, ValueError):
        return None
    if not source.is_file() or source.suffix.lower() not in SOURCE_EXTENSIONS:
        return None
    return branch.stdout.strip(), relative.as_posix()


def _git(root: Path, arguments: list[str], patch_text: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=root,
            input=patch_text.encode("utf-8") if patch_text is not None else None,
            capture_output=True,
            timeout=30,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PatchError(f"Could not run Git safely: {error}") from error
    return subprocess.CompletedProcess(
        args=result.args,
        returncode=result.returncode,
        stdout=result.stdout.decode("utf-8", errors="replace"),
        stderr=result.stderr.decode("utf-8", errors="replace"),
    )


def apply_approved_patch(root: Path, patch_text: str) -> str:
    """Check a clean repository, create a new branch, then apply approved text."""
    # Model-produced unified diffs can contain CRLF endings or inaccurate hunk
    # counts. Normalize line endings and let Git recount only if its strict check
    # rejects the initial patch; the actual changed lines and context stay intact.
    patch_text = patch_text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if not patch_text.endswith("\n"):
        patch_text += "\n"
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
        current_branch = _git(root, ["branch", "--show-current"])
        branch_name = current_branch.stdout.strip() or "the current branch"
        changed_paths = [
            line[3:].strip()
            for line in status.stdout.splitlines()
            if len(line) > 3
        ]
        changed_summary = ", ".join(changed_paths[:4])
        if len(changed_paths) > 4:
            changed_summary += f", and {len(changed_paths) - 4} more"
        detail = f" Changed files: {changed_summary}." if changed_summary else ""
        raise PatchError(
            f"This repository already has uncommitted changes on {branch_name}.{detail} "
            "SentinEX did not overwrite them. If these changes are the patch you already "
            "approved, continue with that branch's GitHub PR workflow; otherwise review and "
            "commit or stash your own changes before starting another patch."
        )

    check = _git(root, ["apply", "--check"], patch_text)
    recount_patch = False
    if check.returncode != 0:
        recount_check = _git(root, ["apply", "--recount", "--check"], patch_text)
        if recount_check.returncode != 0:
            detail = (recount_check.stderr or recount_check.stdout or check.stderr or
                      check.stdout or "Git rejected the patch.").strip()
            raise PatchError(
                "Git could not apply this diff, even after repairing its hunk counts. "
                f"Ask Gemini for a fresh patch and review it again. {detail[-1_500:]}"
            )
        recount_patch = True

    branch = f"sentinex/fix-{uuid.uuid4().hex[:8]}"
    switch = _git(root, ["switch", "-c", branch])
    if switch.returncode != 0:
        raise PatchError((switch.stderr or "Could not create a new branch.").strip()[-2_000:])
    apply_arguments = ["apply", "--recount"] if recount_patch else ["apply"]
    applied = _git(root, apply_arguments, patch_text)
    if applied.returncode != 0:
        detail = (applied.stderr or applied.stdout or "Git could not apply the patch.").strip()
        raise PatchError(detail[-2_000:])
    return branch
