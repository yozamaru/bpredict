"""日次の取り込み（詳細設計 4.2 / 基本設計 4.2）。

**段階的に作っている。** 設計は12ステップを定めるが、いま実装してあるのは
**未実施の試合の取り込み（ステップ1b）だけ**である。残りは順に足す。

| ステップ | 実装 |
|---|---|
| 0. robots / 利用規約のハッシュ照合 | **あり** |
| 1. 前日の結果取得 | まだ（`backfill` が同じ処理を持つ） |
| **1b. 未実施の試合の取り込み** | **あり** |
| 2. スナップショット更新 | まだ |
| 3〜11（照合・Elo・推論・静的JSON） | まだ |
| 12. `ingestion_logs` | **あり** |

**実装していないステップを黙って飛ばさない。** `--only-upcoming` を必須にして、
**いま何をするジョブなのかを呼び出し側が明示する**。全ステップが揃うまで既定の
動作を持たせない — 「日次ジョブを回したつもりで半分しか動いていない」が最も危ない。

未実施の試合を取り込む理由は 4.2 のステップ1b にある。**`backfill` は `FINISHED`
以外を書き込まない**ため（4.8）、これが無いと「向こう7日間の試合について特徴量を
生成」（ステップ8）の対象が1件も無い。
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from batch.jobs.schedule_walk import walk_schedule
from batch.jobs.seed_master import Season, load_club_source_ids, load_seasons
from batch.loader.api import InternalApi, LoaderError
from batch.loader.limits import max_rows_per_request
from batch.loader.payload import SeasonRef, series_numbers, upcoming_games_payload
from batch.parser.errors import ParseError
from batch.parser.schedule_parser import ScheduleGame, SchedulePage, parse_club_options
from batch.parser.terms import report_terms_change
from batch.scraper.client import (
    PolicyError,
    RateLimitedClient,
    ScraperError,
    ScrapingStopped,
)
from batch.scraper.schedule import schedule_html_url

#: 予測の対象にする窓（詳細設計 4.2 のステップ8「向こう7日間」）。
UPCOMING_DAYS = 7

#: 取り込む大会区分（要件 5.3）。`4` 残留プレーオフ / `5` オールスター /
#: `11` 入替戦 / `20` アーリーカップは入れない。
EVENTS = (3, 2)

DEFAULT_STATE_PATH = Path("batch/.scraper-state/state.json")
ROWS_PER_REQUEST = max_rows_per_request("games")


@dataclass
class Result:
    """出力に出す集計。**理由ごとに分けて数える**（詳細設計 4.4）。"""

    ingested: int = 0
    #: 窓の外だった試合（正常。数だけ出す）
    outside_window: int = 0
    #: 既に終わっていた試合（ステップ1 が扱う）
    finished: int = 0
    skipped_undated: int = 0
    skipped_unresolved: int = 0
    club_options: int = 0
    unmatched_clubs: list[str] = field(default_factory=list)
    seasons: list[str] = field(default_factory=list)


def jst_today(now: datetime | None = None) -> str:
    """JST の暦日。**`game_date` の定義そのもの**（CLAUDE.md 時刻の扱い）。"""
    moment = now or datetime.now(UTC)
    return (moment.astimezone(UTC) + timedelta(hours=9)).strftime("%Y-%m-%d")


def window(today: str, days: int = UPCOMING_DAYS) -> tuple[str, str]:
    """`[今日, 今日 + days]` の閉区間。**当日を含める** — 当日の試合はまだ始まって
    いないことがあり、`tipoff_at > now` の絞り込みは推論側（ステップ9）が行う。
    """
    start = datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=UTC)
    return today, (start + timedelta(days=days)).strftime("%Y-%m-%d")


def months_of(start: str, end: str) -> list[int]:
    """窓が触れる暦月。**月をまたぐなら2つ返す**（詳細設計 4.2 のステップ1b）。

    **暦日の文字列から月を取るだけで、時刻を作らない。** `game_date` は JST の
    暦日であり（CLAUDE.md）、ここでタイムゾーンを持ち込むと二重に変換しうる。
    """
    months = [int(start[5:7])]
    last = int(end[5:7])
    if last != months[0]:
        months.append(last)
    return months


def seasons_of(start: str, end: str, seasons: list[Season]) -> list[Season]:
    """窓に重なるシーズン。**時計ではなく `seasons.csv` の期間で決める。**

    期間は「当季の9月1日〜翌年6月30日」に固定してある（詳細設計 1.1）。
    オフシーズンなら空になり、**取得を1回も行わない**。
    """
    return [s for s in seasons if s.start_date <= end and start <= s.end_date]


def _collect(
    client: RateLimitedClient,
    season: Season,
    result: Result,
) -> Iterator[ScheduleGame]:
    """そのシーズンの、窓に触れる月の日程を辿る。"""
    year = int(season.label[:4])
    clubs_by_name = parse_club_options(client.get(schedule_html_url(year)))
    # その年度のクラブ一覧の件数を出す。**20クラブのはずが18なら、ここで分かる**
    result.club_options = len(clubs_by_name)

    def fold(page: SchedulePage) -> None:
        result.skipped_undated += page.undated
        result.skipped_unresolved += page.unresolved
        for name in page.unmatched_clubs:
            if name not in result.unmatched_clubs:
                result.unmatched_clubs.append(name)

    start, end = window(jst_today())
    seen: set[str] = set()
    for month in months_of(start, end):
        for event in EVENTS:
            for game in walk_schedule(
                client, year=year, event=event, clubs_by_name=clubs_by_name,
                on_page=fold, month=month,
            ):
                # **チャンピオンシップ（event=3）を先に確定させる**（要件 5.3）。
                # 後から event=2 で同じ試合を見ても `competition` を上書きしない
                if game.game_id in seen:
                    continue
                seen.add(game.game_id)
                yield game


def pick_upcoming(
    games: list[ScheduleGame], start: str, end: str, result: Result,
) -> list[ScheduleGame]:
    """窓の中の未実施の試合だけを残す。

    **`POSTPONED` と `CANCELLED` も残す。** `SCHEDULED` だけにすると、中止に
    なった試合が `SCHEDULED` のまま残り予測が作られ続ける（詳細設計 1.3）。
    """
    picked = []
    for game in games:
        if not (start <= game.game_date <= end):
            result.outside_window += 1
            continue
        if game.status == "FINISHED":
            result.finished += 1
            continue
        picked.append(game)
    return picked


class Poster(Protocol):
    """`send` が必要とするのは `post` だけである。

    **`InternalApi` そのものを要求しない。** テストが接続先の検証や HTTP の作法を
    持つ本物を組む必要がなくなる（`registry.py` が `Any` で済ませたのと同じ事情だが、
    こちらは**何を呼ぶのか**を型で残す）。
    """

    def post(self, path: str, payload: Mapping[str, object]) -> object: ...


def send(
    api: Poster,
    games: list[ScheduleGame],
    *,
    season: Season,
    club_ids: Mapping[str, str],
    fetched_at: str,
) -> int:
    """`POST /internal/games` で送る。**1リクエストの行数上限を守る**（詳細設計 3.4）。"""
    if not games:
        return 0
    series = series_numbers(
        [(g.game_id, g.game_date, g.home_name, g.away_name) for g in games])
    reference = SeasonRef(season.id, season.label, season.league)
    for begin in range(0, len(games), ROWS_PER_REQUEST):
        api.post(
            "games",
            upcoming_games_payload(
                games[begin:begin + ROWS_PER_REQUEST],
                season=reference,
                club_ids=club_ids,
                series_game_no=series,
                fetched_at=fetched_at,
            ),
        )
    return len(games)


def run_upcoming(client: RateLimitedClient, api: InternalApi) -> Result:
    """ステップ1b。**取得前に robots と利用規約を照合する**（絶対ルール6）。"""
    client.verify_policy(
        terms_reporter=report_terms_change(os.environ.get("SCRAPER_TERMS_SHA256", "")),
    )
    result = Result()
    start, end = window(jst_today())
    club_ids = {row.source_id: row.club_id for row in load_club_source_ids()}
    fetched_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    for season in seasons_of(start, end, load_seasons()):
        result.seasons.append(season.id)
        games = pick_upcoming(list(_collect(client, season, result)), start, end, result)
        result.ingested += send(
            api, games, season=season, club_ids=club_ids, fetched_at=fetched_at)
    return result


def _log(api: InternalApi, status: str, rows: int) -> None:
    """`ingestion_logs` に記録する。**例外の本文を入れない**（絶対ルール4）。"""
    moment = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    try:
        api.post("log", {
            "id": f"daily-{uuid.uuid4().hex[:8]}",
            "job": "daily_ingest",
            "startedAt": moment,
            "finishedAt": moment,
            "status": status,
            "rowsAffected": rows,
        })
    except LoaderError:
        print("  - ログの記録に失敗した")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="日次の取り込み（詳細設計 4.2）")
    parser.add_argument(
        "--only-upcoming", action="store_true", required=True,
        help="未実施の試合の取り込み（ステップ1b）だけを行う。**他のステップは未実装**")
    parser.add_argument("--dry-run", action="store_true", help="D1 には書かない")
    args = parser.parse_args(argv)

    api = InternalApi(
        os.environ.get("API_BASE_URL", ""),
        os.environ.get("INGEST_TOKEN", ""),
        dry_run=args.dry_run,
    )
    client = RateLimitedClient(
        user_agent=os.environ.get("SCRAPER_USER_AGENT", ""),
        state_path=Path(os.environ.get("SCRAPER_STATE_PATH", str(DEFAULT_STATE_PATH))),
        robots_sha256=os.environ.get("SCRAPER_ROBOTS_SHA256") or None,
        terms_sha256=os.environ.get("SCRAPER_TERMS_SHA256") or None,
    )
    try:
        result = run_upcoming(client, api)
    except PolicyError:
        print("daily_ingest: 取得前確認に失敗した（robots / 利用規約）", file=sys.stderr)
        return 1
    except ScrapingStopped:
        # 429 / 503。**スクレイピング区間のみ中止する**（絶対ルール6）
        print("daily_ingest: 取得を中止した（相手側の応答）", file=sys.stderr)
        if not args.dry_run:
            _log(api, "PARTIAL", 0)
        return 1
    except (LoaderError, ParseError, ScraperError) as error:
        print(f"daily_ingest: 失敗（{type(error).__name__}: {error}）", file=sys.stderr)
        if not args.dry_run:
            _log(api, "FAILED", 0)
        return 1
    except Exception as error:  # noqa: BLE001 — 公開ログの境界で本文を除去する
        print(f"daily_ingest: 失敗（{type(error).__name__}）", file=sys.stderr)
        return 1

    print(
        f"daily_ingest: 取り込み={result.ingested} 窓の外={result.outside_window}"
        f" 終了済み={result.finished} 日付不明={result.skipped_undated}"
        f" 状態不明={result.skipped_unresolved}"
        f" クラブ一覧={result.club_options} シーズン={','.join(result.seasons) or 'なし'}"
    )
    if result.unmatched_clubs:
        print(f"  クラブ一覧にない相手: {' / '.join(result.unmatched_clubs)}")
    if not args.dry_run:
        _log(api, "SUCCESS", result.ingested)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
