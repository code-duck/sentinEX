"""Small, deterministic remediations for narrowly recognized Semgrep findings."""

import difflib
import ast as python_ast
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


def _has_top_level_import(contents: str, module_name: str) -> bool:
    """Return whether the module imports the requested name without an alias."""
    try:
        module = python_ast.parse(contents)
    except SyntaxError:
        return False
    return any(
        isinstance(statement, python_ast.Import)
        and any(alias.name == module_name and alias.asname is None for alias in statement.names)
        for statement in module.body
    )


def _add_top_level_import(contents: str, module_name: str) -> str | None:
    """Add an import after a module docstring and any future imports."""
    try:
        module = python_ast.parse(contents)
    except SyntaxError:
        return None

    lines = contents.splitlines(keepends=True)
    insertion_line = 0
    body_index = 0
    if module.body and isinstance(module.body[0], python_ast.Expr):
        value = module.body[0].value
        if isinstance(value, python_ast.Constant) and isinstance(value.value, str):
            insertion_line = module.body[0].end_lineno or 0
            body_index = 1
    else:
        while insertion_line < len(lines):
            stripped = lines[insertion_line].strip()
            if stripped and not stripped.startswith("#"):
                break
            insertion_line += 1

    for statement in module.body[body_index:]:
        if isinstance(statement, python_ast.ImportFrom) and statement.module == "__future__":
            insertion_line = statement.end_lineno or insertion_line
        else:
            break

    prefix = "".join(lines[:insertion_line]).rstrip("\r\n")
    suffix = "".join(lines[insertion_line:]).lstrip("\r\n")
    import_block = f"import {module_name}\n"
    if prefix:
        prefix += "\n\n"
    if suffix:
        import_block += "\n"
    return prefix + import_block + suffix


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
        contents = (
            source.read_bytes()
            .decode("utf-8")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
        )
        lines = contents.splitlines(keepends=True)
    except (OSError, UnicodeError, ValueError):
        return None
    add_import = bool(
        required_source
        and not _has_top_level_import(contents, required_source.removeprefix("import "))
    )
    if add_import and required_source != "import ast":
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
    if add_import:
        updated = _add_top_level_import("".join(updated_lines), "ast")
        if updated is None:
            return None
        updated_lines = updated.splitlines(keepends=True)
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
