#!/usr/bin/env python3
"""
Sync resolved versions from `uv.lock` into `pyproject.toml` dependencies.

Default behavior is a dry-run that prints proposed changes. Use `--apply`
to write changes (a backup of `pyproject.toml` is created). Use `--pin`
to pin exact versions (`==`) instead of setting minimums (`>=`).

Examples:
  python scripts/sync_uv_lock_to_pyproject.py --dry-run
  python scripts/sync_uv_lock_to_pyproject.py --apply --pin
"""

from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import sys
import time
from typing import Dict, List, Tuple


def load_uv_lock(path: pathlib.Path) -> Dict[str, str]:
    """Load `uv.lock` (TOML) and return mapping name -> version.

    Names are normalized to lowercase with '_' -> '-' to improve matching.
    """
    try:
        import tomllib
    except Exception as exc:  # pragma: no cover - environment safety
        raise RuntimeError("Python 3.11+ is required to use tomllib") from exc

    raw = path.read_bytes()
    data = tomllib.loads(raw.decode("utf-8"))
    pkgs = data.get("package") or data.get("packages") or []
    mapping: Dict[str, str] = {}
    for pkg in pkgs:
        name = pkg.get("name")
        version = pkg.get("version")
        if name and version:
            key = name.lower().replace("_", "-")
            mapping[key] = str(version)
    return mapping


def find_dependencies_slice(text: str) -> Tuple[int, int, str]:
    """Find the slice indexes of the first `dependencies = [ ... ]` array.

    Returns (start_index_of_bracket, end_index_of_bracket, inner_content).
    """
    m = re.search(r"(?m)^[ \t]*dependencies\s*=\s*\[", text)
    if not m:
        raise RuntimeError("Could not find 'dependencies = [' in pyproject.toml")
    start_bracket = text.find("[", m.start())
    i = start_bracket
    depth = 0
    end_bracket = -1
    while i < len(text):
        c = text[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                end_bracket = i
                break
        i += 1
    if end_bracket == -1:
        raise RuntimeError("Unbalanced brackets in pyproject.toml dependencies array")
    content = text[start_bracket + 1 : end_bracket]
    return start_bracket, end_bracket, content


def parse_dep_entries(content: str) -> List[Tuple[str, str]]:
    """Parse quoted strings inside the dependencies array.

    Returns list of tuples (quote_char, inner_value).
    """
    pattern = re.compile(r"(?s)([\"'])(.*?)(?<!\\)\1")
    entries: List[Tuple[str, str]] = []
    for m in pattern.finditer(content):
        quote = m.group(1)
        val = m.group(2).strip()
        if val == "":
            continue
        entries.append((quote, val))
    return entries


def build_new_entry(val: str, lock_map: Dict[str, str], pin: bool) -> Tuple[str, str]:
    """Given a dependency value (PEP 508-ish), return (old, new).

    If no change is applicable, returns (old, old).
    """
    old = val
    parts = val.split(";", 1)
    req = parts[0].strip()
    marker = ";" + parts[1].strip() if len(parts) > 1 else ""

    m = re.match(r"^\s*([A-Za-z0-9_.\-]+)(\[[^\]]+\])?", req)
    if not m:
        return old, old
    name = m.group(1)
    extras = m.group(2) or ""
    key = name.lower().replace("_", "-")
    resolved = lock_map.get(key)
    if not resolved:
        return old, old
    if pin:
        new_req = f"{name}{extras}=={resolved}"
    else:
        new_req = f"{name}{extras}>={resolved}"
    new_val = new_req + (" " + marker.lstrip() if marker else "")
    return old, new_val


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sync uv.lock resolved versions into pyproject.toml dependencies"
    )
    parser.add_argument("--uv-lock", default="uv.lock", help="path to uv.lock")
    parser.add_argument("--pyproject", default="pyproject.toml", help="path to pyproject.toml")
    parser.add_argument("--apply", action="store_true", help="Write changes to pyproject.toml")
    parser.add_argument("--pin", action="store_true", help="Pin to exact versions (==) instead of >=")
    parser.add_argument("--packages", nargs="*", help="Limit sync to these package names (case-insensitive)")
    args = parser.parse_args(argv)

    path_lock = pathlib.Path(args.uv_lock)
    path_py = pathlib.Path(args.pyproject)
    if not path_lock.exists():
        print(f"uv.lock not found at {path_lock}", file=sys.stderr)
        return 2
    if not path_py.exists():
        print(f"pyproject.toml not found at {path_py}", file=sys.stderr)
        return 2

    lock_map = load_uv_lock(path_lock)
    text = path_py.read_text(encoding="utf-8")
    start, end, content = find_dependencies_slice(text)
    entries = parse_dep_entries(content)
    if not entries:
        print("No dependency entries found in pyproject.toml", file=sys.stderr)
        return 1

    m_indent = re.search(r"\n([ \t]*)[\"']", content)
    indent = m_indent.group(1) if m_indent else "    "

    package_filter = [p.lower().replace("_", "-") for p in args.packages] if args.packages else None

    changes: List[Tuple[str, str]] = []
    new_values: List[Tuple[str, str]] = []
    for quote, val in entries:
        old, new = build_new_entry(val, lock_map, args.pin)
        if package_filter:
            nm = re.match(r"^\s*([A-Za-z0-9_.\-]+)", val)
            if nm:
                if nm.group(1).lower().replace("_", "-") not in package_filter:
                    new = old
        changes.append((old, new))
        new_values.append((quote, new))

    any_change = any(a != b for a, b in changes)
    if not any_change:
        print("No changes proposed.")
        return 0

    # show diffs
    for old, new in changes:
        if old != new:
            print(f"- {old}\n+ {new}\n")

    if not args.apply:
        print("Dry-run: no files modified. Re-run with --apply to write changes.")
        return 0

    # create backup
    ts = int(time.time())
    backup_path = path_py.with_name(path_py.name + f".bak.{ts}")
    shutil.copyfile(path_py, backup_path)
    print(f"Backup written to {backup_path}")

    # build new dependencies block
    lines: List[str] = []
    for quote, val in new_values:
        lines.append(f"{indent}{quote}{val}{quote},")
    new_block = "[\n" + "\n".join(lines) + "\n]"

    new_text = text[:start] + new_block + text[end + 1 :]
    path_py.write_text(new_text, encoding="utf-8")
    print(f"Wrote updates to {path_py}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
