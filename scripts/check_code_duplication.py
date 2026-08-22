#!/usr/bin/env python3
"""Detect duplicate code blocks across Python and TypeScript/JavaScript source files."""

import argparse
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Set, Tuple


def normalize_line(line: str) -> str:
    """Normalize a source line for duplicate detection."""
    # Strip single-line comments
    line = re.sub(r"(#|//).*", "", line)
    # Strip whitespace
    line = line.strip()
    # Normalize quotes
    line = line.replace('"', "'")
    return line


def get_file_lines(file_path: Path) -> List[Tuple[int, str, str]]:
    """Return list of (line_num, raw_line, normalized_line) for non-empty lines."""
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception:
        return []

    lines = []
    for idx, raw in enumerate(content.splitlines(), 1):
        norm = normalize_line(raw)
        if norm and norm not in ("{", "}", ");", "pass", "return", "continue", "break"):
            lines.append((idx, raw, norm))
    return lines


def find_duplicates(
    root_dirs: List[Path], min_lines: int = 6, top_n: int = 20
) -> List[Dict]:
    """Find contiguous duplicate code blocks across scanned source files."""
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

    file_data: Dict[Path, List[Tuple[int, str, str]]] = {}

    for root_dir in root_dirs:
        for dirpath, dirnames, filenames in os.walk(root_dir):
            dirnames[:] = [d for d in dirnames if d not in ignore_dirs]
            for fname in filenames:
                fpath = Path(dirpath) / fname
                ext = fpath.suffix.lower()
                if ext in (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
                    lines = get_file_lines(fpath)
                    if len(lines) >= min_lines:
                        file_data[fpath] = lines

    # Hash sliding windows of length min_lines
    window_map: Dict[Tuple[str, ...], List[Tuple[Path, int]]] = defaultdict(list)

    for fpath, lines in file_data.items():
        for i in range(len(lines) - min_lines + 1):
            window_norms = tuple(lines[i + j][2] for j in range(min_lines))
            start_line_num = lines[i][0]
            window_map[window_norms].append((fpath, start_line_num))

    # Filter windows appearing in multiple places or multiple files
    duplicate_windows = {
        win: locs for win, locs in window_map.items() if len(locs) > 1
    }

    # Group duplicate instances into blocks
    blocks = []
    processed_locs: Set[Tuple[Path, int]] = set()

    for win, locs in duplicate_windows.items():
        if locs[0] in processed_locs:
            continue

        sample_code = "\n".join(win[:5])
        first_loc = locs[0]

        # Calculate unique files
        files = list({loc[0] for loc in locs})

        blocks.append(
            {
                "line_count": len(win),
                "occurrences": len(locs),
                "score": len(win) * len(locs),
                "locations": locs,
                "files": files,
                "sample": sample_code,
            }
        )

    blocks.sort(key=lambda x: x["score"], reverse=True)
    return blocks[:top_n]


def main():
    parser = argparse.ArgumentParser(
        description="Spot duplicate code blocks across source files."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["python/src", "node/packages", "langchain"],
        help="Root paths to scan",
    )
    parser.add_argument(
        "-m",
        "--min-lines",
        type=int,
        default=6,
        help="Minimum block length in lines (default: 6)",
    )
    parser.add_argument(
        "-n", "--top", type=int, default=20, help="Top matches to show"
    )

    args = parser.parse_args()

    root_paths = [Path(p) for p in args.paths if Path(p).exists()]
    if not root_paths:
        print("No valid paths found.")
        return

    duplicates = find_duplicates(
        root_paths, min_lines=args.min_lines, top_n=args.top
    )

    print(
        f"{'Rank':<5} {'Score':<8} {'Lines':<8} {'Occurrences':<13} {'Files'}"
    )
    print("=" * 80)

    for idx, block in enumerate(duplicates, 1):
        file_list = ", ".join(
            [
                str(f.relative_to(Path.cwd())) if f.is_relative_to(Path.cwd()) else str(f)
                for f in block["files"][:3]
            ]
        )
        if len(block["files"]) > 3:
            file_list += f" (+{len(block['files'])-3} more)"

        print(
            f"{idx:<5} {block['score']:<8} {block['line_count']:<8} {block['occurrences']:<13} {file_list}"
        )
        print("    Locations:")
        for fpath, lnum in block["locations"][:5]:
            rel = (
                fpath.relative_to(Path.cwd())
                if fpath.is_relative_to(Path.cwd())
                else fpath
            )
            print(f"      - {rel}:{lnum}")
        print("    Sample:")
        for s_line in block["sample"].splitlines()[:3]:
            print(f"      | {s_line}")
        print("-" * 80)


if __name__ == "__main__":
    main()
