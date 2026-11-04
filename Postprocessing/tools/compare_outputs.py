#!/usr/bin/env python3
"""Regression check: compare two output folders of run_pipeline.py file by file (SHA-256).

    python tools/compare_outputs.py <baseline Output folder> <new Output folder>

Lists the files that are identical, different, or only in one of the two; the caches are skipped.
Exit code 1 if any file differs or is missing. Needs a baseline run on the same study folder.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

SKIP_DIRS = {"cache", ".cache", "original_logs_cache"}       # caches: rebuilt when needed


def files(root: Path) -> dict[str, Path]:
    return {p.relative_to(root).as_posix(): p for p in root.rglob("*")
            if p.is_file() and not set(p.relative_to(root).parts) & SKIP_DIRS}


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    base, new = files(Path(argv[0])), files(Path(argv[1]))
    common = sorted(set(base) & set(new))
    differ = [k for k in common if digest(base[k]) != digest(new[k])]
    print(f"identical: {len(common) - len(differ)} files")
    for title, names in (("different", differ), ("only in the baseline", sorted(set(base) - set(new))),
                         ("only in the new output", sorted(set(new) - set(base)))):
        print(f"{title}: {len(names)}")
        for name in names:
            print(f"  {name}")
    return 1 if differ or set(base) != set(new) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
