#!/usr/bin/env python3
"""Count comment characters across Python and TypeScript/JavaScript source files."""

import argparse
import ast
import io
import os
import re
import tokenize
from pathlib import Path
from typing import Dict, List, Tuple


def count_python_comment_chars(file_path: Path) -> Tuple[int, int]:
    """Return (comment_chars, total_chars) for a Python file."""
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception:
        return 0, 0

    total_chars = len(content)
    comment_chars = 0

    # 1. Count # comments via tokenize
    try:
        tokens = tokenize.generate_tokens(io.StringIO(content).readline)
        for tok in tokens:
            if tok.type == tokenize.COMMENT:
                comment_chars += len(tok.string)
    except Exception:
        pass

    # 2. Count docstrings via AST
    try:
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                docstring = ast.get_docstring(node, clean=False)
                if docstring:
                    comment_chars += len(docstring)
    except Exception:
        pass

    return comment_chars, total_chars


def count_js_ts_comment_chars(file_path: Path) -> Tuple[int, int]:
    """Return (comment_chars, total_chars) for a TS/JS file."""
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception:
        return 0, 0

    total_chars = len(content)
    comment_chars = 0

    # Match /* ... */ and // ...
    # Simple regex for TS/JS comments
    pattern = re.compile(r"(/\*[\s\S]*?\*/|//[^\r\n]*)")
    for match in pattern.finditer(content):
        comment_chars += len(match.group(0))

    return comment_chars, total_chars


def scan_directory(
    root_dirs: List[Path], top_n: int = 25
) -> List[Tuple[Path, int, int, float]]:
    """Scan directories for source files and return ranked comment metrics."""
    results = []

    ignore_dirs = {
        ".git",
        ".venv",
        "node_modules",
        "dist",
        "build",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }

    for root_dir in root_dirs:
        for dirpath, dirnames, filenames in os.walk(root_dir):
            # Mutate dirnames in place to skip ignored directories
            dirnames[:] = [d for d in dirnames if d not in ignore_dirs]

            for fname in filenames:
                fpath = Path(dirpath) / fname
                ext = fpath.suffix.lower()

                if ext == ".py":
                    c_chars, t_chars = count_python_comment_chars(fpath)
                elif ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
                    c_chars, t_chars = count_js_ts_comment_chars(fpath)
                else:
                    continue

                if t_chars > 0 and c_chars > 0:
                    pct = (c_chars / t_chars) * 100
                    results.append((fpath, c_chars, t_chars, pct))

    results.sort(key=lambda x: x[1], reverse=True)
    return results[:top_n]


def main():
    parser = argparse.ArgumentParser(
        description="Spot files with highest comment character counts."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["python/src", "node/packages", "langchain"],
        help="Root paths to scan (default: python/src, node/packages, langchain)",
    )
    parser.add_argument(
        "-n", "--top", type=int, default=25, help="Number of top files to display"
    )

    args = parser.parse_args()

    root_paths = [Path(p) for p in args.paths if Path(p).exists()]
    if not root_paths:
        print("No valid paths found.")
        return

    top_files = scan_directory(root_paths, top_n=args.top)

    print(
        f"{'Rank':<5} {'Comment Chars':<15} {'Total Chars':<15} {'Comment %':<12} {'Path'}"
    )
    print("=" * 80)
    for idx, (fpath, c_chars, t_chars, pct) in enumerate(top_files, 1):
        rel_path = (
            fpath.relative_to(Path.cwd())
            if fpath.is_relative_to(Path.cwd())
            else fpath
        )
        print(
            f"{idx:<5} {c_chars:<15,d} {t_chars:<15,d} {pct:<11.1f}% {rel_path}"
        )


if __name__ == "__main__":
    main()
