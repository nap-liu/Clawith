#!/usr/bin/env python3
"""Compare selected top-level Python symbols before and after a module move."""

from __future__ import annotations

import argparse
import ast
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _source_at_revision(revision: str, path: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={REPO_ROOT}",
            "show",
            f"{revision}:{path}",
        ],
        cwd=REPO_ROOT,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return result.stdout


def _top_level_symbols(source: str, filename: str) -> dict[str, ast.AST]:
    symbols: dict[str, ast.AST] = {}
    for node in ast.parse(source, filename=filename).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    symbols[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            symbols[node.target.id] = node.value
    return symbols


def _normalized(node: ast.AST) -> str:
    return ast.dump(node, annotate_fields=True, include_attributes=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("symbols", nargs="+")
    args = parser.parse_args()

    before_source = _source_at_revision(args.revision, args.before)
    after_source = (REPO_ROOT / args.after).read_text(encoding="utf-8")
    before_symbols = _top_level_symbols(before_source, args.before)
    after_symbols = _top_level_symbols(after_source, args.after)

    failures: list[str] = []
    for symbol in args.symbols:
        before_node = before_symbols.get(symbol)
        after_node = after_symbols.get(symbol)
        if before_node is None:
            failures.append(f"{symbol}: missing from {args.revision}:{args.before}")
        elif after_node is None:
            failures.append(f"{symbol}: missing from {args.after}")
        elif _normalized(before_node) != _normalized(after_node):
            failures.append(f"{symbol}: AST differs after move")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print(f"PASS: {len(args.symbols)} symbol ASTs are unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
