"""Publish an approved SentinEX branch and open a draft PR through GitHub MCP."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlparse

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .docker_runner import DockerTestError, docker_executable
from .settings import get_setting


class PullRequestError(RuntimeError):
    """Raised when branch publishing or MCP pull-request creation fails."""


@dataclass(frozen=True)
class PullRequestInfo:
    """GitHub pull request created by SentinEX."""

    number: int
    url: str


def github_mcp_token_configured() -> bool:
    """Report whether a GitHub MCP token is configured without exposing it."""
    return bool(get_setting("GITHUB_PERSONAL_ACCESS_TOKEN").strip())


def _git(root: Path, arguments: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PullRequestError(f"Could not run Git safely: {error}") from error


def _repository_slug(root: Path) -> tuple[str, str]:
    remote = _git(root, ["remote", "get-url", "origin"])
    if remote.returncode != 0:
        raise PullRequestError("Add a GitHub repository as the project's `origin` remote first.")

    value = remote.stdout.strip()
    ssh_match = re.fullmatch(r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?", value)
    if ssh_match:
        return ssh_match.group(1), ssh_match.group(2)
    parsed = urlparse(value)
    if parsed.hostname != "github.com":
        raise PullRequestError("SentinEX PR creation currently supports github.com remotes only.")
    segments = [part for part in parsed.path.strip("/").split("/") if part]
    if len(segments) != 2:
        raise PullRequestError("The GitHub origin URL must identify one owner and one repository.")
    return segments[0], segments[1].removesuffix(".git")


def _target_branch(root: Path) -> str:
    remote_head = _git(root, ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"])
    if remote_head.returncode == 0:
        return remote_head.stdout.strip().removeprefix("origin/")
    for candidate in ("main", "master"):
        if _git(root, ["show-ref", "--verify", f"refs/remotes/origin/{candidate}"]).returncode == 0:
            return candidate
    raise PullRequestError(
        "Could not identify the repository's default branch. Fetch the origin branches and retry."
    )


@asynccontextmanager
async def _github_session(root: Path):
    """Open one official GitHub MCP server session for the selected repository."""
    token = get_setting("GITHUB_PERSONAL_ACCESS_TOKEN").strip()
    if not token:
        raise PullRequestError(
            "Set GITHUB_PERSONAL_ACCESS_TOKEN in E:\\SentinEX\\.env. "
            "Use a fine-grained token limited to this repository."
        )

    try:
        docker = docker_executable()
    except DockerTestError as error:
        raise PullRequestError(str(error)) from error

    environment = os.environ.copy()
    environment["GITHUB_PERSONAL_ACCESS_TOKEN"] = token
    parameters = StdioServerParameters(
        command=docker,
        args=[
            "run", "-i", "--rm", "--pull=missing",
            "-e", "GITHUB_PERSONAL_ACCESS_TOKEN",
            "-e", "GITHUB_TOOLSETS=pull_requests,repos",
            "ghcr.io/github/github-mcp-server:latest",
        ],
        env=environment,
        cwd=str(root),
    )

    try:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session
    except PullRequestError:
        raise
    except Exception as error:
        raise PullRequestError(f"GitHub MCP request failed: {error}") from error


def _response_text(response) -> str:
    if response.isError:
        details = "\n".join(
            block.text for block in response.content if getattr(block, "text", None)
        )
        raise PullRequestError(details or "GitHub MCP action failed.")
    return "\n".join(
        block.text for block in response.content if getattr(block, "text", None)
    )


async def _create_pr_with_mcp(
    root: Path,
    owner: str,
    repository: str,
    branch: str,
    base: str,
    title: str,
    body: str,
) -> PullRequestInfo:
    async with _github_session(root) as session:
        response = await session.call_tool(
            "create_pull_request",
            arguments={
                "owner": owner,
                "repo": repository,
                "title": title,
                "head": branch,
                "base": base,
                "body": body,
                "draft": True,
            },
        )

    details = _response_text(response)
    candidates = [details]
    structured = getattr(response, "structuredContent", None)
    if structured is not None:
        candidates.append(str(structured))
        if isinstance(structured, dict):
            pull_request = structured.get("pull_request") or structured.get("pullRequest")
            if isinstance(pull_request, dict):
                candidates.append(str(pull_request.get("url", "")))
            candidates.append(str(structured.get("url", "")))

    url = ""
    number = None
    for candidate in candidates:
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            url = str(payload.get("url", "") or url)
            nested = payload.get("pull_request") or payload.get("pullRequest")
            if isinstance(nested, dict):
                url = str(nested.get("url", "") or url)
        match = re.search(r"https://github\.com/[^\s/]+/[^\s/]+/pull/(\d+)", url or candidate)
        if match:
            number = int(match.group(1))
            if not url:
                url = match.group(0)
            break

    if number is None or not url:
        raise PullRequestError(
            "GitHub reported that the draft PR was created, but SentinEX could not read its URL and number. "
            f"Check the repository's Pull requests page before retrying. MCP response: {details[:500]}"
        )
    return PullRequestInfo(number=number, url=url)


def publish_as_draft_pr(
    root: Path,
    branch: str,
    title: str,
    body: str,
    base: str | None = None,
) -> PullRequestInfo:
    """Push exactly one approved patch branch, then create a draft PR using MCP."""
    root = root.resolve(strict=True)
    if not branch.startswith("sentinex/fix-"):
        raise PullRequestError("Only a SentinEX-approved fix branch can be published here.")
    if not title.strip() or len(title) > 200:
        raise PullRequestError("Enter a pull-request title of 1–200 characters.")
    if not body.strip() or len(body) > 10_000:
        raise PullRequestError("Enter a pull-request description of 1–10,000 characters.")

    top_level = _git(root, ["rev-parse", "--show-toplevel"])
    if top_level.returncode != 0 or Path(top_level.stdout.strip()).resolve() != root:
        raise PullRequestError("Select the root of the Git repository containing this patch.")
    current = _git(root, ["branch", "--show-current"])
    if current.returncode != 0 or current.stdout.strip() != branch:
        raise PullRequestError("The active Git branch no longer matches the approved SentinEX branch.")
    status = _git(root, ["status", "--porcelain"])
    if status.returncode != 0:
        raise PullRequestError("Could not inspect the fix branch before publishing.")

    changed_paths = _git(root, ["diff", "--name-only"])
    staged_paths = _git(root, ["diff", "--cached", "--name-only"])
    if changed_paths.returncode != 0 or staged_paths.returncode != 0:
        raise PullRequestError("Could not inspect the branch changes before publishing.")
    all_changed = set(changed_paths.stdout.splitlines()) | set(staged_paths.stdout.splitlines())
    status_paths = [line[3:] for line in status.stdout.splitlines() if len(line) >= 4]
    if any(line.startswith("??") for line in status.stdout.splitlines()):
        raise PullRequestError("Untracked files are present; review and commit them separately.")
    if status.stdout.strip() and len(all_changed | set(status_paths)) != 1:
        raise PullRequestError("The PR action only publishes a single-file SentinEX patch branch.")

    owner, repository = _repository_slug(root)
    target = base.strip() if base and base.strip() else _target_branch(root)
    if not get_setting("GITHUB_PERSONAL_ACCESS_TOKEN").strip():
        raise PullRequestError(
            "Set GITHUB_PERSONAL_ACCESS_TOKEN in E:\\SentinEX\\.env. "
            "Use a fine-grained token limited to this repository with pull-request write access."
        )

    if status.stdout.strip():
        path = next(iter(all_changed | set(status_paths)))
        stage = _git(root, ["add", "--", path])
        if stage.returncode != 0:
            raise PullRequestError((stage.stderr or "Could not stage the approved patch.").strip()[-2_000:])
        commit = _git(root, ["commit", "-m", "fix: apply approved security patch"], timeout=60)
        if commit.returncode != 0:
            raise PullRequestError((commit.stderr or commit.stdout or "Could not commit the approved patch.").strip()[-2_000:])

    base_ref = f"refs/remotes/origin/{target}"
    patch_paths = _git(root, ["diff", "--name-only", f"{base_ref}...HEAD"])
    commit_count = _git(root, ["rev-list", "--count", f"{base_ref}..HEAD"])
    if patch_paths.returncode != 0 or commit_count.returncode != 0:
        raise PullRequestError("Could not compare the fix branch with its GitHub base branch.")
    if len(patch_paths.stdout.splitlines()) != 1 or commit_count.stdout.strip() != "1":
        raise PullRequestError(
            "The PR branch must contain exactly one SentinEX patch commit that changes one file."
        )

    push = _git(root, ["push", "--set-upstream", "origin", branch], timeout=120)
    if push.returncode != 0:
        detail = (push.stderr or push.stdout or "Git could not push the fix branch.").strip()
        raise PullRequestError(detail[-3_000:])

    try:
        return asyncio.run(
            asyncio.wait_for(
                _create_pr_with_mcp(root, owner, repository, branch, target, title.strip(), body.strip()),
                timeout=120,
            )
        )
    except TimeoutError as error:
        raise PullRequestError("GitHub MCP did not respond within two minutes.") from error


async def _merge_pr_with_mcp(root: Path, owner: str, repository: str, number: int) -> str:
    async with _github_session(root) as session:
        ready_response = await session.call_tool(
            "update_pull_request",
            arguments={
                "owner": owner,
                "repo": repository,
                "pullNumber": number,
                "draft": False,
            },
        )
        _response_text(ready_response)
        merge_response = await session.call_tool(
            "merge_pull_request",
            arguments={
                "owner": owner,
                "repo": repository,
                "pullNumber": number,
                "merge_method": "squash",
            },
        )
    return _response_text(merge_response) or f"Pull request #{number} merged."


def merge_approved_pull_request(root: Path, number: int) -> str:
    """Merge a SentinEX draft PR only after the user explicitly approves in the UI."""
    if not isinstance(number, int) or number < 1:
        raise PullRequestError("The pull request number is invalid.")
    owner, repository = _repository_slug(root)
    if not get_setting("GITHUB_PERSONAL_ACCESS_TOKEN").strip():
        raise PullRequestError(
            "Set GITHUB_PERSONAL_ACCESS_TOKEN in E:\\SentinEX\\.env before merging."
        )
    try:
        return asyncio.run(
            asyncio.wait_for(_merge_pr_with_mcp(root, owner, repository, number), timeout=120)
        )
    except TimeoutError as error:
        raise PullRequestError("GitHub MCP did not respond within two minutes.") from error
