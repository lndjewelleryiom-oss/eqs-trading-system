from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


TEST_DATA_LINE = re.compile(r"(?im)^\s*TEST[_ -]?DATA\s*[:=]\s*(?:true|yes|1)\b")
TEST_DATA_CLASS = re.compile(r"(?i)\bTEST[_ -]?DATA(?:[_ -][A-Z0-9_-]+)?\b")
TEST_EVIDENCE_URI = re.compile(r"(?i)\btest-evidence://")

EXACT_SENSITIVE_KEYS = {
    "objective_evidence_recorded",
    "signed_approval_reference",
    "signed_approval_references",
    "signed_approval_record_reference",
    "signed_approval_record_references",
    "live_authorization_reference",
    "live_authorization_references",
    "live_authorisation_reference",
    "live_authorisation_references",
}

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    "artifacts",
    "node_modules",
    "test-results",
}


@dataclass(frozen=True)
class Violation:
    source: str
    field_path: str
    reason: str

    def render(self) -> str:
        return f"{self.source}:{self.field_path}: {self.reason}"


def _normalise_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")


def _is_live_authorization_key(key: str) -> bool:
    normalised = _normalise_key(key)
    has_live = "live" in normalised
    has_auth = any(token in normalised for token in ("authorization", "authorisation", "authorized", "authorised"))
    return has_live and has_auth


def _is_sensitive_key(key: str) -> bool:
    normalised = _normalise_key(key)
    return normalised in EXACT_SENSITIVE_KEYS or _is_live_authorization_key(normalised)


def _file_is_test_data(path: Path) -> bool:
    if "TEST_DATA" in path.name.upper():
        return True
    # Bound memory even for huge binary artifacts or unbroken text lines.
    found = False
    prefix = ""
    try:
        with path.open("r", encoding="utf-8") as handle:
            while chunk := handle.read(65536):
                for part in chunk.splitlines(keepends=True):
                    ended = part.endswith(("\n", "\r"))
                    # Whitespace length is irrelevant to the marker grammar.
                    prefix = re.sub(r"\s+", " ", prefix + part).lstrip()[:256]
                    match = TEST_DATA_LINE.search(prefix)
                    if match and (ended or match.end() < len(prefix)):
                        found = True
                    if ended:
                        prefix = ""
            found = found or bool(TEST_DATA_LINE.search(prefix))
    except (UnicodeDecodeError, OSError):
        return False
    return found


def discover_test_data_artifacts(root: Path) -> set[str]:
    marked: set[str] = set()
    for path in root.rglob("*"):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
            continue
        if _file_is_test_data(path):
            rel = path.relative_to(root).as_posix()
            marked.add(rel)
            marked.add(path.name)
    return marked


def _dict_marked_test_data(value: dict[str, Any]) -> bool:
    for key, item in value.items():
        normalised = _normalise_key(str(key))
        if normalised == "test_data" and item is True:
            return True
        if normalised in {"classification", "data_classification", "record_classification"}:
            if isinstance(item, str) and TEST_DATA_CLASS.search(item):
                return True
    return False


def _string_has_test_data_reference(value: str, marked_artifacts: set[str]) -> bool:
    if TEST_EVIDENCE_URI.search(value):
        return True
    if TEST_DATA_LINE.search(value):
        return True
    stripped = value.strip("`'\" []()")
    for marker in marked_artifacts:
        if marker and (marker in value or stripped == marker):
            return True
    return False


def value_contains_test_data(value: Any, marked_artifacts: set[str]) -> bool:
    if isinstance(value, dict):
        if _dict_marked_test_data(value):
            return True
        return any(value_contains_test_data(item, marked_artifacts) for item in value.values())
    if isinstance(value, list):
        return any(value_contains_test_data(item, marked_artifacts) for item in value)
    if isinstance(value, str):
        return _string_has_test_data_reference(value, marked_artifacts)
    return False


