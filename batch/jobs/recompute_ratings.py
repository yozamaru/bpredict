"""Elo の再計算と書き出し（工程6 / 詳細設計 4.2 のステップ7、8.1 の工程6）。

    python -m batch.jobs.recompute_ratings --full
    python -m batch.jobs.recompute_ratings --from-date 2026-03-01

**書き出しの順序を守る**（基本設計 2.2）。

    Elo を再計算（入力はスナップショット）
      → team_ratings.parquet へ書き出し（特徴量生成が読むのはこちら）
      → 同じ値を /internal/ratings 経由で D1 にも送る（公開APIの表示用）

D1 側は表示のための複製であり、**特徴量生成が D1 の `team_ratings` を読むことはない**
（CLAUDE.md 絶対ルール3）。

**計算は常に全期間を replay する。** `--from-date` が絞るのは D1 への書き込み範囲
だけである。途中から始めると開始状態を保存済みの値から拾うことになり、丸め差が
世代を追って蓄積する。8,000試合の replay は数十ミリ秒で終わる。
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from batch.features.dataset import Dataset, load_snapshot, write_snapshot
from batch.loader.api import InternalApi, LoaderError
from batch.loader.limits import max_rows_per_request
from batch.ratings.elo import DEFAULT_PARAMS, EloParams, recompute

DEFAULT_SNAPSHOT = Path("batch/snapshot")
#: `team_ratings` の1リクエスト上限（詳細設計 3.4。8列 → floor(100/8)=12 行/文 × 40）
ROWS_PER_REQUEST = max_rows_per_request("team_ratings")


@dataclass
class Result:
    status: str = "SUCCESS"
    rows: int = 0
    requests: int = 0
    from_date: str = ""
    to_date: str = ""
    notes: list[str] = field(default_factory=list)


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _payload_rows(frame: pd.DataFrame) -> list[dict[str, object]]:
    # `off_rating` / `def_rating` / `pace` は NULL のまま送る。これらを使う特徴量は
    # 検証区分で、集計窓が文書で未定義である（`batch/ratings/elo.py` の recompute）。
    return [
        {
            "clubId": str(row["club_id"]),
            "asOfDate": str(row["as_of_date"]),
            "seasonId": str(row["season_id"]),
            "elo": float(row["elo"]),
            "offRating": None,
            "defRating": None,
            "pace": None,
            "gamesPlayed": int(row["games_played"]),
        }
        for row in frame.to_dict("records")
    ]


def chunks(frame: pd.DataFrame, limit: int = ROWS_PER_REQUEST) -> Iterator[pd.DataFrame]:
    """日付の境界で切る。**同じ `as_of_date` を2リクエストに分けない。**

    `/internal/ratings` は `as_of_date BETWEEN fromDate AND toDate` を DELETE して
    から INSERT する。1つの日付が2リクエストに跨ると、後のリクエストの DELETE が
    前のリクエストで入れた同じ日の行を消す。1日の行数は最大でも 13試合 × 2 = 26 で、
    上限（12行/文 × 40 = 480）に対して十分小さい。
    """
    if frame.empty:
        return
    current: list[pd.DataFrame] = []
    size = 0
    for _, day in frame.groupby("as_of_date", sort=True):
        if size and size + len(day) > limit:
            yield pd.concat(current, ignore_index=True)
            current, size = [], 0
        current.append(day)
        size += len(day)
    if current:
        yield pd.concat(current, ignore_index=True)


def run(
    *,
    api: InternalApi,
    snapshot_dir: Path = DEFAULT_SNAPSHOT,
    from_date: str | None = None,
    params: EloParams = DEFAULT_PARAMS,
) -> Result:
    dataset = load_snapshot(snapshot_dir)
    ratings = recompute(dataset.table("games"), dataset.table("seasons"), params=params)
    result = Result(rows=len(ratings))
    if ratings.empty:
        result.notes.append("結果が確定した試合がないため書き出さない")
        return result

    # 特徴量生成が読むのはこちら。**D1 より先に書く。**
    updated = Dataset(tables={**dataset.tables, "team_ratings": ratings})
    write_snapshot(updated, snapshot_dir, tables=["team_ratings"])

    target = ratings if from_date is None else ratings[ratings["as_of_date"] >= from_date]
    if target.empty:
        result.notes.append("書き込み対象の日付範囲に行がない")
        return result
    result.from_date = str(target["as_of_date"].min())
    result.to_date = str(target["as_of_date"].max())

    for chunk in chunks(target):
        api.post(
            "ratings",
            {
                "fromDate": str(chunk["as_of_date"].min()),
                "toDate": str(chunk["as_of_date"].max()),
                "ratings": _payload_rows(chunk),
            },
        )
        result.requests += 1
    return result


def _log(api: InternalApi, result: Result) -> None:
    """`ingestion_logs` に記録する。例外の本文は入れない（絶対ルール4）。"""
    try:
        api.post(
            "log",
            {
                "id": f"ratings-{uuid.uuid4().hex[:8]}",
                "job": "recompute_ratings",
                "startedAt": _iso_now(),
                "finishedAt": _iso_now(),
                "status": result.status,
                "rowsAffected": result.rows,
            },
        )
    except LoaderError:
        result.notes.append("ログの記録に失敗した")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Elo を再計算して書き出す")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--full", action="store_true", help="全期間を洗い替える")
    group.add_argument("--from-date", help="この日付以降を洗い替える（YYYY-MM-DD）")
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--dry-run", action="store_true", help="D1 には書かない")
    args = parser.parse_args(argv)

    try:
        api = InternalApi(
            os.environ.get("API_BASE_URL", ""),
            os.environ.get("INGEST_TOKEN", ""),
            dry_run=args.dry_run,
        )
        result = run(
            api=api,
            snapshot_dir=args.snapshot,
            from_date=None if args.full else args.from_date,
        )
        _log(api, result)
    except LoaderError as error:
        # **`LoaderError` のメッセージは出す。** この例外は設計上、URL のクエリ文字列も
        # 応答本文もトークンも含まない（`batch/loader/api.py`）。型名だけにすると
        # 「4xx なのか 5xx なのか、どの口なのか」が分からず、原因の切り分けに
        # 本番の再実行が要る。実際に2度それが起きた。
        print(f"recompute_ratings: 失敗（{type(error).__name__}: {error}）", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 — 公開ログの境界で本文を除去する
        # 想定外の例外は型名のみ。本文に何が入るか保証できない（絶対ルール4）
        print(f"recompute_ratings: 失敗（{type(error).__name__}）", file=sys.stderr)
        return 1

    span = f"{result.from_date}〜{result.to_date}" if result.from_date else "なし"
    print(
        f"recompute_ratings: {result.status} 行数={result.rows}"
        f" 書き込み範囲={span} リクエスト={result.requests}"
    )
    for note in result.notes:
        print(f"  - {note}")
    return 1 if result.status != "SUCCESS" else 0


if __name__ == "__main__":
    raise SystemExit(main())
