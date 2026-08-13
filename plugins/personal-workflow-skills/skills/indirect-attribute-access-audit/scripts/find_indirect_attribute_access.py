#!/usr/bin/env python3
"""Find indirect Python attribute access that hides interface drift."""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final, Iterable, Sequence


DISALLOWED_CALLS: Final[frozenset[str]] = frozenset({"getattr", "setattr", "hasattr"})
DEFAULT_EXCLUDED_DIRS: Final[frozenset[str]] = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "site-packages",
        "venv",
    }
)


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    line: int
    column: int
    call: str
    severity: str
    message: str
    source: str


@dataclass(frozen=True, slots=True)
class ParseError:
    path: str
    line: int
    message: str


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scan Python files for getattr/setattr/hasattr calls that should be "
            "replaced with direct typed interface access."
        )
    )
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="Files or directories to scan.",
    )
    parser.add_argument(
        "--exclude-dir",
        action="append",
        default=[],
        help="Directory name to skip. Can be passed multiple times.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of text.",
    )
    parser.add_argument(
        "--fail-on-findings",
        action="store_true",
        help="Exit with status 1 when any finding is detected.",
    )
    return parser.parse_args(argv)


def iter_python_files(paths: Sequence[Path], excluded_dirs: frozenset[str]) -> Iterable[Path]:
    for path in paths:
        if path.is_file() and path.suffix == ".py":
            yield path
            continue
        if not path.is_dir():
            continue
        for child in path.rglob("*.py"):
            if excluded_dirs.intersection(child.parts):
                continue
            yield child


def call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name) and node.id in DISALLOWED_CALLS:
        return node.id
    if isinstance(node, ast.Attribute) and node.attr in DISALLOWED_CALLS:
        return node.attr
    return None


def severity_for(call: str, node: ast.Call) -> str:
    if call == "setattr":
        return "high"
    if call == "getattr" and len(node.args) >= 3:
        return "high"
    return "medium"


def message_for(call: str, node: ast.Call) -> str:
    if call == "setattr":
        return "setattr hides write-side interface drift; assign through a concrete typed attribute."
    if call == "getattr" and len(node.args) >= 3:
        return "getattr with a default silently tolerates missing interface members."
    if call == "getattr":
        return "getattr hides read-side interface drift; access the expected attribute directly."
    return "hasattr turns interface drift into branch behavior; use a concrete type or protocol."


def source_line(lines: Sequence[str], line_number: int) -> str:
    if line_number < 1 or line_number > len(lines):
        return ""
    return lines[line_number - 1].strip()


def scan_file(path: Path, root: Path) -> tuple[list[Finding], ParseError | None]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        line = exc.lineno or 0
        return [], ParseError(
            path=str(path.relative_to(root)),
            line=line,
            message=exc.msg,
        )

    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call = call_name(node.func)
        if call is None:
            continue
        findings.append(
            Finding(
                path=str(path.relative_to(root)),
                line=node.lineno,
                column=node.col_offset + 1,
                call=call,
                severity=severity_for(call, node),
                message=message_for(call, node),
                source=source_line(lines, node.lineno),
            )
        )
    return findings, None


def common_root(paths: Sequence[Path]) -> Path:
    resolved = [path.resolve() for path in paths]
    if len(resolved) == 1:
        path = resolved[0]
        return path.parent if path.is_file() else path
    common_parts = resolved[0].parts
    for path in resolved[1:]:
        limit = min(len(common_parts), len(path.parts))
        index = 0
        while index < limit and common_parts[index] == path.parts[index]:
            index += 1
        common_parts = common_parts[:index]
    return Path(*common_parts) if common_parts else Path.cwd()


def emit_text(findings: Sequence[Finding], parse_errors: Sequence[ParseError]) -> None:
    for finding in findings:
        print(
            f"{finding.path}:{finding.line}:{finding.column}: "
            f"{finding.severity}: {finding.call}: {finding.message}"
        )
        if finding.source:
            print(f"    {finding.source}")
    for error in parse_errors:
        print(f"{error.path}:{error.line}: parse-error: {error.message}", file=sys.stderr)
    print(
        f"Scanned with {len(findings)} finding(s) and {len(parse_errors)} parse error(s).",
        file=sys.stderr,
    )


def emit_json(findings: Sequence[Finding], parse_errors: Sequence[ParseError]) -> None:
    payload = {
        "findings": [asdict(finding) for finding in findings],
        "parse_errors": [asdict(error) for error in parse_errors],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    excluded_dirs = DEFAULT_EXCLUDED_DIRS.union(args.exclude_dir)
    root = common_root(args.paths).resolve()
    findings: list[Finding] = []
    parse_errors: list[ParseError] = []

    for path in sorted(iter_python_files(args.paths, excluded_dirs)):
        file_findings, parse_error = scan_file(path.resolve(), root)
        findings.extend(file_findings)
        if parse_error is not None:
            parse_errors.append(parse_error)

    findings.sort(key=lambda finding: (finding.path, finding.line, finding.column))

    if args.json:
        emit_json(findings, parse_errors)
    else:
        emit_text(findings, parse_errors)

    if parse_errors:
        return 2
    if args.fail_on_findings and findings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
