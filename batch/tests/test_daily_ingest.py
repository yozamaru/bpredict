"""日次の取り込み（詳細設計 4.2 のステップ1b）。合成データのみ。通信しない。

| テスト | どの規約か |
|---|---|
| `test_window_includes_today` | 当日を含める（開始前の試合がある） |
| `test_months_cross_the_boundary` | 月をまたぐなら2つ辿る |
| `test_seasons_are_chosen_by_the_csv_not_the_clock` | 時計で当季を決めない |
| `test_offseason_fetches_nothing` | オフシーズンは取得しない |
| `test_finished_games_are_left_to_step_one` | 終了済みはステップ1 が扱う |
| `test_postponed_and_cancelled_are_kept` | ゴースト試合を作らない |
| `test_requests_respect_the_row_limit` | 1リクエストの行数上限（3.4） |
| `test_only_upcoming_is_required` | **実装していないステップを黙って飛ばさない** |
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar

import pandas as pd
import pytest

from batch.features.dataset import Dataset
from batch.jobs import daily_ingest
from batch.jobs.daily_ingest import (
    ROWS_PER_REQUEST,
    UPCOMING_DAYS,
    Result,
    jst_today,
    main,
    months_of,
    pick_upcoming,
    seasons_of,
    send,
    upcoming_for_inference,
    window,
)
from batch.jobs.seed_master import Season
from batch.loader.api import RejectedError
from batch.model.predict import Prediction
from batch.parser.schedule_parser import ScheduleGame


class FakeModels:
    """`run_inference` が `ActiveModels` に求めるのは2つだけである。"""

    versions: ClassVar[dict[str, str]] = {
        "WINNER": "winner-v1.0.0", "MARGIN": "margin-v1.0.0",
        "TOTAL": "total-v1.0.0"}

    def predict(self, _features: Mapping[str, float]) -> Prediction:
        return Prediction(home_win_prob=0.68, margin=7.3, total=162.0,
                          home_score=84.65, away_score=77.35)


SEASON = Season("2026-27-PREMIER", "2026-27", "PREMIER", "2026-09-01", "2027-06-30")
PREVIOUS = Season("2025-26-B1", "2025-26", "B1", "2025-09-01", "2026-06-30")


def game(game_id: str, game_date: str, status: str = "SCHEDULED") -> ScheduleGame:
    return ScheduleGame(
        game_id=game_id, competition="REGULAR", game_date=game_date,
        tipoff_at=f"{game_date}T10:05:00Z",
        home_source_id="703", away_source_id="704",
        home_name="架空ブ", away_name="架空タ",
        home_score=None, away_score=None, status=status,
        source_url="https://example.invalid/schedule/",
    )


# --- 窓 ---

def test_jst_today_converts_from_utc() -> None:
    """**JST の暦日**（CLAUDE.md 時刻の扱い）。UTC の 15:00 は翌日である。"""
    assert jst_today(datetime(2026, 10, 4, 15, 0, tzinfo=UTC)) == "2026-10-05"
    assert jst_today(datetime(2026, 10, 4, 14, 59, tzinfo=UTC)) == "2026-10-04"


def test_window_includes_today() -> None:
    """**当日を含める。** 当日の試合はまだ始まっていないことがあり、
    `tipoff_at > now` の絞り込みは推論側（ステップ9）が行う。"""
    start, end = window("2026-10-04")
    assert start == "2026-10-04"
    assert end == "2026-10-11"
    assert UPCOMING_DAYS == 7


def test_months_cross_the_boundary() -> None:
    """月をまたぐなら2つ辿る（詳細設計 4.2 のステップ1b）。"""
    assert months_of("2026-10-04", "2026-10-11") == [10]
    assert months_of("2026-10-28", "2026-11-04") == [10, 11]
    assert months_of("2026-12-30", "2027-01-06") == [12, 1]


# --- シーズンの決め方 ---

def test_seasons_are_chosen_by_the_csv_not_the_clock() -> None:
    """**時計で当季を決めない。** `seasons.csv` の期間で決める（詳細設計 1.1）。"""
    start, end = window("2026-10-04")
    assert [s.id for s in seasons_of(start, end, [PREVIOUS, SEASON])] == [SEASON.id]


def test_a_window_touching_two_seasons_returns_both() -> None:
    """**重なるシーズンをすべて返す。** 期間は上位集合なので重なりうる。"""
    picked = seasons_of("2026-06-29", "2026-09-05", [PREVIOUS, SEASON])
    assert {s.id for s in picked} == {PREVIOUS.id, SEASON.id}


def test_offseason_fetches_nothing() -> None:
    """**オフシーズンは取得を1回も行わない。** 年間の1/3以上がオフである（要件 8.5）。"""
    assert seasons_of("2027-07-10", "2027-07-17", [PREVIOUS, SEASON]) == []


# --- 窓と状態の絞り込み ---

def test_games_outside_the_window_are_counted_not_sent() -> None:
    result = Result()
    picked = pick_upcoming(
        [game("1", "2026-10-04"), game("2", "2026-10-20")],
        "2026-10-04", "2026-10-11", result)
    assert [g.game_id for g in picked] == ["1"]
    assert result.outside_window == 1


def test_finished_games_are_left_to_step_one() -> None:
    """終了済みはステップ1（ボックススコアの取り込み）が扱う。**ここでは数えるだけ。**"""
    result = Result()
    picked = pick_upcoming(
        [game("1", "2026-10-05", "FINISHED"), game("2", "2026-10-05")],
        "2026-10-04", "2026-10-11", result)
    assert [g.game_id for g in picked] == ["2"]
    assert result.finished == 1


@pytest.mark.parametrize("status", ["POSTPONED", "CANCELLED"])
def test_postponed_and_cancelled_are_kept(status: str) -> None:
    """**ゴースト試合を作らない。**

    `SCHEDULED` だけを入れると、中止になった試合が `SCHEDULED` のまま残り
    予測が作られ続ける（詳細設計 1.3）。
    """
    result = Result()
    picked = pick_upcoming([game("1", "2026-10-05", status)],
                           "2026-10-04", "2026-10-11", result)
    assert len(picked) == 1


def test_the_window_is_inclusive_at_both_ends() -> None:
    result = Result()
    picked = pick_upcoming(
        [game("1", "2026-10-04"), game("2", "2026-10-11")],
        "2026-10-04", "2026-10-11", result)
    assert len(picked) == 2


# --- 送信 ---

@dataclass
class FakeApi:
    """送った内容を覚えるだけ。

    **引数は `Mapping` で受ける。** `dict` で受けると `Poster` を満たさない
    （引数の型は反変であり、より狭い型しか受けない実装は差し替えにならない）。
    """

    posted: list[tuple[str, Mapping[str, object]]]

    def post(self, path: str, payload: Mapping[str, object]) -> object:
        self.posted.append((path, payload))
        return None


def test_requests_respect_the_row_limit() -> None:
    """**1リクエストの行数上限を守る**（詳細設計 3.4。`games` は160行）。"""
    api = FakeApi(posted=[])
    games = [game(str(i), "2026-10-05") for i in range(ROWS_PER_REQUEST + 5)]
    sent = send(api, games, season=SEASON,
                club_ids={"703": "703", "704": "704"}, fetched_at="2026-10-04T00:00:00Z")
    assert sent == len(games)
    assert len(api.posted) == 2
    for _path, body in api.posted:
        rows = body["games"]
        assert isinstance(rows, list) and len(rows) <= ROWS_PER_REQUEST


def test_nothing_is_sent_when_there_is_no_game() -> None:
    api = FakeApi(posted=[])
    assert send(api, [], season=SEASON, club_ids={},
                fetched_at="2026-10-04T00:00:00Z") == 0
    assert api.posted == []


def test_series_numbers_are_attached() -> None:
    """同一カードの連戦番号を付ける（`series_numbers` と同じ規約）。"""
    api = FakeApi(posted=[])
    send(api, [game("1", "2026-10-04"), game("2", "2026-10-05")], season=SEASON,
         club_ids={"703": "703", "704": "704"}, fetched_at="2026-10-04T00:00:00Z")
    rows = api.posted[0][1]["games"]
    assert isinstance(rows, list)
    assert [r["seriesGameNo"] for r in rows] == [1, 2]


# --- 未実装のステップを黙って飛ばさない ---

def test_only_upcoming_is_required() -> None:
    """**既定の動作を持たせない。**

    設計は12ステップを定めるが、実装してあるのはステップ1b だけである。
    「日次ジョブを回したつもりで半分しか動いていない」が最も危ない。
    """
    with pytest.raises(SystemExit):
        main([])


# --- ステップ4: 推論 ---

def snapshot_dataset(
    *, status: str = "SCHEDULED", game_date: str = "2026-10-06",
) -> Dataset:
    """推論の対象を引くのに必要な列だけを持つ最小のスナップショット。"""
    games = pd.DataFrame([
        # 終了した試合（`data_as_of` の出どころ）
        {"id": "past", "season_id": "2026-27-PREMIER", "status": "FINISHED",
         "game_date": "2026-10-01", "tipoff_at": "2026-10-01T10:05:00Z",
         "finished_at": "2026-10-01T12:05:00Z"},
        {"id": "g1", "season_id": "2026-27-PREMIER", "status": status,
         "game_date": game_date, "tipoff_at": f"{game_date}T10:05:00Z",
         "finished_at": None},
    ])
    return Dataset(tables={"games": games})


def test_only_scheduled_games_are_predicted() -> None:
    """**`POSTPONED` と `CANCELLED` には予測を作らない**（詳細設計 4.2）。

    取り込みはするが（ステップ1b）、予測は作らない — 中止試合の予測は `VOID` と
    して母数から外れるだけで、作る意味がない。
    """
    now = "2026-10-05T12:00:00Z"
    assert upcoming_for_inference(
        snapshot_dataset(), "2026-10-05", "2026-10-12", now) == [
        ("g1", "2026-27-PREMIER", "2026-10-06T10:05:00Z")]
    for status in ("POSTPONED", "CANCELLED", "FINISHED"):
        assert upcoming_for_inference(
            snapshot_dataset(status=status), "2026-10-05", "2026-10-12", now) == []


def test_games_that_already_started_are_not_predicted() -> None:
    """`tipoff_at > now` で絞る。**過ぎた試合に書くと API が 409 を返す**（3.4）。"""
    started = snapshot_dataset(game_date="2026-10-05")
    assert upcoming_for_inference(
        started, "2026-10-05", "2026-10-12", "2026-10-05T12:00:00Z") == []
    assert upcoming_for_inference(
        started, "2026-10-05", "2026-10-12", "2026-10-05T09:00:00Z") != []


def test_games_outside_the_window_are_not_predicted() -> None:
    """窓は当日から7日（閉区間）。"""
    far = snapshot_dataset(game_date="2026-10-20")
    assert upcoming_for_inference(
        far, "2026-10-05", "2026-10-12", "2026-10-05T12:00:00Z") == []


def test_a_rejected_game_does_not_stop_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """409 を受けた試合だけを飛ばして続ける（cron 遅延で起きうる。4.1）。"""
    sent: list[str] = []

    class Rejecting:
        def post(self, path: str, payload: Mapping[str, object]) -> object:
            sent.append(str(payload["gameId"]))
            raise RejectedError("内部APIが入力を拒否した（409 / predictions）")

        def get(self, path: str, query: Mapping[str, str] | None = None) -> object:
            raise AssertionError("get は呼ばれない")

    monkeypatch.setattr(daily_ingest, "load_snapshot", lambda _p: snapshot_dataset())
    monkeypatch.setattr(daily_ingest, "prepare", lambda _ds: None)
    monkeypatch.setattr(daily_ingest, "build_features", lambda *a, **k: {"x": 1.0})
    monkeypatch.setattr(daily_ingest, "load_active", lambda *a, **k: FakeModels())
    result = daily_ingest.run_inference(
        Rejecting(),  # type: ignore[arg-type]
        run_id="daily-test",
        now=datetime(2026, 10, 5, 12, 0, tzinfo=UTC), log=lambda _m: None)
    assert result.predicted == 0
    assert result.skipped_after_tipoff == ["g1"]
    assert sent == ["g1"]


def test_a_game_without_features_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """特徴量が作れない試合は飛ばして続ける。**件数と試合IDを出す。**"""
    def boom(*_a: object, **_k: object) -> dict[str, float]:
        raise ValueError("特徴量のキーが定義と一致しない")

    monkeypatch.setattr(daily_ingest, "load_snapshot", lambda _p: snapshot_dataset())
    monkeypatch.setattr(daily_ingest, "prepare", lambda _ds: None)
    monkeypatch.setattr(daily_ingest, "build_features", boom)
    monkeypatch.setattr(daily_ingest, "load_active", lambda *a, **k: FakeModels())
    api = FakeApi(posted=[])
    result = daily_ingest.run_inference(
        api, run_id="daily-test",  # type: ignore[arg-type]
        now=datetime(2026, 10, 5, 12, 0, tzinfo=UTC), log=lambda _m: None)
    assert result.skipped_features == ["g1"]
    assert api.posted == []


def test_the_run_id_is_shared_with_the_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """**`predictions.run_id` と `ingestion_logs.id` が同じ値であること**（1.5）。

    ログの中で採番すると、予測行から実行を辿れない。
    """
    monkeypatch.setattr(daily_ingest, "load_snapshot", lambda _p: snapshot_dataset())
    monkeypatch.setattr(daily_ingest, "prepare", lambda _ds: None)
    monkeypatch.setattr(daily_ingest, "build_features", lambda *a, **k: {"x": 1.0})
    monkeypatch.setattr(daily_ingest, "load_active", lambda *a, **k: FakeModels())
    monkeypatch.setattr(daily_ingest, "new_run_id", lambda: "daily-fixed")
    monkeypatch.setattr(daily_ingest, "jst_today", lambda now=None: "2026-10-05")
    monkeypatch.setattr(
        daily_ingest, "InternalApi", lambda *a, **k: FakeApi(posted=[]))

    api = FakeApi(posted=[])
    monkeypatch.setattr(daily_ingest, "InternalApi", lambda *a, **k: api)
    monkeypatch.setenv("API_BASE_URL", "https://example.test")
    monkeypatch.setenv("INGEST_TOKEN", "t")
    assert main(["--only-inference"]) == 0
    paths = {path for path, _ in api.posted}
    assert paths == {"predictions", "log"}
    by_path = dict(api.posted)
    assert by_path["predictions"]["runId"] == "daily-fixed"
    assert by_path["log"]["id"] == "daily-fixed"


def test_either_step_must_be_requested() -> None:
    """**既定の動作を持たせない。** どのステップを行うか必ず明示させる。"""
    with pytest.raises(SystemExit):
        main([])
