"""Write the verb matrix from the registry (task 000661).

    python scripts/capabilities.py                 # docs/CAPABILITIES.md
    python scripts/capabilities.py --docs PATH     # and the docs site's page

The matrix in docs/CAPABILITIES.md sits between its verbs markers; the
docs site's page (mechbench-docs content/verbs.md) is written whole.
tests/test_parity.py fails when the first is stale.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mechbench_runner import capabilities  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--docs", metavar="PATH", help="Also write the docs site's page here."
    )
    ns = ap.parse_args()
    doc = ROOT / "docs" / "CAPABILITIES.md"
    doc.write_text(capabilities.splice(doc.read_text()))
    print(f"wrote {doc}")
    if ns.docs:
        pathlib.Path(ns.docs).write_text(capabilities.docs_page())
        print(f"wrote {ns.docs}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
