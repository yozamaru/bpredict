"""本拠会場を導出して CSV に固定し、D1 へ送る（詳細設計 1.2 / 4.11）。

    python -m batch.jobs.derive_primary_venues [--dry-run] [--load]

**入力はスナップショットの `games` と手入力の CSV だけ。** D1 を入力として読まない
（CLAUDE.md 絶対ルール3）。

**`--load` を既定にしない。** 「1回だけ確定して CSV に固定する」ため、人（または AI）が
CSV を見てからコミットする。`resolve_venue_geo` と同じ扱いである。

**既存の口を使う。** `POST /internal/games` の `clubSeasons` 配列が
`primaryVenueId` を受ける。新しい口を増やさない（関門を増やすほど、Zod 検証と
認可を通る経路の見落としが生まれる。詳細設計 3.4）。
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from batch.features.dataset import load_snapshot
from batch.loader.api import InternalApi, LoaderError
from batch.loader.limits import max_rows_per_request
from batch.masters.primary_venues import (
    PRIMARY_VENUE_CSV,
    BuildResult,
    PrimaryVenueError,
    derive,
    load_csv,
    merge,
    primary_of,
    write_csv,
)

DEFAULT_SNAPSHOT = Path("batch/snapshot")
#: 8列 → floor(100/8)=12 行/文 × 40 = 480（詳細設計 3.4）
ROWS_PER_REQUEST = max_rows_per_request("club_seasons")


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def build(snapshot_dir: Path = DEFAULT_SNAPSHOT) -> BuildResult:
    """導出して、手入力の行を優先してから返す。"""
    dataset = load_snapshot(snapshot_dir)
    return merge(derive(dataset.table("games")), load_csv())


def _club_season_payload(
    seasons: pd.DataFrame, primary: dict[tuple[str, str], str],
) -> list[dict[str, object]]:
    """`club_seasons` の全行を送る。

    **`primary_venue_id` だけを送る経路はない。** `clubSeasons` の Zod スキーマは
    `name` / `shortName` / `league` を必須にしており（NOT NULL の列である）、
    upsert は与えられた値で上書きする。スナップショットの値をそのまま載せ直す。
    """
    rows: list[dict[str, object]] = []
    for record in seasons.to_dict("records"):
        club_id = str(record["club_id"])
        season_id = str(record["season_id"])
        venue_id = primary.get((season_id, club_id))
        if venue_id is None:
            venue_id = _text(record.get("primary_venue_id"))
        rows.append({
            "clubId": club_id,
            "seasonId": season_id,
            "name": str(record["name"]),
            "shortName": str(record["short_name"]),
            "league": str(record["league"]),
            "primaryVenueId": venue_id,
            "colorPrimary": _text(record.get("color_primary")),
            "colorSecondary": _text(record.get("color_secondary")),
        })
    return rows


def _text(value: object) -> str | None:
    """欠損を None に寄せる。**空文字と欠損を混ぜない。**"""
    if value is None:
        return None
    text = str(value)
    return None if text in ("nan", "NaT", "<NA>", "") else text


def load(
    *, api: InternalApi, snapshot_dir: Path = DEFAULT_SNAPSHOT,
    csv_path: Path = PRIMARY_VENUE_CSV,
) -> int:
    """コミット済みの CSV を D1 へ送る。**導出し直さない。**

    CSV を正とするのは、`--load` の前に人が内容を見ているからである。ここで
    導出をやり直すと、見た内容と送る内容が食い違う余地が生まれる。
    """
    rows = load_csv(csv_path)
    if not rows:
        raise PrimaryVenueError(f"{csv_path} が空である（先に導出する）")
    dataset = load_snapshot(snapshot_dir)
    payload = _club_season_payload(dataset.table("club_seasons"), primary_of(rows))
    for start in range(0, len(payload), ROWS_PER_REQUEST):
        api.post("games", {"clubSeasons": payload[start:start + ROWS_PER_REQUEST]})
    return len(payload)


def _log(api: InternalApi, status: str, rows: int) -> None:
    try:
        api.post("log", {
            "id": f"primary-venue-{uuid.uuid4().hex[:8]}",
            "job": "derive_primary_venues",
            "startedAt": _iso_now(),
            "finishedAt": _iso_now(),
            "status": status,
            "rowsAffected": rows,
        })
    except LoaderError:
        print("  - ログの記録に失敗した")


def _report(result: BuildResult) -> None:
    manual = sum(1 for r in result.rows if r.is_manual)
    print(f"導出: {len(result.rows)}行（うち手入力 {manual}）")
    low = sorted((r for r in result.rows if not r.is_manual), key=lambda r: r.share)[:5]
    if low:
        print("  占有率の低い組（本拠と呼べるか目で見る）:")
        for row in low:
            print(f"    {row.season_id} {row.club_id} -> {row.venue_id}"
                  f" {row.games}試合 / 占有率 {row.share:.3f}")
    if result.skipped:
        print(f"  飛ばした組: {len(result.skipped)}")
    if result.ties:
        print(f"  **同数が起きた組: {len(result.ties)}** -> {result.ties[:5]}")
    for season_id, club_id, kept, derived_id in result.conflicts:
        print(f"  **食い違い**: {season_id} {club_id}"
              f" 手入力 {kept} / 導出 {derived_id}（手入力を採った）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本拠会場を導出して CSV に固定する")
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--csv", type=Path, default=PRIMARY_VENUE_CSV)
    parser.add_argument("--dry-run", action="store_true", help="D1 には書かない")
    parser.add_argument(
        "--load", action="store_true",
        help="導出せず、コミット済みの CSV を D1 へ送る")
    args = parser.parse_args(argv)

    try:
        if args.load:
            # **導出モードでは内部APIを作らない。** `InternalApi` は生成時に
            # `API_BASE_URL` の形式を検証するため、作るだけで落ちる。導出は D1 に
            # 一切触らないのに環境変数を要求するのは誤りである（工程6 で同じ形の
            # 落とし穴を踏んでいる）
            api = InternalApi(
                os.environ.get("API_BASE_URL", ""),
                os.environ.get("INGEST_TOKEN", ""),
                dry_run=args.dry_run,
            )
            rows = load(api=api, snapshot_dir=args.snapshot, csv_path=args.csv)
            print(f"D1 へ送った club_seasons: {rows}行")
            if not args.dry_run:
                _log(api, "SUCCESS", rows)
            return 0
        result = build(args.snapshot)
        _report(result)
        write_csv(result.rows, args.csv)
        print(f"書き出した: {args.csv}")
        print("**内容を確認してからコミットし、--load で D1 へ送る**")
    except PrimaryVenueError as error:
        print(f"中止: {error}", file=sys.stderr)
        return 1
    except LoaderError as error:
        print(f"中止: 内部APIへの書き込みに失敗した: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