def _scan_json_node(
    node: Any,
    *,
    source: str,
    marked_artifacts: set[str],
    path: tuple[str, ...] = (),
    in_live_authorization: bool = False,
) -> list[Violation]:
    violations: list[Violation] = []
    if isinstance(node, dict):
        for key, value in node.items():
            key_str = str(key)
            child_path = path + (key_str,)
            live_scope = in_live_authorization or _is_live_authorization_key(key_str)
            sensitive = _is_sensitive_key(key_str) or in_live_authorization
            if sensitive and value_contains_test_data(value, marked_artifacts):
                violations.append(
                    Violation(
                        source=source,
                        field_path=".".join(child_path),
                        reason="TEST_DATA is forbidden in live evidence/authorization fields",
                    )
                )
            violations.extend(
                _scan_json_node(
                    value,
                    source=source,
                    marked_artifacts=marked_artifacts,
                    path=child_path,
                    in_live_authorization=live_scope,
                )
            )
    elif isinstance(node, list):
        for index, item in enumerate(node):
            violations.extend(
                _scan_json_node(
                    item,
                    source=source,
                    marked_artifacts=marked_artifacts,
                    path=path + (f"[{index}]",),
                    in_live_authorization=in_live_authorization,
                )
            )
    return violations


def scan_json_file(path: Path, root: Path, marked_artifacts: set[str]) -> list[Violation]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return _scan_json_node(
        payload,
        source=path.relative_to(root).as_posix(),
        marked_artifacts=marked_artifacts,
    )


def scan_markdown_tracker(path: Path, root: Path, marked_artifacts: set[str]) -> list[Violation]:
    if not path.exists():
        return []
    violations: list[Violation] = []
    lines = path.read_text(encoding="utf-8").splitlines()

    headers: list[str] | None = None
    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            normalised = [_normalise_key(cell) for cell in cells]
            if "objective_evidence_recorded" in normalised:
                headers = normalised
                continue
            if headers and len(cells) == len(headers):
                try:
                    index = headers.index("objective_evidence_recorded")
                except ValueError:
                    index = -1
                if index >= 0 and value_contains_test_data(cells[index], marked_artifacts):
                    violations.append(
                        Violation(
                            source=path.relative_to(root).as_posix(),
                            field_path=f"line {line_no}.objective_evidence_recorded",
                            reason="TEST_DATA reference found in canonical tracker evidence cell",
                        )
                    )
                continue
        elif headers and stripped:
            headers = None

        label_match = re.match(r"^\s*(?:[-*]\s*)?\*{0,2}([^:*]+)\*{0,2}:\s*(.*)$", line)
        if label_match:
            label, value = label_match.groups()
            if _is_sensitive_key(label) and value_contains_test_data(value, marked_artifacts):
                violations.append(
                    Violation(
                        source=path.relative_to(root).as_posix(),
                        field_path=f"line {line_no}.{_normalise_key(label)}",
                        reason="TEST_DATA reference found in signed approval/live authorization field",
                    )
                )
    return violations


def iter_json_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*.json"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def scan_repository(root: Path) -> list[Violation]:
    root = root.resolve()
    marked_artifacts = discover_test_data_artifacts(root)
    violations: list[Violation] = []
    for path in iter_json_files(root):
        violations.extend(scan_json_file(path, root, marked_artifacts))
    violations.extend(scan_markdown_tracker(root / "docs" / "TRACKER.md", root, marked_artifacts))
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail CI if TEST_DATA contaminates live evidence or authorization fields."
    )
    parser.add_argument("--root", default=".", help="Repository root")
    args = parser.parse_args(argv)

    root = Path(args.root)
    violations = scan_repository(root)
    if violations:
        print("TEST_DATA LIVE-EVIDENCE GUARD: FAIL")
        for violation in violations:
            print(f"- {violation.render()}")
        return 1

    print("TEST_DATA LIVE-EVIDENCE GUARD: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
