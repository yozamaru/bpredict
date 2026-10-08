"""日次の取り込み（詳細設計 4.2 / 基本設計 4.2）。

**段階的に作っている。** 設計は12ステップを定めるが、いま実装してあるのは
**結果の取得（1）・未実施の試合（1b）・照合と Elo（3）・推論（4）・静的JSON（5）**である。

| ステップ | 実装 |
|---|---|
| 0. robots / 利用規約のハッシュ照合 | **あり** |
| **1. 結果の取得（前日と当日）** | **あり**（`--only-results`。1試合ごとの取り込みは `backfill` と共有する） |
| **1b. 未実施の試合の取り込み** | **あり**（`--only-upcoming`） |
| **2. スナップショット更新** | **あり**（1b が送った行を同じ本文から写す） |
| **3. 照合・集計・Elo** | **あり**（`--only-settle`。`batch.jobs.evaluate` と `recompute_ratings` を呼ぶ） |
| **4. 推論** | **あり**（`--only-inference`） |
| **5. 静的JSON の書き出し** | **あり**（`--only-inference` の後段） |
| 12. `ingestion_logs` | **あり** |

**実装していないステップを黙って飛ばさない。** どのステップを行うかを引数で
**必ず明示させる**（最低1つ required）。全ステップが揃うまで既定の動作を
持たせない — 「日次ジョブを回したつもりで半分しか動いていない」が最も危ない。

**推論は外部アクセスを行わない。** `--only-inference` だけなら robots の照合も
スクレイピングもしない（入力はスナップショットと `/internal/*` の GET だけ）。

未実施の試合を取り込む理由は 4.2 のステップ1b にある。**`backfill` は `FINISHED`
以外を書き込まない**ため（4.8）、これが無いと「向こう7日間の試合について特徴量を
生成」（ステップ8）の対象が1件も無い。
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pandas as pd

from batch.features.base import build_context
from batch.features.builder import FEATURE_KEYS, build_features
from batch.features.dataset import (
    Dataset,
    SnapshotError,
    load_snapshot,
    write_snapshot,
)
from batch.features.prepared import prepare
from batch.jobs import evaluate as evaluate_job
from batch.jobs import recompute_ratings as ratings_job
from batch.jobs import summarize_stats as summarize_job
from batch.jobs.evaluate import EvaluateError
from batch.jobs.game_ingest import ingest_game
from batch.jobs.schedule_walk import walk_schedule
from batch.jobs.seed_master import Season, load_club_source_ids, load_seasons
from batch.jobs.summarize_stats import SummarizeError
from batch.loader import exclusions
from batch.loader.api import InternalApi, LoaderError, Poster, RejectedError
from batch.loader.limits import max_rows_per_request
from batch.loader.payload import (
    SeasonRef,
    player_prediction_payload,
    prediction_payload,
    series_numbers,
    snapshot_rows,
    team_target_payload,
    upcoming_games_payload,
)
from batch.model.dataset import as_of
from batch.model.explain import factor_payload
from batch.model.explain import payload_of as reason_payload
from batch.model.predict import (
    ActiveModels,
    PredictError,
    load_active,
    load_active_rates,
)
from batch.model.predict_players import (
    BoxScore,
    BoxScoreError,
    InfeasibleTargetError,
    SideBox,
    predict_box_score,
)
from batch.parser.errors import (
    DataUnavailable,
    OutOfScopeError,
    ParseError,
    ParseErrorStreak,
    ParseFailureTracker,
    ValidationError,
)
from batch.parser.schedule_parser import (
    ExcludedGame,
    ScheduleGame,
    SchedulePage,
    parse_club_options,
)
from batch.parser.terms import report_terms_change
from batch.scraper.client import (
    PolicyError,
    RateLimitedClient,
    ResponseError,
    ScraperError,
    ScrapingStopped,
    TransportError,
)
from batch.scraper.schedule import schedule_html_url
from batch.static_json.builder import (
    EvaluationInput,
    FactorInput,
    PlayerInput,
    ReasonInput,
)
from batch.static_json.from_snapshot import PredictedGame, build_inputs
from batch.static_json.writer import (
    DATA_DIR,
    SCHEDULE_WINDOW_DAYS,
    write_static_json,
)

#: 予測の対象にする窓（詳細設計 4.2 のステップ8「向こう7日間」）。
UPCOMING_DAYS = 7

#: 取り込む大会区分（要件 5.3）。`4` 残留プレーオフ / `5` オールスター /
#: `11` 入替戦 / `20` アーリーカップは入れない。
EVENTS = (3, 2)

DEFAULT_STATE_PATH = Path("batch/.scraper-state/state.json")

#: スナップショットの置き場。**入力はここだけである**（絶対ルール3）。
DEFAULT_SNAPSHOT = Path("batch/snapshot")
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
    #: ステップ2 で書いた行数（テーブルごと）
    snapshot_rows: dict[str, int] = field(default_factory=dict)
    #: 取り込まない試合（不戦敗）をシーズンごとに持つ。**件数だけでなく中身を残す**
    #: （要件 5.3 / 4.4）。`backfill` は artifact で持ち帰るしかないが、
    #: **このジョブは `contents: write` であり自分でコミットできる**
    excluded: dict[str, list[ExcludedGame]] = field(default_factory=dict)


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
    *,
    through: str,
    clubs_by_name: Mapping[str, str],
) -> Iterator[ScheduleGame]:
    """そのシーズンの日程を、`through` を超えるまで辿る。

    **月で絞らない**（`mon=10` は読める行を1つも返さない。詳細設計 4.2 のステップ1b）。
    `index` を進めながら、ページの最終日が `through` を超えたら止める。

    **常に開幕から辿る。** 連戦番号（`series_numbers`）は前日までの試合を見るため、
    途中から始めると**全試合が1戦目になる**。
    """
    year = int(season.label[:4])

    def fold(page: SchedulePage) -> None:
        result.skipped_undated += page.undated
        result.skipped_unresolved += page.unresolved
        # **取り込まない試合を捨てない**（要件 5.3）。黙って消えるのは、この設計が
        # 最も避けたい壊れ方である
        _add_excluded(result.excluded, season.id, page.excluded)
        for name in page.unmatched_clubs:
            if name not in result.unmatched_clubs:
                result.unmatched_clubs.append(name)

    def past_the_window(page: SchedulePage) -> bool:
        """ページの最終日が `through` を超えたら、以降のページは要らない。

        **ページは日付の昇順である**（実測。index 0 が開幕戦から始まる）。
        `mon` で月に絞る案は使えない — **読める行を1つも返さなかった**
        （詳細設計 4.2 のステップ1b）。
        """
        return page.last_date is not None and page.last_date > through

    seen: set[str] = set()
    for event in EVENTS:
        for game in walk_schedule(
            client, year=year, event=event, clubs_by_name=clubs_by_name,
            on_page=fold, stop=past_the_window,
        ):
            # **チャンピオンシップ（event=3）を先に確定させる**（要件 5.3）。
            # 後から event=2 で同じ試合を見ても `competition` を上書きしない
            if game.game_id in seen:
                continue
            seen.add(game.game_id)
            yield game


def _add_excluded(
    target: dict[str, list[ExcludedGame]], season_id: str,
    found: Sequence[ExcludedGame],
) -> None:
    """取り込まない試合を**試合IDで重ねる**（要件 5.3 / 詳細設計 4.4）。

    **素朴に足さない。** 日程は大会区分ごとに2回辿るため（`EVENTS`）、同じ試合が
    `event=3` と `event=2` の両方に現れ、件数が二重になる。
    """
    for game in found:
        known = target.setdefault(season_id, [])
        if all(row.game_id != game.game_id for row in known):
            known.append(game)


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


def send(
    api: Poster,
    games: list[ScheduleGame],
    *,
    season: Season,
    club_ids: Mapping[str, str],
    fetched_at: str,
    collect: list[Mapping[str, object]] | None = None,
) -> int:
    """`POST /internal/games` で送る。**1リクエストの行数上限を守る**（詳細設計 3.4）。

    `collect` を渡すと、送った本文をそこに積む。**ステップ2がそれを
    スナップショットへ写す** — 同じ値であることが要点である（基本設計 2.2）。
    """
    if not games:
        return 0
    series = series_numbers(
        [(g.game_id, g.game_date, g.home_name, g.away_name) for g in games])
    reference = SeasonRef(season.id, season.label, season.league)
    for begin in range(0, len(games), ROWS_PER_REQUEST):
        body = upcoming_games_payload(
            games[begin:begin + ROWS_PER_REQUEST],
            season=reference,
            club_ids=club_ids,
            series_game_no=series,
            fetched_at=fetched_at,
        )
        api.post("games", body)
        if collect is not None:
            collect.append(body)
    return len(games)


def run_upcoming(
    client: RateLimitedClient, api: InternalApi, *,
    snapshot: Path | None = None, log: Callable[[str], None] = print,
) -> Result:
    """ステップ1b と2。**取得前に robots と利用規約を照合する**（絶対ルール6）。

    `snapshot` を渡すと、送ったのと同じ行を**スナップショットにも書く**
    （ステップ2。基本設計 2.2）。**渡さないと推論が未実施の試合を見られない。**
    """
    client.verify_policy(
        terms_reporter=report_terms_change(os.environ.get("SCRAPER_TERMS_SHA256", "")),
    )
    result = Result()
    start, end = window(jst_today())
    club_ids = {row.source_id: row.club_id for row in load_club_source_ids()}
    fetched_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    bodies: list[Mapping[str, object]] = []
    for season in seasons_of(start, end, load_seasons()):
        result.seasons.append(season.id)
        clubs_by_name = parse_club_options(client.get(schedule_html_url(int(season.label[:4]))))
        # その年度のクラブ一覧の件数を出す。**20クラブのはずが18なら、ここで分かる**
        result.club_options = len(clubs_by_name)
        games = pick_upcoming(
            list(_collect(client, season, result, through=end,
                          clubs_by_name=clubs_by_name)),
            start, end, result)
        result.ingested += send(
            api, games, season=season, club_ids=club_ids, fetched_at=fetched_at,
            collect=bodies)

    # --- ステップ2。**D1 に送ったのと同じ本文から写す** ---
    if snapshot is not None and bodies:
        ds = load_snapshot(snapshot)
        written: dict[str, int] = {}
        for body in bodies:
            for table, count in apply_to_snapshot(ds, snapshot_rows(body)).items():
                written[table] = written.get(table, 0) + count
        write_snapshot(ds, snapshot, tables=UPCOMING_TABLES)
        result.snapshot_rows = written
        log(f"daily_ingest: スナップショットを更新した（{written}）")
    return result


# --- ステップ1: 前日の結果取得（詳細設計 4.2 のステップ1） ---

@dataclass
class Finished:
    """前日の結果取得の集計。**理由ごとに分けて数える**（詳細設計 4.4）。"""

    status: str = "SUCCESS"
    ingested: int = 0
    #: 窓の外だった試合（正常。開幕から辿るため必ず出る）
    other_days: int = 0
    #: 既に取り込んであった試合（**飛ばさないと1日4回書き直す**）
    already: int = 0
    #: 前日だが終了していない試合（延期・中止・開始前）
    unfinished: int = 0
    #: 対象外（選抜チーム・海外クラブ・大会区分の食い違い）
    non_league: int = 0
    #: 値域・恒等式の違反。**データの欠陥である**
    invalid: int = 0
    #: 取得できなかった試合（非200・通信失敗）。**`不正` と混ぜない**（4.4）
    unfetched: int = 0
    skipped_undated: int = 0
    skipped_unresolved: int = 0
    excluded: dict[str, list[ExcludedGame]] = field(default_factory=dict)
    seasons: list[str] = field(default_factory=list)
    snapshot_rows: dict[str, int] = field(default_factory=dict)
    #: (game_id, 例外の型名, 自前メッセージ)。**件数だけでは調査ができない**（4.4）
    skipped: list[tuple[str, str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def skip(self, game_id: str, error: Exception) -> None:
        """例外オブジェクトを残さない。型名と自前メッセージだけにする（絶対ルール4）。"""
        self.skipped.append((game_id, type(error).__name__, str(error)))

    def degrade(self, note: str) -> None:
        self.status = "PARTIAL"
        self.notes.append(note)


def yesterday_jst(now: datetime | None = None) -> str:
    """前日の JST の暦日。**`game_date` の定義そのもの**（CLAUDE.md 時刻の扱い）。"""
    moment = (now or datetime.now(UTC)).astimezone(UTC) + timedelta(hours=9)
    return (moment - timedelta(days=1)).strftime("%Y-%m-%d")


def run_results(
    client: RateLimitedClient, api: InternalApi, *,
    snapshot: Path | None = None, now: datetime | None = None,
    log: Callable[[str], None] = print,
) -> Finished:
    """ステップ1。`[前日, 当日]`（JST）の終了した試合を取り込み、スナップショットにも写す。

    **当日も見る**（v1.97）。前日だけだと、14:05 に始まって16時過ぎに終わった試合が
    **翌朝06:00 まで取り込まれない** — 的中の記録が最大14時間遅れる。

    **既に取り込んだ試合は飛ばす**（`backfill` と同じ判定。4.8）。飛ばさないと
    同じ試合を1日4回書き直す（1試合118.6行）。

    **ステップ1b と日程の walk を共有しない。** 止める日付が違うだけだが、
    429 で片方が中止されてももう片方は続けるという 4.3 の方針を保つには、
    **取得区間が独立している**必要がある。1回の walk は最大40ページ前後 × 2区分で、
    日次上限3,000に対して無視できる。

    **取り込めなかった試合を翌日に拾わない**（詳細設計 4.2）。前日ぶんだけを見る
    ため、失敗した試合はそのまま欠ける。拾うのは `backfill`（再開可能）である。
    """
    client.verify_policy(
        terms_reporter=report_terms_change(os.environ.get("SCRAPER_TERMS_SHA256", "")),
    )
    result = Finished()
    start, end = yesterday_jst(now), jst_today(now)
    club_ids = {row.source_id: row.club_id for row in load_club_source_ids()}
    tracker = ParseFailureTracker()
    bodies: list[Mapping[str, object]] = []

    for season in seasons_of(start, end, load_seasons()):
        result.seasons.append(season.id)
        collected, short_names = _collect_finished(client, season, result, through=end)
        if collected is None:
            return _mirror(result, snapshot, bodies, log)
        # **既に取り込んだ試合を飛ばす**（`backfill` と同じ口。4.8）。
        # 「スタッツまで入っているか」を見るため、試合行だけ残った状態も拾える
        done = api.ingested_game_ids(season.id)

        # **連戦番号はシーズン全体から導く**（`series_numbers` は前日までを見る）。
        # 前日だけで数えると全試合が1戦目になる
        series = series_numbers([
            (g.game_id, g.game_date, g.home_source_id, g.away_source_id)
            for g, _ in collected
        ])
        reference = SeasonRef(season.id, season.label, season.league)

        for game, event in collected:
            if not (start <= game.game_date <= end):
                result.other_days += 1
                continue
            if game.status != "FINISHED":
                # 延期・中止・開始前。ステップ1b が `games` に状態として入れる
                result.unfinished += 1
                continue
            if game.game_id in done:
                result.already += 1
                continue
            try:
                sent = ingest_game(
                    client, api, game, event=event, season=reference,
                    club_ids=club_ids, short_names=short_names,
                    series_game_no=series.get(game.game_id))
            except ScrapingStopped:
                result.degrade("429/503 により取得区間を中止した")
                break
            except OutOfScopeError as error:
                # 対象外（選抜チーム・海外クラブ・大会区分の食い違い）。
                # **`不正` に数えず、連続失敗にも入れない** — 再実行すれば直るものと
                # 永久に対象外のものを混ぜない（詳細設計 4.4）
                result.non_league += 1
                result.skip(game.game_id, error)
                continue
            except LoaderError as error:
                # **D1 への書き込みが失敗したら、その場で止める**（4.3）。
                # 枠が尽きた状態で残りを叩いても全部失敗する
                result.degrade(f"D1 への書き込みを中止した（{error}）")
                break
            except (ValidationError, DataUnavailable) as error:
                # 値域・恒等式の違反は当該試合を飛ばす（異常値を Elo に流さない）。
                # **連続失敗には数えない**（`backfill` と同じ扱い）
                result.invalid += 1
                result.skip(game.game_id, error)
                tracker.success()
                continue
            except (ResponseError, TransportError, ParseError) as error:
                # 取得失敗は**相手側の事情であり再実行で解消する**ため `不正` と
                # 分けて数える（4.4）。**連続3件で中止する** — 非200が続くのは
                # 遮断の疑いであり、叩き続けるのは絶対ルール6に反する
                if isinstance(error, ParseError):
                    result.invalid += 1
                else:
                    result.unfetched += 1
                result.skip(game.game_id, error)
                try:
                    tracker.failure()
                except ParseErrorStreak:
                    result.degrade("取得・パースの失敗が連続3件。中止した")
                    break
                continue
            tracker.success()
            result.ingested += 1
            bodies.extend((sent.games, sent.stats))

    return _mirror(result, snapshot, bodies, log)


def _collect_finished(
    client: RateLimitedClient, season: Season, result: Finished, *, through: str,
) -> tuple[list[tuple[ScheduleGame, int]] | None, dict[str, str]]:
    """日程を辿って `(試合, event)` を集める。失敗したら `(None, …)` を返す。

    **チャンピオンシップ（`event=3`）を先に確定させる**（要件 5.3）。
    """
    year = int(season.label[:4])
    bridge = Result()
    try:
        clubs_by_name = parse_club_options(client.get(schedule_html_url(year)))
    except ScrapingStopped:
        result.degrade("429/503 により日程の取得を中止した")
        return None, {}
    except (ParseError, ValidationError) as error:
        result.degrade(f"日程の解析に失敗した（{type(error).__name__}: {error}）")
        return None, {}

    short_names = {source_id: name for name, source_id in clubs_by_name.items()}
    collected: list[tuple[ScheduleGame, int]] = []
    seen: set[str] = set()
    try:
        for event in EVENTS:
            for game in _collect(
                client, season, bridge, through=through,
                clubs_by_name=clubs_by_name,
            ):
                if game.game_id in seen:
                    continue
                seen.add(game.game_id)
                collected.append((game, event))
    except ScrapingStopped:
        result.degrade("429/503 により日程の取得を中止した")
        return None, {}
    except (ParseError, ValidationError) as error:
        result.degrade(f"日程の解析に失敗した（{type(error).__name__}: {error}）")
        return None, {}

    result.skipped_undated += bridge.skipped_undated
    result.skipped_unresolved += bridge.skipped_unresolved
    for season_id, found in bridge.excluded.items():
        _add_excluded(result.excluded, season_id, found)
    return collected, short_names


def _mirror(
    result: Finished, snapshot: Path | None,
    bodies: list[Mapping[str, object]], log: Callable[[str], None],
) -> Finished:
    """ステップ2。**D1 に送ったのと同じ本文から写す**（基本設計 2.2）。"""
    if snapshot is None or not bodies:
        return result
    ds = load_snapshot(snapshot)
    written: dict[str, int] = {}
    for table, count in apply_to_snapshot(ds, snapshot_rows(*bodies)).items():
        written[table] = written.get(table, 0) + count
    write_snapshot(ds, snapshot, tables=tuple(written))
    result.snapshot_rows = written
    log(f"daily_ingest: スナップショットを更新した（{written}）")
    return result


# --- ステップ2: スナップショット更新（詳細設計 4.2 のステップ2） ---

#: ステップ1b が触るテーブル。**書き直すのはこの2つだけ**（基本設計 2.2 の部分書き出し）。
UPCOMING_TABLES = ("games", "team_games")

#: 行の同一性（詳細設計 4.2 のステップ1 の表。DDL の主キーと同じ）。
KEYS: Mapping[str, tuple[str, ...]] = {
    "games": ("id",),
    "team_games": ("club_id", "game_date", "game_id"),
    "team_game_stats": ("game_id", "club_id"),
    "player_game_stats": ("game_id", "player_id"),
    "players": ("id",),
    "venues": ("id",),
    "venue_source_keys": ("source_code",),
    "club_seasons": ("club_id", "season_id"),
    # `POST /internal/rosters` が送る（詳細設計 4.13）。D1 の主キーと同じ3列
    "player_seasons": ("player_id", "season_id", "club_id"),
}

#: **既存の行では上書きしない列。** D1 の upsert が `update` に入れていない列と
#: そろえる（詳細設計 3.4）。
#:
#: `venues.name` は**本文にあるのに更新しない唯一の列**である — 初出の名称で固定し、
#: 当時の名称で現在の表示名を上書きしない（1.2）。**本文に無い列は触らない**という
#: 一般の規則で、座標・本拠会場・チームカラー・身長（D1 側の `preserve`）は片づく。
NEVER_UPDATE: Mapping[str, frozenset[str]] = {
    "venues": frozenset({"name"}),
}


def apply_to_snapshot(
    ds: Dataset, rows: Mapping[str, list[dict[str, object]]],
) -> dict[str, int]:
    """送った行をスナップショットに upsert する。**D1 に送ったのと同じ値を書く。**

    **これが無いと推論が未実施の試合を見られない。** 推論の入力はスナップショット
    だけであり（絶対ルール3）、D1 にだけ書くと「取り込んだのに予測が作られない」
    状態になる（基本設計 2.2 が座標140件で踏んだのと同じ形）。

    **行を置き換えず、本文にある列だけを上書きする**（詳細設計 4.2 のステップ1）。
    置き換えると、**本文が送らない列が消える** — 3.4 が D1 側で直したのと同じ
    壊れ方である（取り込みは `venues` の `{id, name}` だけを送るため、
    座標が失われる）。返すのは書いた行数。

    **行の位置は保たない。** 既にある行は末尾へ移るが、特徴量が入力の並びに
    依存するのは `_recent_minutes`（同じ日に複数試合がある選手）だけで、
    **1人が1日に2試合出ることはない**（2.1.1 の注記3）。
    """
    written: dict[str, int] = {}
    for table, incoming in rows.items():
        if not incoming:
            continue
        if table not in KEYS:
            raise SnapshotError(f"スナップショットへの写し方が未定のテーブル: {table}")
        current = ds.table(table)
        keys = list(KEYS[table])
        protect = NEVER_UPDATE.get(table, frozenset())

        converted: dict[tuple[str, ...], dict[str, object]] = {}
        for row in incoming:
            missing = sorted(set(row) - set(current.columns))
            if missing:
                raise SnapshotError(f"{table} にない列を書こうとした: {missing}")
            converted[tuple(str(row[k]) for k in keys)] = dict(row)

        if current.empty:
            kept, existing = current, cast("dict[tuple[str, ...], dict[str, object]]", {})
        else:
            found = current[keys].astype(str).agg(tuple, axis=1).isin(converted)
            kept = current[~found]
            # **一致した行だけを辞書にする。** 全件を辞書にすると
            # `player_game_stats`（146,463行）で桁が変わる
            existing = {
                tuple(str(row[k]) for k in keys): {str(c): v for c, v in row.items()}
                for row in current[found].to_dict(orient="records")
            }

        merged = []
        for key, row in converted.items():
            base = existing.get(key)
            if base is None:
                merged.append(row)          # 新規。本文のまま入れる
            else:
                patch = {c: v for c, v in row.items() if c not in protect}
                merged.append({**base, **patch})
        ds.tables[table] = pd.concat(
            [kept, pd.DataFrame(merged)], ignore_index=True)[list(current.columns)]
        written[table] = len(merged)
    return written


# --- ステップ3: 照合・集計・Elo 再計算（詳細設計 4.2 のステップ3） ---

@dataclass
class Settled:
    """照合・集計・Elo の結果（ステップ3）。

    **タプルで返さない。** 3つ目（`verdicts`）を足した時点で、呼び出し側が
    位置で覚えることになる（`GameListRow` と同じ理由）。
    """

    status: str = "SUCCESS"
    #: `/results`（引数なし）が既定で見る日（詳細設計 3.7）
    latest_result_date: str | None = None
    #: 試合ID → 照合の結果。**一覧の併記に使う**（詳細設計 3.3 の v1.131）。
    #: **この回に照合した分だけ**である — 遅い時刻に終わった試合は次の回に付く
    verdicts: dict[str, EvaluationInput] = field(default_factory=dict)


def run_settle(
    api: InternalApi, *, snapshot: Path = DEFAULT_SNAPSHOT,
    log: Callable[[str], None] = print,
) -> Settled:
    """照合（A-04）と Elo の再計算を回す。

    `("SUCCESS" | "PARTIAL", 照合した最も新しい試合日)` を返す。
    後者は `meta.json` の `latestResultDate` になる（詳細設計 3.7）— これが
    `/results`（引数なし）が既定で見る日である。**照合が無ければ None** で、
    書き出し側が前回の値を引き継ぐ。

    **cron を置いたことで、ここを結線しないと Elo が毎日1日ずつ古くなる。**
    ステップ1 が結果を取り込んでも `team_ratings` は書き変わらず、推論が読む Elo は
    **最後に `rebuild-derived` を手で回した日のまま**になる。

    **推論より前に呼ぶ。** 推論はスナップショットの `team_ratings` を読むため、
    後に回すとその日の推論が古い Elo を使う。

    **照合が失敗しても Elo は回す。** 互いに依存せず、**Elo を止める方が予測に
    直接効く**（`elo_diff` は寄与度が最大の列である）。どちらも冪等なので、
    失敗しても翌日やり直せる。
    """
    settled = Settled()
    status = "SUCCESS"
    try:
        outcome = evaluate_job.run(api=api, snapshot_dir=snapshot)
        counted = [r for r in outcome.results if r.counted]
        settled.latest_result_date = outcome.latest_result_date
        # **一覧の併記に渡す**（詳細設計 3.3 の v1.131）。`VOID` は入れない —
        # 的中率の母数から除外される予測であり、対比の対象がない（1.6）
        settled.verdicts = {
            r.game_id: EvaluationInput(
                is_correct=None if r.is_correct is None else bool(r.is_correct),
                score_error=r.score_mae,
                outcome=r.outcome,
            )
            for r in counted
        }
        log(
            f"daily_ingest: 照合={len(outcome.results)}件"
            f"（母数に入る {len(counted)}件 / 飛ばした {len(outcome.skipped)}件）"
        )
    except (EvaluateError, LoaderError, SnapshotError) as error:
        # **型名だけにしない**（詳細設計 4.3）。いずれも自前の文言を持つ
        log(f"  - 照合に失敗（{type(error).__name__}: {error}）")
        status = "PARTIAL"

    try:
        # **食い違う日付以降だけを D1 へ送る**（詳細設計 4.2 のステップ3）。
        # 毎回全期間（12,584行）を送ると、書き込み枠の12.6%を Elo だけで使う
        # — 1日4回回すと50%になる
        ratings = ratings_job.run(api=api, snapshot_dir=snapshot, only_changed=True)
        log(
            f"daily_ingest: Elo を再計算した（{ratings.rows}行"
            f" / 送った {ratings.requests}リクエスト"
            f" / {ratings.from_date or '—'}〜{ratings.to_date or '—'}"
            f"{' / 保存済みと一致' if ratings.unchanged else ''}）"
        )
        for note in ratings.notes:
            log(f"  - {note}")
    except (LoaderError, SnapshotError, ValueError) as error:
        log(f"  - Elo の再計算に失敗（{type(error).__name__}: {error}）")
        status = "PARTIAL"

    # **実績の集計は最後に置く**（詳細設計 4.2 のステップ6b）。推論はこの2表を
    # 読まないため、失敗しても予測は出る
    try:
        stats = summarize_job.run(api=api, snapshot_dir=snapshot)
        log(
            f"daily_ingest: 実績を集計した（選手 {len(stats.players)}行"
            f" / クラブ {len(stats.teams)}行"
            f" / 静的生成する選手 {len(stats.recent_players)}人）"
        )
    except (SummarizeError, LoaderError, SnapshotError) as error:
        log(f"  - 実績の集計に失敗（{type(error).__name__}: {error}）")
        status = "PARTIAL"
    settled.status = status
    return settled


# --- ステップ4: 推論（詳細設計 4.2 の「推論（ステップ4）」） ---


@dataclass
class InferenceResult:
    """推論の集計。**飛ばした理由ごとに分けて数える**（詳細設計 4.4 と同じ方針）。"""

    predicted: int = 0
    #: 特徴量が作れなかった試合（試合IDを出す）
    skipped_features: list[str] = field(default_factory=list)
    #: tipoff を過ぎていて 409 を受けた試合（cron 遅延で起きうる）
    skipped_after_tipoff: list[str] = field(default_factory=list)
    #: 個人スタッツを破棄した試合（試合IDを出す。詳細設計 4.2 / 2.4）
    skipped_box_score: list[str] = field(default_factory=list)
    #: 個人スタッツまで出せた試合
    with_box_score: int = 0
    data_as_of: str | None = None
    model_versions: dict[str, str] = field(default_factory=dict)
    #: 30本の版。`(model_type, target)` → version。**揃っていなければ空**
    rate_versions: dict[tuple[str, str], str] = field(default_factory=dict)
    #: ステップ5（静的JSON）に渡す。**D1 から読み戻さない**（詳細設計 4.2）
    rows: list[PredictedGame] = field(default_factory=list)
    #: 書き出したファイル数（0 なら書いていない）
    written: int = 0


#: 推論の対象から外す余裕（分。設計 4.1 の `tipoff > now + 30分`）。
#: **cron の遅延と推論の所要時間で開始を跨ぐ**ため、開始直前の試合は外す。
INFERENCE_MARGIN_MINUTES = 30


def _plus_minutes(moment: str, minutes: int) -> str:
    """ISO 8601（UTC）の文字列に分を足す。**文字列の比較に使うため同じ書式で返す。**"""
    parsed = datetime.fromisoformat(moment).astimezone(UTC)
    return (parsed + timedelta(minutes=minutes)).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def upcoming_for_inference(
    ds: Dataset, start: str, end: str, now: str,
) -> list[tuple[str, str, str]]:
    """推論の対象（`game_id` / `season_id` / `tipoff_at`）。

    **`SCHEDULED` だけを対象にする。** `POSTPONED` と `CANCELLED` は取り込むが
    （ステップ1b）、予測は作らない — 中止試合の予測は `VOID` として母数から
    外れるだけで、作る意味がない（詳細設計 4.2）。

    **`tipoff_at > now + 30分` で絞る**（設計 4.1）。過ぎた試合に書くと API が 409 を
    返す（3.4 の関門3）。開始直前の試合も外すのは、**cron の遅延と推論の所要時間で
    開始を跨ぐ**ことがあるためである。

    **1日4回のすべてで同じ余裕を使う**（4.2）。06:00 の実行では最も早い開始時刻が
    8時間後であり、この余裕は実際には効かない — **同じ規約を2つ持たない**方を採る。
    """
    games = ds.table("games")
    margin = _plus_minutes(now, INFERENCE_MARGIN_MINUTES)
    picked = games[
        (games["status"] == "SCHEDULED")
        & (games["game_date"] >= start)
        & (games["game_date"] <= end)
        & (games["tipoff_at"] > margin)
    ].sort_values(["tipoff_at", "id"])
    return [
        (str(row.id), str(row.season_id), str(row.tipoff_at))
        for row in picked.itertuples()
    ]


def player_labels(ds: Dataset) -> tuple[dict[str, str], dict[str, str | None]]:
    """選手ID → 氏名 / ポジション。**画面に出す名前はスナップショットから取る。**

    ポジションは `player_seasons` の断面で、**未登録は None**（本番で185名。1.2）。
    同じ選手が複数季に現れるため、**最後に見た季の値**を採る（`player_seasons` は
    `season_id` 昇順で並んでいない前提で、辞書の上書きに任せる）。
    """
    names: dict[str, str] = {}
    if "players" in ds.tables:
        for row in ds.tables["players"].itertuples():
            names[str(row.id)] = str(row.name)
    positions: dict[str, str | None] = {}
    if "player_seasons" in ds.tables:
        for row in ds.tables["player_seasons"].itertuples():
            value = getattr(row, "position", None)
            positions[str(row.player_id)] = (
                None if value is None or pd.isna(value) else str(value))
    return names, positions


def _sides(box: BoxScore | None) -> list[tuple[SideBox, bool]]:
    return [] if box is None else [(box.home, True), (box.away, False)]


def team_target_rows(box: BoxScore | None) -> list[dict[str, object]]:
    """`prediction_team_targets` の2行（または0行）。"""
    return [
        team_target_payload(
            club_id=side.club_id, is_home=is_home, targets=side.targets)
        for side, is_home in _sides(box)
    ]


def player_rows(box: BoxScore | None) -> list[dict[str, object]]:
    """`player_predictions` の行。**両チーム分か0行**である（詳細設計 4.2）。"""
    rows: list[dict[str, object]] = []
    for side, _ in _sides(box):
        players = side.players
        for index, player_id in enumerate(players.player_ids):
            rows.append(player_prediction_payload(
                player_id=player_id, club_id=side.club_id,
                avail_prob=side.avail[player_id],
                minutes=float(players.minutes[index]),
                counts={k: float(v[index]) for k, v in players.counts.items()},
                pcts={k: float(v[index]) for k, v in players.pcts.items()},
            ))
    return rows


def player_inputs(
    box: BoxScore | None, names: Mapping[str, str],
    positions: Mapping[str, str | None],
) -> tuple[PlayerInput, ...]:
    """静的JSON に渡す形（`batch/static_json/builder.py` の `PlayerInput`）。

    **氏名が引けない選手は `player_id` を出さない。** 画面に内部IDを出すのは
    避けたいが、**名前が無いことを隠すほうが悪い** — 取り込みが `players` を
    書いているため、引けないのは上流の欠陥である。`（氏名不明）` と出す。
    """
    out: list[PlayerInput] = []
    for side, _ in _sides(box):
        players = side.players
        for index, player_id in enumerate(players.player_ids):
            counts = {k: float(v[index]) for k, v in players.counts.items()}
            pcts = {k: float(v[index]) for k, v in players.pcts.items()}
            out.append(PlayerInput(
                player_id=player_id,
                name=names.get(player_id, "（氏名不明）"),
                club_id=side.club_id,
                position=positions.get(player_id),
                avail_prob=side.avail[player_id],
                minutes=float(players.minutes[index]),
                **{k: counts[k] for k in ("fg2a", "fg3a", "fta", "oreb", "dreb",
                                          "ast", "tov", "stl", "blk", "pf", "fd")},
                **{k: pcts[k] for k in ("fg2_pct", "fg3_pct", "ft_pct")},
            ))
    return tuple(out)


def run_inference(
    api: InternalApi, *, ds: Dataset, run_id: str,
    now: datetime | None = None, log: Callable[[str], None] = print,
) -> InferenceResult:
    """ステップ4。**外部サイトへは一度もアクセスしない。**

    **3本が揃わなければ1件も書かない**（`load_active` が落とす）。勝率だけ出して
    予想スコアを NULL にする経路を作らない（要件 F-02 / F-03）。

    **スナップショットは呼び出し側が読む。** ステップ5（静的JSON）も同じものを
    読むため、ここで読むと2回読むことになる。
    """
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    stamp = moment.isoformat(timespec="seconds").replace("+00:00", "Z")
    models: ActiveModels = load_active(api, list(FEATURE_KEYS))
    # **30本は揃っていなくてもよい。** 勝率と予想スコアは出せる（詳細設計 4.2）。
    # **黙って通さない** — 欠けている `model_type` をログに出す
    rates = load_active_rates(api, log=log)
    result = InferenceResult(
        data_as_of=ds.max_finished_at, model_versions=dict(models.versions),
        rate_versions=dict(rates.versions) if rates is not None else {})
    if result.data_as_of is None:
        raise SnapshotError("スナップショットに終了した試合がない（data_as_of が出ない）")

    start, end = window(jst_today(moment))
    targets = upcoming_for_inference(ds, start, end, stamp)
    log(f"daily_ingest: 推論の対象 {len(targets)}試合（{start}〜{end}）")
    if not targets:
        return result

    # **索引は1回だけ作る**（詳細設計 2.1.1）。試合ごとに作ると桁が変わる
    prepared = prepare(ds)
    names, positions = player_labels(ds)
    for game_id, season_id, tipoff_at in targets:
        try:
            # **`as_of` は必ず `games.tipoff_at` である**（用語集 / 2.1）。
            # 解釈は `batch/model/dataset.py` の `_as_of` と同じにする
            features = build_features(
                game_id, as_of(tipoff_at), ds, prepared)
            prediction = models.predict(features)
            # **30本が無ければ `Context` を作らない。** `build_features` が
            # 内側で1つ作っており、個人スタッツを出さない回では要らない
            context = (
                None if rates is None
                else build_context(game_id, as_of(tipoff_at), ds, prepared)
            )
        except (ValueError, KeyError, PredictError) as error:
            # **件数だけでなく試合IDを出す**（詳細設計 4.4）。件数だけでは調査できない
            log(f"  - skip {game_id} {type(error).__name__}: {error}")
            result.skipped_features.append(game_id)
            continue

        # **チーム目標と個人スタッツ。** 失敗したらこの試合の個人スタッツだけを
        # 破棄し、チーム予測は保存する（詳細設計 2.4 / 4.2）。**ジョブは止めない**
        box: BoxScore | None = None
        if rates is not None and context is not None:
            try:
                box = predict_box_score(
                    rates, context,
                    home_score=prediction.home_score,
                    away_score=prediction.away_score,
                )
            except (InfeasibleTargetError, BoxScoreError, ValueError, KeyError) as error:
                log(f"  - 個人スタッツなし {game_id} {type(error).__name__}: {error}")
                result.skipped_box_score.append(game_id)
        # **根拠は寄与から機械的に出る**（詳細設計 2.7.1）。例外を投げる経路は
        # 「列が足りない」だけで、それは `models.predict` が先に落とす
        reasons = models.explainer.reasons(features)
        # **使った項目は全列出る**（詳細設計 2.7.2）。`reasons` が2件しか出ない
        # ことの埋め合わせではなく、別の問い（「何を見たか」）への答えである
        factors = models.explainer.factors(features)
        try:
            api.post("predictions", prediction_payload(
                game_id=game_id, season_id=season_id, run_id=run_id,
                predicted_at=stamp, as_of=tipoff_at, data_as_of=result.data_as_of,
                prediction=prediction, features=features,
                model_versions=models.versions,
                reasons=[reason_payload(r) for r in reasons],
                factors=[factor_payload(f) for f in factors],
                team_targets=team_target_rows(box),
                player_predictions=player_rows(box),
                # **使ったモデルだけを並べる**（詳細設計 4.2 の `modelBundle`）。
                # 個人スタッツを破棄した試合では30本の値が1つも保存されないため、
                # 束にも入れない — **どの値がどのモデルから出たか**の記録である
                rate_versions=result.rate_versions if box is not None else None,
            ))
        except RejectedError:
            # 409（tipoff 経過）か 400。**この試合だけ飛ばして続ける** —
            # cron の遅延で起きうる（詳細設計 4.1）
            log(f"  - skip {game_id} 内部APIが拒否した（tipoff 経過の可能性）")
            result.skipped_after_tipoff.append(game_id)
            continue
        result.predicted += 1
        if box is not None:
            result.with_box_score += 1
        result.rows.append(PredictedGame(
            game_id=game_id,
            home_win_prob=prediction.home_win_prob,
            pred_home_score=prediction.home_score,
            pred_away_score=prediction.away_score,
            model_version=models.versions["WINNER"],
            players=player_inputs(box, names, positions),
            reasons=tuple(
                ReasonInput(
                    group_key=r.group_key, label_ja=r.label_ja,
                    value_text=r.value_text, favors=r.favors,
                    contribution=r.contribution,
                )
                for r in reasons
            ),
            factors=tuple(
                FactorInput(
                    group_key=f.group_key, label_ja=f.label_ja,
                    value_text=f.value_text, larger=f.larger,
                )
                for f in factors
            ),
        ))
    return result


# --- ステップ5: 静的JSON の書き出し（詳細設計 4.2 の「静的JSON の書き出し」） ---

def write_json(
    ds: Dataset, result: InferenceResult, *, today: str, status: str,
    latest_result_date: str | None = None,
    verdicts: Mapping[str, EvaluationInput] | None = None,
    root: Path = DATA_DIR, log: Callable[[str], None] = print,
) -> int:
    """窓の全ファイルを書き直す。書いたファイル数を返す。

    **試合が1件も無い日も書く。** 書かないと古い `today.json` が残り、昨日の試合が
    「今日の試合」として配信され続ける。

    **推論に失敗したときは呼ばない**（前回のものを維持する。基本設計 4.3）。
    呼ぶかどうかの判断は呼び出し側にある。
    """
    today_list, upcoming, details = build_inputs(
        ds, result.rows, today=today, days=SCHEDULE_WINDOW_DAYS,
        verdicts=verdicts)
    written = write_static_json(
        today=today_list, upcoming=upcoming, details=details,
        generated_at=datetime.now(UTC).isoformat(timespec="seconds").replace(
            "+00:00", "Z"),
        data_as_of=result.data_as_of,
        last_run_status=status,
        model_versions=sorted(result.model_versions.values()),
        latest_result_date=latest_result_date,
        root=root,
    )
    log(
        f"daily_ingest: 静的JSON を書いた（{len(written.written)}件"
        f" / 削除 {len(written.removed)}件 / {root}）"
    )
    return len(written.written)


def record_exclusions(
    found: Mapping[str, list[ExcludedGame]], *,
    path: Path = exclusions.DEFAULT_PATH,
    log: Callable[[str], None] = print,
) -> int:
    """取り込まない試合の一覧をリポジトリへ書く（要件 5.3 / 詳細設計 4.4）。

    **`backfill` と違い、このジョブは自分でコミットできる**（`contents: write`）。
    一覧は試合IDで重ねるため、何度流しても同じ結果になる。
    """
    written = 0
    for season_id, games in found.items():
        if not games:
            continue
        exclusions.save(
            exclusions.merge(exclusions.load(path), games, season_id=season_id),
            path)
        written += len(games)
    if written:
        log(f"daily_ingest: 取り込まない試合を {written}件 一覧に残した")
    return written


def new_run_id() -> str:
    """`ingestion_logs.id` と `predictions.run_id` に使う値。

    **ジョブの先頭で1回だけ作る。** 予測行から実行を辿れるようにするためで
    （1.5 の `run_id` のコメント）、ログの中で作ると対応が取れない。
    """
    return f"daily-{uuid.uuid4().hex[:8]}"


def _log(api: InternalApi, status: str, rows: int, run_id: str) -> None:
    """`ingestion_logs` に記録する。**例外の本文を入れない**（絶対ルール4）。"""
    moment = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    try:
        api.post("log", {
            "id": run_id,
            "job": "daily_ingest",
            "startedAt": moment,
            "finishedAt": moment,
            "status": status,
            "rowsAffected": rows,
        })
    except LoaderError:
        print("  - ログの記録に失敗した")


def _client() -> RateLimitedClient:
    """スクレイピングの関門つきクライアント。**ステップ1 と 1b で同じ設定を使う。**

    **状態ファイルを共有する。** 日次3,000件のカウンタと 429/503 後の停止は
    このファイルが持つため、2つのステップを合わせて上限を守る（絶対ルール6）。
    """
    return RateLimitedClient(
        user_agent=os.environ.get("SCRAPER_USER_AGENT", ""),
        state_path=Path(os.environ.get("SCRAPER_STATE_PATH", str(DEFAULT_STATE_PATH))),
        robots_sha256=os.environ.get("SCRAPER_ROBOTS_SHA256") or None,
        terms_sha256=os.environ.get("SCRAPER_TERMS_SHA256") or None,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="日次の取り込み（詳細設計 4.2）")
    parser.add_argument(
        "--only-results", action="store_true",
        help="結果の取得（ステップ1 と2）を行う。窓は [前日, 当日]（JST）")
    parser.add_argument(
        "--only-upcoming", action="store_true",
        help="未実施の試合の取り込み（ステップ1b）を行う")
    parser.add_argument(
        "--only-settle", action="store_true",
        help="照合・集計・Elo 再計算（ステップ3）。外部サイトへはアクセスしない")
    parser.add_argument(
        "--only-inference", action="store_true",
        help="推論と静的JSON の書き出し（ステップ4と5）。外部サイトへはアクセスしない")
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--data", type=Path, default=DATA_DIR,
                        help="静的JSON の書き出し先")
    parser.add_argument("--dry-run", action="store_true", help="D1 には書かない")
    args = parser.parse_args(argv)

    # **どのステップを行うかを必ず明示させる。** 既定の動作を持たせない —
    # 全ステップが揃うまで「回したつもりで半分しか動いていない」が起きる
    if not (args.only_results or args.only_upcoming or args.only_settle
            or args.only_inference):
        parser.error(
            "--only-results / --only-upcoming / --only-settle / "
            "--only-inference のいずれかを指定する")

    api = InternalApi(
        os.environ.get("API_BASE_URL", ""),
        os.environ.get("INGEST_TOKEN", ""),
        dry_run=args.dry_run,
    )
    run_id = new_run_id()
    status = "SUCCESS"
    rows = 0

    if args.only_results:
        client = _client()
        try:
            finished = run_results(client, api, snapshot=args.snapshot)
        except PolicyError:
            print("daily_ingest: 取得前確認に失敗した（robots / 利用規約）", file=sys.stderr)
            return 1
        except (LoaderError, ParseError, ScraperError, SnapshotError) as error:
            # **型名だけにしない**（詳細設計 4.3）。これらは自前の文言を持ち、
            # URL も応答本文もトークンも含まない（絶対ルール4に触れない）
            print(f"daily_ingest: 前日の取得に失敗（{type(error).__name__}: {error}）",
                  file=sys.stderr)
            if not args.dry_run:
                _log(api, "FAILED", 0, run_id)
            return 1
        except Exception as error:  # noqa: BLE001 — 公開ログの境界で本文を除去する
            print(f"daily_ingest: 前日の取得に失敗（{type(error).__name__}）", file=sys.stderr)
            return 1

        print(
            f"daily_ingest: 結果 {yesterday_jst()}〜{jst_today()}"
            f" 取り込み={finished.ingested} 既取得={finished.already}"
            f" 窓の外={finished.other_days} 未終了={finished.unfinished}"
            f" 非リーグ戦={finished.non_league} 不正={finished.invalid}"
            f" 取得失敗={finished.unfetched} 日付不明={finished.skipped_undated}"
            f" 状態不明={finished.skipped_unresolved}"
            f" シーズン={','.join(finished.seasons) or 'なし'}"
            f" スナップショット={finished.snapshot_rows or 'なし'}"
        )
        for game_id, kind, message in finished.skipped:
            # **件数だけでは調査ができない**（詳細設計 4.4）
            print(f"  - skip {game_id} {kind} {message}")
        for note in finished.notes:
            print(f"  - {note}")
        if not args.dry_run:
            record_exclusions(finished.excluded)
        rows += finished.ingested
        if finished.status != "SUCCESS":
            status = "PARTIAL"

    if args.only_upcoming:
        client = _client()
        try:
            result = run_upcoming(client, api, snapshot=args.snapshot)
        except PolicyError:
            print("daily_ingest: 取得前確認に失敗した（robots / 利用規約）", file=sys.stderr)
            return 1
        except ScrapingStopped:
            # 429 / 503。**スクレイピング区間のみ中止する**（絶対ルール6）
            print("daily_ingest: 取得を中止した（相手側の応答）", file=sys.stderr)
            if not args.dry_run:
                _log(api, "PARTIAL", 0, run_id)
            return 1
        except (LoaderError, ParseError, ScraperError) as error:
            print(f"daily_ingest: 失敗（{type(error).__name__}: {error}）", file=sys.stderr)
            if not args.dry_run:
                _log(api, "FAILED", 0, run_id)
            return 1
        except Exception as error:  # noqa: BLE001 — 公開ログの境界で本文を除去する
            print(f"daily_ingest: 失敗（{type(error).__name__}）", file=sys.stderr)
            return 1

        print(
            f"daily_ingest: 取り込み={result.ingested} 窓の外={result.outside_window}"
            f" 終了済み={result.finished} 日付不明={result.skipped_undated}"
            f" 状態不明={result.skipped_unresolved}"
            f" クラブ一覧={result.club_options} シーズン={','.join(result.seasons) or 'なし'}"
            f" スナップショット={result.snapshot_rows or 'なし'}"
        )
        if result.unmatched_clubs:
            print(f"  クラブ一覧にない相手: {' / '.join(result.unmatched_clubs)}")
        rows += result.ingested

    # **推論より前に置く。** 推論はスナップショットの `team_ratings` を読むため、
    # 後に回すとその日の推論が古い Elo を使う（詳細設計 4.2 のステップ3）
    settled = Settled()
    if args.only_settle:
        settled = run_settle(api, snapshot=args.snapshot)
        if settled.status != "SUCCESS":
            status = "PARTIAL"

    if args.only_inference:
        try:
            ds = load_snapshot(args.snapshot)
            inferred = run_inference(api, ds=ds, run_id=run_id)
        except (PredictError, SnapshotError) as error:
            # **有効モデルが揃っていなければ推論を行わない**（PARTIAL）。
            # 前回の静的JSONを維持する（基本設計 4.3）
            print(f"daily_ingest: 推論を行わなかった（{type(error).__name__}: {error}）",
                  file=sys.stderr)
            if not args.dry_run:
                _log(api, "PARTIAL", rows, run_id)
            return 1
        except (LoaderError, ValueError) as error:
            print(f"daily_ingest: 推論に失敗（{type(error).__name__}: {error}）",
                  file=sys.stderr)
            if not args.dry_run:
                _log(api, "FAILED", rows, run_id)
            return 1
        except Exception as error:  # noqa: BLE001 — 公開ログの境界で本文を除去する
            print(f"daily_ingest: 推論に失敗（{type(error).__name__}）", file=sys.stderr)
            return 1

        print(
            f"daily_ingest: 予測={inferred.predicted}"
            f" 特徴量を作れず={len(inferred.skipped_features)}"
            f" 拒否={len(inferred.skipped_after_tipoff)}"
            f" data_as_of={inferred.data_as_of}"
            f" モデル={','.join(sorted(inferred.model_versions.values()))}"
        )
        if inferred.skipped_features or inferred.skipped_after_tipoff:
            status = "PARTIAL"
        rows += inferred.predicted
        # --- ステップ5。**推論が通ったときだけ書く**（基本設計 4.3） ---
        inferred.written = write_json(
            ds, inferred, today=jst_today(), status=status,
            latest_result_date=settled.latest_result_date,
            verdicts=settled.verdicts, root=args.data)

    if not args.dry_run:
        _log(api, status, rows, run_id)
    return 1 if status == "PARTIAL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
