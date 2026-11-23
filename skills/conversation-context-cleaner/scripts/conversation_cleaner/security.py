from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from .contracts import PathSafetyError


_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("authorization_bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}")),
    ("api_key_assignment", re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?key|secret|token)\s*[=:]\s*[A-Za-z0-9._~+/=-]{8,}")),
    ("synthetic_openai_key", re.compile(r"(?i)(?<![A-Za-z0-9_-])sk-(?:proj-)?[A-Za-z0-9][A-Za-z0-9_-]{11,}(?![A-Za-z0-9_-])")),
    ("password_assignment", re.compile(r"(?i)\bpassword\s*[=:]\s*\S{6,}")),
    ("credentialed_uri", re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")),
)

_FORBIDDEN_PATH_NAMES = {".env", ".secrets", ".secret", "secrets"}


def assert_safe_path(path: str | Path, *, label: str) -> Path:
    resolved = Path(path).expanduser().resolve(strict=False)
    for part in resolved.parts:
        lowered = part.casefold()
        if lowered in _FORBIDDEN_PATH_NAMES or lowered.startswith(".env."):
            raise PathSafetyError(f"{label} contains a forbidden secret path component")
    return resolved


def ensure_within(path: Path, parent: Path, *, label: str) -> Path:
    resolved_path = path.resolve(strict=False)
    resolved_parent = parent.resolve(strict=False)
    try:
        resolved_path.relative_to(resolved_parent)
    except ValueError as exc:
        raise PathSafetyError(f"{label} must stay inside {resolved_parent}") from exc
    return resolved_path


def validate_input_location(input_root: str | Path, input_path: str | Path) -> tuple[Path, Path]:
    root = assert_safe_path(input_root, label="input root")
    source = assert_safe_path(input_path, label="input file")
    if not root.is_dir():
        raise PathSafetyError("input root must be an existing directory")
    if not source.is_file():
        raise PathSafetyError("input file must be an existing explicit file")
    ensure_within(source, root, label="input file")
    return root, source


def validate_workspace_root(workspace_root: str | Path) -> Path:
    return assert_safe_path(workspace_root, label="workspace root")


def scan_text_for_secrets(text: str) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        categories = [name for name, pattern in _SECRET_PATTERNS if pattern.search(line)]
        if categories:
            findings.append({"line": line_number, "categories": categories})
    return findings


def scan_texts_for_secrets(items: Iterable[tuple[str, str]]) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for label, text in items:
        for finding in scan_text_for_secrets(text):
            findings.append({"artifact": label, **finding})
    return findings
