from __future__ import annotations

import re
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "townlight_311"
PATTERN = re.compile(r"^\s*(?:from|import)\s+(civiccore|civic311)\b", re.MULTILINE)


def main() -> int:
    if not SOURCE_ROOT.is_dir():
        print(f"LEGACY-IMPORT-CHECK: FAILED (source root not found: {SOURCE_ROOT})")
        return 1

    failures: list[str] = []
    scanned = 0
    for path in SOURCE_ROOT.rglob("*.py"):
        scanned += 1
        text = path.read_text(encoding="utf-8")
        for match in PATTERN.finditer(text):
            legacy_module = match.group(1)
            failures.append(
                f"{path}: legacy import of {legacy_module} is not allowed. "
                "Use townlight_core instead."
            )

    if scanned == 0:
        print(f"LEGACY-IMPORT-CHECK: FAILED (no Python files scanned in {SOURCE_ROOT})")
        return 1

    if failures:
        print("LEGACY-IMPORT-CHECK: FAILED")
        for failure in failures:
            print(failure)
        return 1

    print(f"LEGACY-IMPORT-CHECK: PASSED ({scanned} source files scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
