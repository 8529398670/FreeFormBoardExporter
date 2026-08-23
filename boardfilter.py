#!/usr/bin/env python3
"""Decide which boards are served and which are held back.

Reads an ignore file and the board list from `freeform.py list --json`, and
says which boards belong in the served export and which belong in the private
one. Ignored boards are still exported and still get their viewer — they are
simply written somewhere the container never sees.

    python3 freeform.py list --json | python3 boardfilter.py .boardignore --allowed
    python3 freeform.py list --json | python3 boardfilter.py .boardignore --report
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys

GLOB_CHARS = "*?["


def read_patterns(path):
    """One board name per line. Blank lines and whole-line comments are out.

    Only a line whose first non-space character is '#' is a comment: a '#'
    later in the line is part of the board's name, because board names are
    written by people and people use '#'.
    """
    patterns = []
    if not path or not os.path.isfile(path):
        return patterns
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            patterns.append(line)
    return patterns


def matches(pattern, board):
    """A name matches exactly, a glob matches loosely, an id matches by prefix.

    Plain names are deliberately exact. Substring matching would mean that
    ignoring 'MCAT' silently swallowed 'MCAT - TEMP1' as well, and the whole
    point of this file is that nothing leaks by surprise. Ask for a glob when
    you want one.
    """
    title = (board.get("title") or "").strip().lower()
    uuid = (board.get("uuid") or "").lower()
    needle = pattern.strip().lower()

    if any(ch in pattern for ch in GLOB_CHARS):
        return fnmatch.fnmatch(title, needle)
    if needle == title:
        return True
    return len(needle) >= 8 and uuid.startswith(needle)


def selects(pattern, board):
    """The exporter's own rule, so --select behaves exactly like --board.

    That one matches a title fragment or an id prefix, which is looser than
    the ignore file on purpose: `--board MCAT` is a request to grab the MCAT
    boards, while a line reading MCAT in the ignore file is a statement about
    one board in particular.
    """
    needle = pattern.strip().lower()
    title = (board.get("title") or "").lower()
    uuid = (board.get("uuid") or "").lower()
    return uuid.startswith(needle) or needle in title


def partition(boards, patterns, select=None):
    """Split into (allowed, ignored, unmatched patterns).

    `select` narrows the field first, the way `--board` does for the exporter.
    The ignore list is applied afterwards and always wins, so a board named on
    both lists stays out of the served export.
    """
    all_boards = list(boards)
    if select:
        chosen = []
        for board in boards:
            if any(selects(p, board) for p in select):
                chosen.append(board)
        boards = chosen

    allowed, ignored = [], []
    used = set()
    for board in boards:
        hit = None
        for pattern in patterns:
            if matches(pattern, board):
                hit = pattern
                break
        if hit:
            used.add(hit)
            ignored.append(board)
        else:
            allowed.append(board)

    # Unmatched is judged against every board, not just the selected ones.
    # Otherwise `--board Misc` would report every other ignore entry as a
    # typo, when all it means is that those boards were not asked for.
    unmatched = [p for p in patterns
                 if not any(matches(p, b) for b in all_boards)]
    return allowed, ignored, unmatched


def main():
    parser = argparse.ArgumentParser(
        prog="boardfilter.py",
        description="Split the board list into served and held-back halves.")
    parser.add_argument("ignore_file", nargs="?", default=".boardignore")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--allowed", action="store_true",
                      help="print the id of every board that should be served")
    mode.add_argument("--ignored", action="store_true",
                      help="print the id of every board that should not be")
    mode.add_argument("--report", action="store_true",
                      help="print a readable summary")
    mode.add_argument("--unmatched", action="store_true",
                      help="print ignore entries that matched no board")
    parser.add_argument("--select", action="append", default=[], metavar="PATTERN",
                        help="consider only boards matching this, as --board does")
    args = parser.parse_args()

    try:
        boards = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        sys.exit(f"could not read the board list: {exc}")
    if isinstance(boards, dict):
        boards = boards.get("boards", [])
    boards = [b for b in boards if not b.get("deleted")]

    patterns = read_patterns(args.ignore_file)
    allowed, ignored, unmatched = partition(boards, patterns, args.select)

    if args.allowed:
        for board in allowed:
            print(board["uuid"])
    elif args.ignored:
        for board in ignored:
            print(board["uuid"])
    elif args.unmatched:
        for pattern in unmatched:
            print(pattern)
    else:
        print(f"  ignore file: {args.ignore_file}"
              f"{'' if os.path.isfile(args.ignore_file) else '  (none yet)'}")
        print(f"  {len(allowed)} served, {len(ignored)} held back")
        for board in ignored:
            print(f"    held back: {board['title']}")
        for pattern in unmatched:
            print(f"    matches nothing: {pattern}")


if __name__ == "__main__":
    main()
