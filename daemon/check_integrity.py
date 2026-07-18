#!/usr/bin/env python3
"""Verify files named by the Achievement Box release manifest."""

from __future__ import annotations

import argparse

from achievementbox.integrity import verify_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--shipped-only", action="store_true",
        help="skip external components such as the user-supplied edlink.exe")
    args = parser.parse_args()

    try:
        results = verify_manifest(shipped_only=args.shipped_only)
    except (OSError, ValueError) as error:
        print(f"INTEGRITY ERROR: {error}")
        return 1

    failed = False
    for result in results:
        print(f"{result.status.upper():8} {result.name}: {result.path}")
        if not result.ok:
            failed = True
            print(f"         expected {result.expected}")
            print(f"         actual   {result.actual or '-'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
