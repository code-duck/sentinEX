"""Suggest common project test commands without executing project code."""

from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex

from .settings import EXCLUDED_DIRECTORIES


@dataclass(frozen=True)
class TestSetup:
    """A detected test suite and the Docker setup needed to run it."""

    label: str
    image: str
    command: str
    reason: str
    dependency_note: str


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve(strict=True).relative_to(root)
        return candidate.is_file()
    except (OSError, ValueError):
        return False


def _python_tests(root: Path) -> list[Path]:
    files: list[Path] = []
    excluded_directories = {name.casefold() for name in EXCLUDED_DIRECTORIES}
    for current, directories, filenames in os.walk(root, topdown=True):
        directories[:] = sorted(
            name for name in directories if name.casefold() not in excluded_directories
        )
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            if filename.startswith("test_") or filename.endswith("_test.py"):
                candidate = Path(current) / filename
                if _inside(root, candidate):
                    files.append(candidate)
            if len(files) >= 500:
                return sorted(files)
    return sorted(files)


def _python_test_command(test_file: Path, root: Path, is_pytest: bool) -> str:
    relative = test_file.relative_to(root)
    if is_pytest:
        return f"python -m pytest -q {shlex.quote(relative.as_posix())}"
    if len(relative.parts) == 1:
        module = relative.with_suffix("").as_posix().replace("/", ".")
        return f"python -m unittest {shlex.quote(module)} -v"
    parent = shlex.quote(relative.parent.as_posix())
    pattern = shlex.quote(relative.name)
    return f"python -m unittest discover -s {parent} -p {pattern} -v"


def _is_pytest(test_file: Path) -> bool:
    try:
        text = test_file.read_text(encoding="utf-8", errors="replace")[:100_000]
    except OSError:
        return False
    if re.search(r"(?m)^\s*(?:import\s+unittest|from\s+unittest\b)|unittest\.TestCase", text):
        return False
    return bool(
        re.search(r"(?m)^\s*(?:import\s+pytest|from\s+pytest\b|def\s+test_)", text)
    )


def _find_related_python_test(root: Path, target_file: str | None) -> Path | None:
    if not target_file:
        return None
    target = PurePosixPath(target_file)
    wanted = {f"test_{target.stem}.py", f"{target.stem}_test.py"}
    tests = _python_tests(root)
    for candidate in tests:
        relative = candidate.relative_to(root).as_posix()
        if candidate.name in wanted and (
            len(PurePosixPath(relative).parts) == 1
            or PurePosixPath(relative).parts[0].lower() in {"test", "tests", "spec"}
        ):
            return candidate
    return None


def _detect_python(root: Path, target_file: str | None) -> TestSetup | None:
    related = _find_related_python_test(root, target_file)
    tests = [related] if related else _python_tests(root)
    if not tests:
        return None

    representative = tests[0]
    pytest_suite = _is_pytest(representative)
    if related:
        command = _python_test_command(related, root, pytest_suite)
        reason = f"Found a test file matching the changed module: {related.relative_to(root).as_posix()}"
        label = "Python pytest" if pytest_suite else "Python unittest"
    else:
        framework = next((_is_pytest(test) for test in tests if _is_pytest(test)), False)
        if framework:
            command = "python -m pytest -q"
            label = "Python pytest"
        else:
            command = "python -m unittest discover -v"
            label = "Python unittest"
        reason = f"Found {len(tests)} Python test file(s)"

    test_tool = "pytest" if pytest_suite else "the project's Python test dependencies"
    dependency_note = (
        f"The selected image must already contain {test_tool}. SentinEX does not install "
        "packages; this test container has no network access."
    )
    return TestSetup(label, "python:3.14-slim", command, reason, dependency_note)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def detect_test_setup(root: Path, target_file: str | None = None) -> TestSetup | None:
    """Return a reviewable test suggestion based on manifests and test filenames."""
    root = root.resolve(strict=True)

    python_setup = _detect_python(root, target_file)
    if python_setup:
        return python_setup

    package = _read_json(root / "package.json")
    scripts = package.get("scripts") or {}
    if isinstance(scripts, dict) and isinstance(scripts.get("test"), str):
        script = scripts["test"].strip()
        if script and not re.search(r"(?i)no test specified|missing script", script):
            return TestSetup(
                "Node.js package test script",
                "node:lts-slim",
                "npm test",
                "Found a `test` script in package.json",
                "Node modules must already be available in the project or image. "
                "SentinEX does not install packages; the test container has no network access.",
            )

    go_tests = list(root.glob("**/*_test.go"))[:1]
    if (root / "go.mod").is_file() and go_tests:
        return TestSetup(
            "Go test suite",
            "golang:alpine",
            "go test ./...",
            "Found go.mod and Go test files",
            "Go modules must be vendored or cached in the image because the test container has no network access.",
        )

    if (root / "pom.xml").is_file() and (root / "src" / "test").is_dir():
        return TestSetup(
            "Maven test suite",
            "maven:3-eclipse-temurin-21",
            "mvn -B -o test",
            "Found pom.xml and src/test",
            "Maven dependencies must already be cached in the image; offline tests cannot download them.",
        )

    if any((root / name).is_file() for name in ("build.gradle", "build.gradle.kts")):
        wrapper = root / "gradlew"
        command = "./gradlew test --offline" if wrapper.is_file() else "gradle test --offline"
        return TestSetup(
            "Gradle test suite",
            "gradle:jdk21",
            command,
            "Found a Gradle build file",
            "The Gradle wrapper and dependencies must already be cached; tests run without network access.",
        )

    composer = _read_json(root / "composer.json")
    composer_scripts = composer.get("scripts") or {}
    if isinstance(composer_scripts, dict) and "test" in composer_scripts:
        return TestSetup(
            "PHP Composer test script",
            "composer:2",
            "composer test --no-interaction",
            "Found a test script in composer.json",
            "Composer packages must already be present in the project or image; the test container has no network access.",
        )

    if (root / "Gemfile").is_file() and (root / "Rakefile").is_file():
        return TestSetup(
            "Ruby Rake test task",
            "ruby:alpine",
            "bundle exec rake test",
            "Found Gemfile and Rakefile",
            "Ruby gems must already be installed or vendored; SentinEX does not download dependencies.",
        )

    return None
