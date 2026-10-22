"""Fail if an SDK's tests do not cover every case in CASES.md.

    python3 check.py ../surfra-rust/tests
"""

import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
CASES = re.findall(r"^\| ([a-z]\d\d) \|", (HERE / "CASES.md").read_text(), re.M)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    text = "\n".join(path.read_text(errors="ignore") for path in Path(sys.argv[1]).rglob("*") if path.is_file())
    missing = [case for case in CASES if not re.search(rf"\b{case}_", text, re.I)]
    if missing:
        print(f"{sys.argv[1]} is missing cases: {', '.join(missing)}")
        return 1
    print(f"{sys.argv[1]} covers all {len(CASES)} cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
