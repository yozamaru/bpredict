#!/usr/bin/env python3
"""スナップショットを SQLite から再生成する。**復旧手段であり、日常の学習経路ではない**。

日常の書き出しは `daily_ingest` が D1 へ書くのと同じデータから行う（基本設計 2.2）。
このスクリプトは、スナップショットが壊れた・失われたときに、
`wrangler d1 export` などで得た SQLite から作り直すために使う。

    python scripts/rebuild_snapshot.py --database local.sqlite --out batch/snapshot
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from batch.features.dataset import export_sqlite, verify_snapshot, write_snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path, help="読み込む SQLite ファイル")
    parser.add_argument("--out", default=REPO_ROOT / "batch" / "snapshot", type=Path)
    args = parser.parse_args(argv)

    if not args.database.exists():
        print("指定された SQLite ファイルがない", file=sys.stderr)
        return 1

    connection = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True)
    try:
        manifest = write_snapshot(export_sqlite(connection), args.out)
    finally:
        connection.close()

    verify_snapshot(args.out)
    files = manifest["files"]
    assert isinstance(files, dict)
    total = sum(int(entry["rows"]) for entry in files.values())
    print(f"rebuild_snapshot: {len(files)} テーブル / {total} 行を書き出した")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
