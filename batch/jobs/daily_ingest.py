"""日次の取り込み（詳細設計 4.2 / 基本設計 4.2）。

**段階的に作っている。** 設計は12ステップを定めるが、いま実装してあるのは
**未実施の試合の取り込み（1b）と推論（4）**である。残りは順に足す。

| ステップ | 実装 |
|---|---|
| 0. robots / 利用規約のハッシュ照合 | **あり** |
| 1. 前日の結果取得 | まだ（`backfill` が同じ処理を持つ） |
| **1b. 未実施の試合の取り込み** | **あり**（`--only-upcoming`） |
| **2. スナップショット更新** | **あり**（1b が送った行を同じ本文から写す） |
| 3. 照合・集計・Elo | まだ（`batch.jobs.evaluate` / `recompute_ratings` が別に持つ） |
| **4. 推論** | **あり**（`--only-inference`） |
| 5. 静的JSON の書き出し | まだ（`batch.static_json` が別に持つ） |
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
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import pandas as pd

from batch.features.builder import FEATURE_KEYS, build_features
from batch.features.dataset import (
    Dataset,
    SnapshotError,
    load_snapshot,
    write_snapshot,
)
from batch.features.prepared import prepare
from batch.jobs.schedule_walk import walk_schedule
from batch.jobs.seed_master import Season, load_club_source_ids, load_seasons
from batch.loader.api import InternalApi, LoaderError, RejectedError
from batch.loader.limits import max_rows_per_request
from batch.loader.payload import (
    SeasonRef,
    prediction_payload,
    series_numbers,
    snapshot_rows,
    upcoming_games_payload,
)
from batch.model.dataset import as_of
from batch.model.predict import ActiveModels, PredictError, load_active
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
) -> Iterator[ScheduleGame]:
    """そのシーズンの日程を、窓の終わりを超えるまで辿る。

    **月で絞らない**（`mon=10` は読める行を1つも返さない。詳細設計 4.2 のステップ1b）。
    `index` を進めながら、ページの最終日が窓の終わりを超えたら止める。
    """
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

    _, end = window(jst_today())

    def past_the_window(page: SchedulePage) -> bool:
        """ページの最終日が窓の終わりを超えたら、以降のページは要らない。

        **ページは日付の昇順である**（実測。index 0 が開幕戦から始まる）。
        `mon` で月に絞る案は使えない — **読める行を1つも返さなかった**
        （詳細設計 4.2 のステップ1b）。
        """
        return page.last_date is not None and page.last_date > end

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
        games = pick_upcoming(list(_collect(client, season, result)), start, end, result)
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


# --- ステップ2: スナップショット更新（詳細設計 4.2 のステップ2） ---

#: ステップ1b が触るテーブル。**書き直すのはこの2つだけ**（基本設計 2.2 の部分書き出し）。
UPCOMING_TABLES = ("games", "team_games")

#: 行の同一性。**公式試合IDが主キーである**（詳細設計 1.3 / `team_games` は 1.3 の PK）。
KEYS: Mapping[str, tuple[str, ...]] = {
    "games": ("id",),
    "team_games": ("club_id", "game_date", "game_id"),
}


def apply_to_snapshot(
    ds: Dataset, rows: Mapping[str, list[dict[str, object]]],
) -> dict[str, int]:
    """送った行をスナップショットに upsert する。**D1 に送ったのと同じ値を書く。**

    **これが無いと推論が未実施の試合を見られない。** 推論の入力はスナップショット
    だけであり（絶対ルール3）、D1 にだけ書くと「取り込んだのに予測が作られない」
    状態になる（基本設計 2.2 が座標140件で踏んだのと同じ形）。

    **主キーで置き換える**（延期で `game_date` が変わっても別レコードにならない。
    詳細設計 1.3）。返すのは書いた行数。
    """
    written: dict[str, int] = {}
    for table, incoming in rows.items():
        if not incoming:
            continue
        if table not in KEYS:
            raise SnapshotError(f"スナップショットへの写し方が未定のテーブル: {table}")
        current = ds.table(table)
        frame = pd.DataFrame(incoming)
        missing = sorted(set(frame.columns) - set(current.columns))
        if missing:
            raise SnapshotError(f"{table} にない列を書こうとした: {missing}")
        keys = list(KEYS[table])
        index = {
            tuple(str(row[k]) for k in keys)
            for row in frame[keys].to_dict(orient="records")
        }
        kept = current[~current[keys].astype(str).agg(tuple, axis=1).isin(index)] \
            if not current.empty else current
        ds.tables[table] = pd.concat([kept, frame], ignore_index=True)[
            list(current.columns)
        ]
        written[table] = len(frame)
    return written


# --- ステップ4: 推論（詳細設計 4.2 の「推論（ステップ4）」） ---

#: スナップショットの置き場。**入力はここだけである**（絶対ルール3）。
DEFAULT_SNAPSHOT = Path("batch/snapshot")


@dataclass
class InferenceResult:
    """推論の集計。**飛ばした理由ごとに分けて数える**（詳細設計 4.4 と同じ方針）。"""

    predicted: int = 0
    #: 特徴量が作れなかった試合（試合IDを出す）
    skipped_features: list[str] = field(default_factory=list)
    #: tipoff を過ぎていて 409 を受けた試合（cron 遅延で起きうる）
    skipped_after_tipoff: list[str] = field(default_factory=list)
    data_as_of: str | None = None
    model_versions: dict[str, str] = field(default_factory=dict)
    #: ステップ5（静的JSON）に渡す。**D1 から読み戻さない**（詳細設計 4.2）
    rows: list[PredictedGame] = field(default_factory=list)
    #: 書き出したファイル数（0 なら書いていない）
    written: int = 0


def upcoming_for_inference(
    ds: Dataset, start: str, end: str, now: str,
) -> list[tuple[str, str, str]]:
    """推論の対象（`game_id` / `season_id` / `tipoff_at`）。

    **`SCHEDULED` だけを対象にする。** `POSTPONED` と `CANCELLED` は取り込むが
    （ステップ1b）、予測は作らない — 中止試合の予測は `VOID` として母数から
    外れるだけで、作る意味がない（詳細設計 4.2）。

    **`tipoff_at > now` で絞る。** 過ぎた試合に書くと API が 409 を返す（3.4 の
    関門3）。ここで落としておけば、通常の運用では 409 を受けない。
    """
    games = ds.table("games")
    picked = games[
        (games["status"] == "SCHEDULED")
        & (games["game_date"] >= start)
        & (games["game_date"] <= end)
        & (games["tipoff_at"] > now)
    ].sort_values(["tipoff_at", "id"])
    return [
        (str(row.id), str(row.season_id), str(row.tipoff_at))
        for row in picked.itertuples()
    ]


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
    result = InferenceResult(
        data_as_of=ds.max_finished_at, model_versions=dict(models.versions))
    if result.data_as_of is None:
        raise SnapshotError("スナップショットに終了した試合がない（data_as_of が出ない）")

    start, end = window(jst_today(moment))
    targets = upcoming_for_inference(ds, start, end, stamp)
    log(f"daily_ingest: 推論の対象 {len(targets)}試合（{start}〜{end}）")
    if not targets:
        return result

    # **索引は1回だけ作る**（詳細設計 2.1.1）。試合ごとに作ると桁が変わる
    prepared = prepare(ds)
    for game_id, season_id, tipoff_at in targets:
        try:
            # **`as_of` は必ず `games.tipoff_at` である**（用語集 / 2.1）。
            # 解釈は `batch/model/dataset.py` の `_as_of` と同じにする
            features = build_features(
                game_id, as_of(tipoff_at), ds, prepared)
            prediction = models.predict(features)
        except (ValueError, KeyError, PredictError) as error:
            # **件数だけでなく試合IDを出す**（詳細設計 4.4）。件数だけでは調査できない
            log(f"  - skip {game_id} {type(error).__name__}: {error}")
            result.skipped_features.append(game_id)
            continue
        try:
            api.post("predictions", prediction_payload(
                game_id=game_id, season_id=season_id, run_id=run_id,
                predicted_at=stamp, as_of=tipoff_at, data_as_of=result.data_as_of,
                prediction=prediction, features=features,
                model_versions=models.versions,
            ))
        except RejectedError:
            # 409（tipoff 経過）か 400。**この試合だけ飛ばして続ける** —
            # cron の遅延で起きうる（詳細設計 4.1）
            log(f"  - skip {game_id} 内部APIが拒否した（tipoff 経過の可能性）")
            result.skipped_after_tipoff.append(game_id)
            continue
        result.predicted += 1
        result.rows.append(PredictedGame(
            game_id=game_id,
            home_win_prob=prediction.home_win_prob,
            pred_home_score=prediction.home_score,
            pred_away_score=prediction.away_score,
            model_version=models.versions["WINNER"],
        ))
    return result


# --- ステップ5: 静的JSON の書き出し（詳細設計 4.2 の「静的JSON の書き出し」） ---

def write_json(
    ds: Dataset, result: InferenceResult, *, today: str, status: str,
    root: Path = DATA_DIR, log: Callable[[str], None] = print,
) -> int:
    """窓の全ファイルを書き直す。書いたファイル数を返す。

    **試合が1件も無い日も書く。** 書かないと古い `today.json` が残り、昨日の試合が
    「今日の試合」として配信され続ける。

    **推論に失敗したときは呼ばない**（前回のものを維持する。基本設計 4.3）。
    呼ぶかどうかの判断は呼び出し側にある。
    """
    today_list, upcoming, details = build_inputs(
        ds, result.rows, today=today, days=SCHEDULE_WINDOW_DAYS)
    written = write_static_json(
        today=today_list, upcoming=upcoming, details=details,
        generated_at=datetime.now(UTC).isoformat(timespec="seconds").replace(
            "+00:00", "Z"),
        data_as_of=result.data_as_of,
        last_run_status=status,
        model_versions=sorted(result.model_versions.values()),
        root=root,
    )
    log(
        f"daily_ingest: 静的JSON を書いた（{len(written.written)}件"
        f" / 削除 {len(written.removed)}件 / {root}）"
    )
    return len(written.written)


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="日次の取り込み（詳細設計 4.2）")
    parser.add_argument(
        "--only-upcoming", action="store_true",
        help="未実施の試合の取り込み（ステップ1b）を行う")
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
    if not (args.only_upcoming or args.only_inference):
        parser.error("--only-upcoming か --only-inference のどちらかを指定する")

    api = InternalApi(
        os.environ.get("API_BASE_URL", ""),
        os.environ.get("INGEST_TOKEN", ""),
        dry_run=args.dry_run,
    )
    run_id = new_run_id()
    status = "SUCCESS"
    rows = 0

    if args.only_upcoming:
        client = RateLimitedClient(
            user_agent=os.environ.get("SCRAPER_USER_AGENT", ""),
            state_path=Path(os.environ.get("SCRAPER_STATE_PATH", str(DEFAULT_STATE_PATH))),
            robots_sha256=os.environ.get("SCRAPER_ROBOTS_SHA256") or None,
            terms_sha256=os.environ.get("SCRAPER_TERMS_SHA256") or None,
        )
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
            ds, inferred, today=jst_today(), status=status, root=args.data)

    if not args.dry_run:
        _log(api, status, rows, run_id)
    return 1 if status == "PARTIAL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
