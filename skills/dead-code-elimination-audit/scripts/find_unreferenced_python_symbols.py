#!/usr/bin/env python3
"""Find unreferenced top-level Python symbols.

The script reports top-level functions and classes with zero static
references across the provided roots.
"""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

IGNORED_DIRS: Final[frozenset[str]] = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        ".venv",
        "venv",
    }
)


@dataclass(slots=True, frozen=True)
class Symbol:
    """Represent a top-level symbol definition."""

    name: str
    kind: str
    file_path: Path
    line: int
    exported: bool
    decorated: bool


def is_test_path(path: Path) -> bool:
    """Return True when the path is likely part of a pytest suite."""

    if path.name.startswith("test_"):
        return True
    return "tests" in path.parts


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(
        description="Report unreferenced top-level Python functions/classes.",
    )
    parser.add_argument(
        "roots",
        nargs="+",
        type=Path,
        help="Root directories or files to scan.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output.",
    )
    return parser.parse_args()


def iter_python_files(roots: list[Path]) -> list[Path]:
    """Collect Python files under the provided roots."""

    files: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            files.append(root)
            continue
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if any(part in IGNORED_DIRS for part in path.parts):
                continue
            files.append(path)
    return sorted(set(files))


def extract_exports(module: ast.Module) -> set[str]:
    """Extract names included in __all__ if statically declared."""

    exported: set[str] = set()
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id != "__all__":
            continue
        if isinstance(node.value, (ast.List, ast.Tuple)):
            for elt in node.value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    exported.add(elt.value)
    return exported


def collect_symbols(path: Path) -> list[Symbol]:
    """Collect top-level functions and classes from one module."""

    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    exported = extract_exports(tree)

    symbols: list[Symbol] = []
    in_tests = is_test_path(path)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Pytest discovers these via naming conventions, not imports.
            if in_tests and node.name.startswith("test_"):
                continue
            symbols.append(
                Symbol(
                    name=node.name,
                    kind="function",
                    file_path=path,
                    line=node.lineno,
                    exported=node.name in exported,
                    decorated=bool(node.decorator_list),
                )
            )
            continue
        if isinstance(node, ast.ClassDef):
            # Pytest class-based tests are also discovery-driven.
            if in_tests and node.name.startswith("Test"):
                continue
            symbols.append(
                Symbol(
                    name=node.name,
                    kind="class",
                    file_path=path,
                    line=node.lineno,
                    exported=node.name in exported,
                    decorated=bool(node.decorator_list),
                )
            )
    return symbols


def collect_usage_names(path: Path) -> list[str]:
    """Collect loaded names and attribute names from one module."""

    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            names.append(node.id)
        if isinstance(node, ast.Attribute):
            names.append(node.attr)
    return names


def confidence_for(symbol: Symbol) -> str:
    """Assign a conservative confidence tier for removal."""

    path_text = str(symbol.file_path)
    in_tests = "/tests/" in path_text or path_text.startswith("tests/")
    if symbol.exported:
        return "low"
    if symbol.file_path.name == "__init__.py":
        return "low"
    if symbol.decorated:
        # Decorators often imply registration-driven or framework entrypoints.
        return "low"
    if symbol.name.startswith("_") or in_tests:
        return "high"
    return "medium"


def build_report(paths: list[Path]) -> list[dict[str, object]]:
    """Build candidate report for unreferenced symbols."""

    symbols: list[Symbol] = []
    usage_counts: dict[str, int] = {}

    for path in paths:
        symbols.extend(collect_symbols(path))
        for name in collect_usage_names(path):
            usage_counts[name] = usage_counts.get(name, 0) + 1

    results: list[dict[str, object]] = []
    for symbol in symbols:
        if usage_counts.get(symbol.name, 0) != 0:
            continue
        results.append(
            {
                "name": symbol.name,
                "kind": symbol.kind,
                "file": str(symbol.file_path),
                "line": symbol.line,
                "exported": symbol.exported,
                "confidence": confidence_for(symbol),
            }
        )

    confidence_order = {"high": 0, "medium": 1, "low": 2}
    return sorted(
        results,
        key=lambda row: (
            confidence_order.get(str(row["confidence"]), 99),
            str(row["file"]),
            int(row["line"]),
        ),
    )


def print_table(rows: list[dict[str, object]]) -> None:
    """Print a compact text table."""

    if not rows:
        print("No unreferenced top-level symbols found.")
        return

    print("confidence\tkind\tname\tfile:line\texported")
    for row in rows:
        file_line = f"{row['file']}:{row['line']}"
        print(
            f"{row['confidence']}\t{row['kind']}\t{row['name']}"
            f"\t{file_line}\t{row['exported']}"
        )


def main() -> int:
    """Run CLI."""

    args = parse_args()
    files = iter_python_files(list(args.roots))
    rows = build_report(files)

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
