"""会場の名称・収容人数の履歴を構築して書き出す（工程6 / 詳細設計 4.9）。

    python -m batch.jobs.build_venue_revisions [--dry-run]

**入力はスナップショットと手入力の CSV だけ。** D1 を入力として読まない
（CLAUDE.md 絶対ルール3）。`recompute_ratings` と同じく**全期間を再計算して洗い替える**。

自動で解決せず報告するもの（詳細設計 4.9）。

- CSV の `(venue_id, valid_from)` が名称区間に一致しない … **中止**（exit 1）
- 同一シーズン内での名称変更 … 表記ゆれの疑いとして**報告**（ジョブは継続）
- 区間が1つもない会場 … `venues.name` へのフォールバックとして**報告**
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from batch.features.dataset import Dataset, load_snapshot, write_snapshot
from batch.loader.api import InternalApi, LoaderError
from batch.loader.limits import max_rows_per_request
from batch.masters.venue_revisions import (
    BuildResult,
    Revision,
    VenueRevisionError,
    build,
)

DEFAULT_SNAPSHOT = Path("batch/snapshot")
#: 5列 → floor(100/5)=20 行/文 × 40 = 800（詳細設計 3.4）
ROWS_PER_REQUEST = max_rows_per_request("venue_revisions")


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _payload(revisions: list[Revision]) -> dict[str, object]:
    """1リクエストで送る。**会場を2リクエストに分けない**（期間 DELETE が前の行を消す）。

    全会場でも数百行に収まるため分割は要らない。上限を超えたら分割せずに失敗させる
    （黙って分けると履歴が壊れる）。
    """
    return {
        "fromDate": min(r.valid_from for r in revisions),
        "toDate": max(r.valid_from for r in revisions),
        "revisions": [
            {
                "venueId": r.venue_id,
                "validFrom": r.valid_from,
                "validTo": r.valid_to,
                "name": r.name,
                "capacity": r.capacity,
            }
            for r in revisions
        ],
    }


def _to_frame(revisions: list[Revision]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "venue_id": r.venue_id,
                "valid_from": r.valid_from,
                "valid_to": r.valid_to,
                "name": r.name,
                "capacity": r.capacity,
            }
            for r in revisions
        ],
        columns=["venue_id", "valid_from", "valid_to", "name", "capacity"],
    )


def run(
    *,
    api: InternalApi,
    snapshot_dir: Path = DEFAULT_SNAPSHOT,
) -> BuildResult:
    dataset = load_snapshot(snapshot_dir)
    result = build(dataset.table("games"))
    if not result.revisions:
        return result

    if len(result.revisions) > ROWS_PER_REQUEST:
        raise VenueRevisionError(
            f"1リクエストの上限を超えている: {len(result.revisions)} > {ROWS_PER_REQUEST}"
        )

    # 特徴量が読むのはスナップショット側。**D1 より先に書く**（基本設計 2.2）
    updated = Dataset(
        tables={**dataset.tables, "venue_revisions": _to_frame(result.revisions)}
    )
    write_snapshot(updated, snapshot_dir, tables=["venue_revisions"])

    api.post("venue-revisions", _payload(result.revisions))
    return result


def _log(api: InternalApi, status: str, rows: int) -> None:
    """`ingestion_logs` に記録する。例外の本文は入れない（絶対ルール4）。"""
    try:
        api.post(
            "log",
            {
                "id": f"venue-rev-{uuid.uuid4().hex[:8]}",
                "job": "build_venue_revisions",
                "startedAt": _iso_now(),
                "finishedAt": _iso_now(),
                "status": status,
                "rowsAffected": rows,
            },
        )
    except LoaderError:
        print("  - ログの記録に失敗した")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="会場の履歴を構築して書き出す")
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--dry-run", action="store_true", help="D1 には書かない")
    args = parser.parse_args(argv)

    try:
        api = InternalApi(
            os.environ.get("API_BASE_URL", ""),
            os.environ.get("INGEST_TOKEN", ""),
            dry_run=args.dry_run,
        )
        result = run(api=api, snapshot_dir=args.snapshot)
    except VenueRevisionError as error:
        # 入力の矛盾。**自動で解決しない**（詳細設計 4.9）。本文は自前のメッセージのみ
        print(f"build_venue_revisions: 中止（{error}）", file=sys.stderr)
        return 1
    except LoaderError as error:
        # **`LoaderError` のメッセージは出す。** この例外は設計上、URL のクエリ文字列も
        # 応答本文もトークンも含まない（`batch/loader/api.py`）。型名だけにすると
        # 「4xx なのか 5xx なのか、どの口なのか」が分からず、原因の切り分けに
        # 本番の再実行が要る。実際に2度それが起きた。
        print(f"build_venue_revisions: 失敗（{type(error).__name__}: {error}）", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 — 公開ログの境界で本文を除去する
        # 想定外の例外は型名のみ。本文に何が入るか保証できない（絶対ルール4）
        print(f"build_venue_revisions: 失敗（{type(error).__name__}）", file=sys.stderr)
        return 1

    venues = len({r.venue_id for r in result.revisions})
    with_capacity = sum(1 for r in result.revisions if r.capacity is not None)
    print(
        f"build_venue_revisions: SUCCESS 区間={len(result.revisions)} 会場={venues}"
        f" 収容人数あり={with_capacity}"
    )
    # **黙ってフォールバックしない。** 過去試合が現在名で表示されていることに
    # 気づけなくなる（詳細設計 1.2）
    for venue_id in result.fallback_venue_ids:
        print(f"  - 名称が取れず venues.name にフォールバック: {venue_id}")
    # **同一シーズン内の変更は名寄せしない。** 表記ゆれの規則を推測で作らない
    for note in result.intra_season_changes:
        print(f"  - 同一シーズン内で名称が変わった（表記ゆれの疑い）: {note}")

    _log(api, "SUCCESS", len(result.revisions))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
