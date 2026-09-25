"""過去シーズンの一括取得（工程6 / 詳細設計 4.8）。**再開可能にする。**

再開可能にしないと、55分経過時点で 429 を食らった際に翌日また全ページを取り直す
ことになり、相手サイトへの負荷を二重にかける。

    python -m batch.jobs.backfill --season 2016-17-B1 [--limit 3] [--dry-run]

失敗時の挙動（基本設計 4.3）
- 個別試合の取得失敗・値域検証エラー … その試合をスキップしてジョブを継続する
- パースエラーが連続3件 … ジョブを中止し `PARTIAL` + exit 1
- 429 / 503 … 取得区間のみ中止する。内部処理（ログ記録）は継続する
- `robots.txt` / 利用規約の変更 … 取得前に中止し `ABORTED` + exit 1
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

from batch.jobs.seed_master import load_club_source_ids, load_seasons
from batch.loader.api import InternalApi, LoaderError
from batch.loader.payload import SeasonRef, games_payload, series_numbers, stats_payload
from batch.parser.boxscore_parser import parse_boxscore
from batch.parser.errors import (
    DataUnavailable,
    ParseError,
    ParseErrorStreak,
    ParseFailureTracker,
    ValidationError,
)
from batch.parser.schedule_parser import ScheduleGame, parse_club_options, parse_schedule
from batch.parser.terms import report_terms_change
from batch.scraper.boxscore import boxscore_url
from batch.scraper.client import PolicyError, RateLimitedClient, ScrapingStopped
from batch.scraper.schedule import schedule_html_url, schedule_url

#: 取り込むのはリーグ戦とチャンピオンシップだけ（要件 5.3）。
#: **チャンピオンシップ（3）を先に辿る。** `event=2` は「そのシーズンの日程」であり、
#: リーグ戦だけでなく CS・オールスター・国際試合も含む（2016-17 の実データで確認）。
#: 先に CS を確定させ、`event=2` では同じ試合IDを飛ばすことで、
#: `competition` が REGULAR で上書きされるのを防ぎ、取得も重複しない。
EVENTS = (3, 2)
DEFAULT_STATE_PATH = Path("batch/.scraper-state/state.json")


@dataclass
class Result:
    status: str = "SUCCESS"
    ingested: int = 0
    skipped_existing: int = 0
    skipped_unfinished: int = 0
    skipped_invalid: int = 0
    #: 日程に混ざる非リーグ戦（オールスター・国際試合）。件数を必ず表に出す
    skipped_non_league: int = 0
    notes: list[str] = field(default_factory=list)
    #: スキップした試合の (game_id, 例外の型名, 自前メッセージ)。
    #: **件数だけでは調査ができない**（詳細設計 4.4）。2016-17 で9試合が落ちたとき、
    #: どれがなぜ落ちたかを知るために実サイトへ約80件の再取得が必要になった。
    skipped: list[tuple[str, str, str]] = field(default_factory=list)

    def skip(self, game_id: str, error: Exception) -> None:
        """例外オブジェクトは残さない。型名と自前メッセージだけにする（絶対ルール4）。"""
        self.skipped.append((game_id, type(error).__name__, str(error)))

    def degrade(self, status: str, note: str) -> None:
        self.status = status
        self.notes.append(note)


def _season(season_id: str) -> tuple[SeasonRef, int]:
    for season in load_seasons():
        if season.id == season_id:
            return SeasonRef(season.id, season.label, season.league), int(season.label[:4])
    raise LoaderError(f"シーズンが seasons.csv にない: {season_id}")


def _schedule_pages(
    client: RateLimitedClient,
    year: int,
    event: int,
    clubs_by_name: dict[str, str],
    result: Result,
) -> Iterator[ScheduleGame]:
    """終端まで日程ページを辿る。空の `topics` と `index=null` が終端。"""
    index = 0
    previous_date: str | None = None
    while True:
        page = parse_schedule(
            client.get(schedule_url(year, event, index)),
            year=year,
            event=event,
            clubs_by_name=clubs_by_name,
            previous_date=previous_date,
            index=index,
        )
        yield from page.games
        # 非リーグ戦を黙って捨てない。件数を集計して出力に出す
        result.skipped_non_league += page.skipped
        previous_date = page.last_date
        if page.next_index is None:
            return
        index = page.next_index


def run(
    season_id: str,
    *,
    client: RateLimitedClient,
    api: InternalApi,
    limit: int | None = None,
) -> Result:
    season, year = _season(season_id)
    club_ids = {row.source_id: row.club_id for row in load_club_source_ids()}
    result = Result()

    # 取得前確認。変更があればここで止める（CLAUDE.md 絶対ルール6）。
    # 規約が変わった場合は**どの節が変わったか**を出す。関門が「形骸化した通知」に
    # ならないよう、確認の手間を下げる（詳細設計 4.3）
    client.verify_policy(
        terms_reporter=report_terms_change(os.environ.get("SCRAPER_TERMS_SHA256", "")),
    )

    collected: list[tuple[ScheduleGame, int]] = []
    try:
        clubs_by_name = parse_club_options(client.get(schedule_html_url(year)))
        seen: set[str] = set()
        for event in EVENTS:
            for game in _schedule_pages(client, year, event, clubs_by_name, result):
                if game.game_id in seen:
                    # CS は `event=2` にも現れる。先に確定した PLAYOFF を保つ
                    continue
                seen.add(game.game_id)
                collected.append((game, event))
    except ScrapingStopped:
        result.degrade("PARTIAL", "429/503 により日程の取得を中止した")
        return _finish(api, season_id, result)
    except (ParseError, ValidationError) as error:
        result.degrade("PARTIAL", f"日程の解析に失敗した（{type(error).__name__}）")
        return _finish(api, season_id, result)

    short_names = {source_id: name for name, source_id in clubs_by_name.items()}
    series = series_numbers(
        [(g.game_id, g.game_date, g.home_source_id, g.away_source_id) for g, _ in collected]
    )
    done = api.ingested_game_ids(season_id)
    tracker = ParseFailureTracker()

    for game, event in collected:
        if game.game_id in done:
            result.skipped_existing += 1
            continue
        if game.status != "FINISHED":
            # 未実施・中止・延期は試合詳細に記録がない。日次取り込みが扱う
            result.skipped_unfinished += 1
            continue
        if limit is not None and result.ingested >= limit:
            break
        try:
            _ingest_one(client, api, game, event, season, club_ids, short_names,
                        series.get(game.game_id))
        except ScrapingStopped:
            result.degrade("PARTIAL", "429/503 により取得区間を中止した")
            break
        except ValidationError as error:
            # 値域・恒等式の違反は当該試合をスキップする（異常値を Elo に流さない）
            result.skipped_invalid += 1
            result.skip(game.game_id, error)
            tracker.success()
            continue
        except DataUnavailable as error:
            result.skipped_unfinished += 1
            result.skip(game.game_id, error)
            tracker.success()
            continue
        except ParseError as error:
            result.skip(game.game_id, error)
            try:
                tracker.failure()
            except ParseErrorStreak:
                result.degrade("PARTIAL", "パース失敗が連続3件。構造変更の可能性")
                break
            result.skipped_invalid += 1
            continue
        tracker.success()
        result.ingested += 1

    return _finish(api, season_id, result)


def _ingest_one(
    client: RateLimitedClient,
    api: InternalApi,
    game: ScheduleGame,
    event: int,
    season: SeasonRef,
    club_ids: dict[str, str],
    short_names: dict[str, str],
    series_game_no: int | None,
) -> None:
    url = boxscore_url(game.game_id)
    box = parse_boxscore(
        client.get(url), event=event, clubs=club_ids, expected_game_id=game.game_id
    )
    fetched_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    api.post(
        "games",
        games_payload(
            box,
            season=season,
            club_ids=club_ids,
            short_names=short_names,
            series_game_no=series_game_no,
            source_url=url,
            fetched_at=fetched_at,
        ),
    )
    api.post("stats", stats_payload(box, club_ids=club_ids, fetched_at=fetched_at))


def _finish(api: InternalApi, season_id: str, result: Result) -> Result:
    """`ingestion_logs` に結果を記録する。例外の本文は入れない（絶対ルール4）。"""
    try:
        api.post(
            "log",
            {
                "id": f"backfill-{season_id}-{uuid.uuid4().hex[:8]}",
                "job": "backfill",
                "startedAt": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "finishedAt": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "status": result.status,
                "rowsAffected": result.ingested,
            },
        )
    except LoaderError:
        result.notes.append("ログの記録に失敗した")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="過去シーズンの一括取得（1日1シーズン）")
    parser.add_argument("--season", required=True, help="seasons.csv の id（例: 2016-17-B1）")
    parser.add_argument("--limit", type=int, default=None, help="取り込む試合数の上限（確認用）")
    parser.add_argument("--dry-run", action="store_true", help="取得はするが D1 には書かない")
    args = parser.parse_args(argv)

    try:
        client = RateLimitedClient(
            user_agent=os.environ.get("SCRAPER_USER_AGENT", ""),
            state_path=Path(os.environ.get("SCRAPER_STATE_PATH", str(DEFAULT_STATE_PATH))),
            robots_sha256=os.environ.get("SCRAPER_ROBOTS_SHA256") or None,
            terms_sha256=os.environ.get("SCRAPER_TERMS_SHA256") or None,
        )
        api = InternalApi(
            os.environ.get("API_BASE_URL", ""),
            os.environ.get("INGEST_TOKEN", ""),
            dry_run=args.dry_run,
        )
        result = run(args.season, client=client, api=api, limit=args.limit)
    except PolicyError:
        print("backfill: 取得前確認に失敗した（robots / 利用規約）", file=sys.stderr)
        return 1
    except LoaderError as error:
        # **`LoaderError` のメッセージは出す。** この例外は設計上、URL のクエリ文字列も
        # 応答本文もトークンも含まない（`batch/loader/api.py`）。型名だけにすると
        # 「4xx なのか 5xx なのか、どの口なのか」が分からず、原因の切り分けに
        # 本番の再実行が要る。実際に2度それが起きた。
        print(f"backfill: 失敗（{type(error).__name__}: {error}）", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 — 公開ログの境界で本文を除去する
        # 想定外の例外は型名のみ。本文に何が入るか保証できない（絶対ルール4）
        print(f"backfill: 失敗（{type(error).__name__}）", file=sys.stderr)
        return 1

    print(
        f"backfill: {result.status} 取り込み={result.ingested} 既取得={result.skipped_existing}"
        f" 未実施={result.skipped_unfinished} 不正={result.skipped_invalid}"
        f" 非リーグ戦={result.skipped_non_league}"
    )
    for note in result.notes:
        print(f"  - {note}")
    # **スキップした試合は1行1件で出す。** 件数だけでは、どの試合がなぜ落ちたかを
    # 調べるために実サイトへ再取得することになる（詳細設計 4.4）
    for game_id, kind, message in result.skipped:
        print(f"  skip {game_id} {kind} {message}")
    # PARTIAL を exit 0 で終えない（失敗通知に乗らず放置される。基本設計 4.3）
    return 1 if result.status != "SUCCESS" else 0


if __name__ == "__main__":
    raise SystemExit(main())
