#!/usr/bin/env python3
"""Explicit schema upgrade. Existing databases are never migrated by reads or serve."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from goal import ROOT, _sibling


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=ROOT / 'data' / 'snowball.db')
    parser.add_argument('--bundle-root', type=Path, help='Destination for the pre-migration verified backup bundle')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = _sibling('orchestrator').Store.migrate(args.db, args.bundle_root)
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps({'error': 'migration_blocked', 'kind': type(exc).__name__, 'detail': str(exc)}))
        return 2
    print(json.dumps(result, sort_keys=True, indent=None if args.json else 2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
