"""Small, deterministic remediations for narrowly recognized Semgrep findings."""

import difflib
from pathlib import Path, PurePosixPath
import re


_RULE_FIXES = {
    "python.lang.security.audit.subprocess-shell-true.subprocess-shell-true": (
        re.compile(r"(\bshell\s*=\s*)True\b"),
        r"\1False",
        "Explicitly set shell=False for this subprocess call.",
        None,
    ),
    "python.lang.security.audit.eval-detected.eval-detected": (
        re.compile(r"\beval\s*\("),
        "ast.literal_eval(",
        "Use ast.literal_eval for parsing Python literal values.",
        "import ast",
    ),
    "sentinex.python.eval-detected": (
        re.compile(r"\beval\s*\("),
        "ast.literal_eval(",
        "Use ast.literal_eval for parsing Python literal values.",
        "import ast",
    ),
    "python.lang.security.unverified-ssl-context.unverified-ssl-context": (
        re.compile(r"\bssl\._create_unverified_context\s*\("),
        "ssl.create_default_context(",
        "Use a TLS context that verifies certificates by default.",
        "import ssl",
    ),
    "sentinex.python.unverified-ssl-context": (
        re.compile(r"\bssl\._create_unverified_context\s*\("),
        "ssl.create_default_context(",
        "Use a TLS context that verifies certificates by default.",
        "import ssl",
    ),
}


def propose_rule_based_patch(root: Path, finding: dict) -> dict[str, str] | None:
    """Prepare a narrowly scoped offline fix for a recognized finding."""
    rule_fix = _RULE_FIXES.get(str(finding.get("check_id", "")))
    if rule_fix is None:
        return None
    pattern, replacement, remediation, required_source = rule_fix

    relative_path = PurePosixPath(str(finding.get("path", "")))
    if relative_path.is_absolute() or ".." in relative_path.parts or not relative_path.parts:
        return None

    project_root = root.resolve()
    source = project_root.joinpath(*relative_path.parts)
    try:
        source.resolve(strict=True).relative_to(project_root)
        contents = source.read_bytes().decode("utf-8")
        lines = contents.splitlines(keepends=True)
    except (OSError, UnicodeError, ValueError):
        return None
    if required_source and required_source not in contents:
        return None

    try:
        line_number = int(finding.get("start", {}).get("line", 0))
    except (TypeError, ValueError):
        return None
    if not 1 <= line_number <= len(lines):
        return None

    old_line = lines[line_number - 1]
    new_line, replacements = pattern.subn(replacement, old_line, count=1)
    if replacements != 1:
        return None

    updated_lines = list(lines)
    updated_lines[line_number - 1] = new_line
    diff = "".join(
        difflib.unified_diff(
            lines,
            updated_lines,
            fromfile=f"a/{relative_path.as_posix()}",
            tofile=f"b/{relative_path.as_posix()}",
            lineterm="\n",
        )
    )
    return {
        "summary": f"Built-in remediation: {remediation} It was prepared locally, not by Gemini.",
        "diff": diff,
        "source": "Built-in rule-based fix (offline; not Gemini-generated)",
    }
