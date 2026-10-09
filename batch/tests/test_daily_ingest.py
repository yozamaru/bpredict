"""日次の取り込み（詳細設計 4.2 のステップ1b）。合成データのみ。通信しない。

| テスト | どの規約か |
|---|---|
| `test_window_includes_today` | 当日を含める（開始前の試合がある） |
| `test_seasons_are_chosen_by_the_csv_not_the_clock` | 時計で当季を決めない |
| `test_offseason_fetches_nothing` | オフシーズンは取得しない |
| `test_finished_games_are_left_to_step_one` | 終了済みはステップ1 が扱う |
| `test_postponed_and_cancelled_are_kept` | ゴースト試合を作らない |
| `test_requests_respect_the_row_limit` | 1リクエストの行数上限（3.4） |
| `test_only_upcoming_is_required` | **実装していないステップを黙って飛ばさない** |
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import numpy as np
import pandas as pd
import pytest

from batch.features.builder import DEFAULTS, FEATURE_KEYS
from batch.features.dataset import Dataset, SnapshotError
from batch.jobs import daily_ingest
from batch.jobs.daily_ingest import (
    ROWS_PER_REQUEST,
    UPCOMING_DAYS,
    Result,
    jst_today,
    main,
    pick_upcoming,
    seasons_of,
    send,
    upcoming_for_inference,
    window,
)
from batch.jobs.seed_master import Season
from batch.loader.api import RejectedError
from batch.loader.payload import (
    SeasonRef,
    snapshot_rows,
    upcoming_games_payload,
)
from batch.model.explain import Explainer
from batch.model.predict import Prediction
from batch.parser.schedule_parser import ScheduleGame
from batch.static_json.from_snapshot import PredictedGame
from batch.static_json.writer import read_game_index


class FakeModels:
    """`run_inference` が `ActiveModels` に求めるのは3つだけである。

    **`explainer` は本物を持つ。** 文言表（詳細設計 2.7.1）を通さないと、
    `label_ja` と `value_text` がここで固定されてしまい、本番と違う形で通る。
    """

    versions: ClassVar[dict[str, str]] = {
        "WINNER": "winner-v1.0.0", "MARGIN": "margin-v1.0.0",
        "TOTAL": "total-v1.0.0"}
    #: 係数1・平均0 なので寄与は特徴量の値そのものになる。`PLAYER` の3列は
    #: 既定値 0 で寄与も 0 になり、**本番と同じく行が作られない**
    explainer: ClassVar[Explainer] = Explainer(
        features=tuple(FEATURE_KEYS),
        intercept=0.0,
        coefficients=np.ones(len(FEATURE_KEYS)),
        means=np.zeros(len(FEATURE_KEYS)),
    )

    def predict(self, _features: Mapping[str, float]) -> Prediction:
        return Prediction(home_win_prob=0.68, margin=7.3, total=162.0,
                          home_score=84.65, away_score=77.35)


SEASON = Season("2026-27-PREMIER", "2026-27", "PREMIER", "2026-09-01", "2027-06-30")
PREVIOUS = Season("2025-26-B1", "2025-26", "B1", "2025-09-01", "2026-06-30")
SEASON_REF = SeasonRef(SEASON.id, SEASON.label, SEASON.league)


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

#: `games` の全列（DDL の順。詳細設計 1.3）。**部分集合にしない** —
#: ステップ2 は「スナップショットに無い列を書こうとしたら落とす」ため、
#: 列を削ると本物では通る操作がテストでだけ落ちる。
GAME_COLUMNS = (
    "id", "season_id", "league", "competition", "game_date", "tipoff_at",
    "finished_at", "finished_at_is_estimated", "home_club_id", "away_club_id",
    "venue_id", "venue_name_at_game", "is_primary_venue", "series_game_no",
    "status", "rescheduled_to", "home_score", "away_score", "attendance",
    "spectator_restricted", "result_revision", "source_url", "fetched_at",
    "created_at", "updated_at",
)


def snapshot_dataset(
    *, status: str = "SCHEDULED", game_date: str = "2026-10-06",
) -> Dataset:
    """推論とステップ2 に必要な列を持つスナップショット。"""
    games = pd.DataFrame([
        # 終了した試合（`data_as_of` の出どころ）
        {"id": "past", "season_id": "2026-27-PREMIER", "status": "FINISHED",
         "game_date": "2026-10-01", "tipoff_at": "2026-10-01T10:05:00Z",
         "finished_at": "2026-10-01T12:05:00Z", "competition": "REGULAR",
         "league": "PREMIER", "home_club_id": "703", "away_club_id": "704",
         "venue_name_at_game": None, "home_score": 88, "away_score": 81},
        {"id": "g1", "season_id": "2026-27-PREMIER", "status": status,
         "game_date": game_date, "tipoff_at": f"{game_date}T10:05:00Z",
         "finished_at": None, "competition": "REGULAR",
         "league": "PREMIER", "home_club_id": "703", "away_club_id": "704",
         "venue_name_at_game": None, "home_score": None, "away_score": None},
    ])
    # **`clubs` は必須**（slug が導線になる）。`club_seasons` は空でよい —
    # 当季は最初の試合が終わるまで存在しない（詳細設計 4.2 のステップ5）
    clubs = pd.DataFrame([
        {"id": "703", "slug": "club-703", "name": "架空ブ"},
        {"id": "704", "slug": "club-704", "name": "架空タ"},
    ])
    for column in GAME_COLUMNS:
        if column not in games.columns:
            games[column] = None
    games = games[list(GAME_COLUMNS)]
    team_games = pd.DataFrame(
        columns=["game_id", "club_id", "opponent_id", "season_id", "game_date",
                 "finished_at", "is_home", "competition", "result", "margin"])
    return Dataset(tables={
        "games": games, "clubs": clubs, "team_games": team_games})


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

    monkeypatch.setattr(daily_ingest, "prepare", lambda _ds: None)
    monkeypatch.setattr(
        daily_ingest, "build_features", lambda *a, **k: dict(DEFAULTS))
    monkeypatch.setattr(daily_ingest, "load_active", lambda *a, **k: FakeModels())
    # 30本は揃っていない状態（勝敗だけを書く。詳細設計 4.2）
    monkeypatch.setattr(daily_ingest, "load_active_rates", lambda *a, **k: None)
    result = daily_ingest.run_inference(
        Rejecting(),  # type: ignore[arg-type]
        ds=snapshot_dataset(), run_id="daily-test",
        now=datetime(2026, 10, 5, 12, 0, tzinfo=UTC), log=lambda _m: None)
    assert result.predicted == 0
    assert result.skipped_after_tipoff == ["g1"]
    assert sent == ["g1"]


def test_a_game_without_features_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """特徴量が作れない試合は飛ばして続ける。**件数と試合IDを出す。**"""
    def boom(*_a: object, **_k: object) -> dict[str, float]:
        raise ValueError("特徴量のキーが定義と一致しない")

    monkeypatch.setattr(daily_ingest, "prepare", lambda _ds: None)
    monkeypatch.setattr(daily_ingest, "build_features", boom)
    monkeypatch.setattr(daily_ingest, "load_active", lambda *a, **k: FakeModels())
    # 30本は揃っていない状態（勝敗だけを書く。詳細設計 4.2）
    monkeypatch.setattr(daily_ingest, "load_active_rates", lambda *a, **k: None)
    api = FakeApi(posted=[])
    result = daily_ingest.run_inference(
        api, ds=snapshot_dataset(), run_id="daily-test",  # type: ignore[arg-type]
        now=datetime(2026, 10, 5, 12, 0, tzinfo=UTC), log=lambda _m: None)
    assert result.skipped_features == ["g1"]
    assert api.posted == []


class _FrozenDatetime(datetime):
    """`datetime.now()` だけを固定する。**テストを実行日に依存させない。**

    `daily_ingest` は `from datetime import datetime` で名前を取り込んでいるため、
    モジュール属性を差し替えれば `datetime.now(UTC)` の呼び出しがすべてここを通る。
    継承しているので `fromisoformat` などはそのまま動く。
    """

    @classmethod
    def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
        return datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def test_the_run_id_is_shared_with_the_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """**`predictions.run_id` と `ingestion_logs.id` が同じ値であること**（1.5）。

    ログの中で採番すると、予測行から実行を辿れない。
    """
    monkeypatch.setattr(daily_ingest, "load_snapshot", lambda _p: snapshot_dataset())
    monkeypatch.setattr(daily_ingest, "prepare", lambda _ds: None)
    monkeypatch.setattr(
        daily_ingest, "build_features", lambda *a, **k: dict(DEFAULTS))
    monkeypatch.setattr(daily_ingest, "load_active", lambda *a, **k: FakeModels())
    # 30本は揃っていない状態（勝敗だけを書く。詳細設計 4.2）
    monkeypatch.setattr(daily_ingest, "load_active_rates", lambda *a, **k: None)
    monkeypatch.setattr(daily_ingest, "new_run_id", lambda: "daily-fixed")
    monkeypatch.setattr(daily_ingest, "jst_today", lambda now=None: "2026-10-05")
    # **時計を止める。** 窓の日付だけを固定して `tipoff_at > now` の比較を実時刻に
    # 任せていたため、**2026-10-07 に実行したら対象0試合になって落ちた**
    # （フィクスチャの試合が過去になった）。`main` は `now` を受け取らないので、
    # モジュールが見ている `datetime` を差し替える
    monkeypatch.setattr(daily_ingest, "datetime", _FrozenDatetime)
    monkeypatch.setattr(
        daily_ingest, "InternalApi", lambda *a, **k: FakeApi(posted=[]))

    api = FakeApi(posted=[])
    monkeypatch.setattr(daily_ingest, "InternalApi", lambda *a, **k: api)
    monkeypatch.setenv("API_BASE_URL", "https://example.test")
    monkeypatch.setenv("INGEST_TOKEN", "t")
    assert main(["--only-inference", "--data", str(tmp_path)]) == 0
    paths = {path for path, _ in api.posted}
    assert paths == {"predictions", "log"}
    by_path = dict(api.posted)
    assert by_path["predictions"]["runId"] == "daily-fixed"
    assert by_path["log"]["id"] == "daily-fixed"


def test_either_step_must_be_requested() -> None:
    """**既定の動作を持たせない。** どのステップを行うか必ず明示させる。"""
    with pytest.raises(SystemExit):
        main([])


# --- ステップ5: 静的JSON の書き出し ---

def test_static_json_is_written_for_the_whole_window(tmp_path: Path) -> None:
    """窓の全ファイルを書く（`today` ＋ 7日 ＋ 当日の詳細 ＋ `meta`）。"""
    result = daily_ingest.InferenceResult(
        predicted=1, data_as_of="2026-10-01T12:05:00Z",
        model_versions={"WINNER": "winner-v1.0.0"},
        rows=[PredictedGame("g1", 0.68, 84.6, 77.4, "winner-v1.0.0")],
    )
    written = daily_ingest.write_json(
        snapshot_dataset(game_date="2026-10-05"), result,
        today="2026-10-05", status="SUCCESS", root=tmp_path, log=lambda _m: None)
    # today.json ＋ schedule 7日 ＋ 当日の詳細1件 ＋ games/index.json ＋ meta.json
    assert written == 1 + 7 + 1 + 1 + 1
    today = json.loads((tmp_path / "today.json").read_text(encoding="utf-8"))
    game = today["data"]["games"][0]
    assert game["prediction"]["homeWinProb"] == pytest.approx(0.68)
    # **予測がある試合は `prediction` が入る。** 丸めない（画面で丸める）
    assert game["prediction"]["predHomeScore"] == pytest.approx(84.6)


def test_static_json_is_written_even_with_no_games(tmp_path: Path) -> None:
    """**試合が1件も無い日も書く。**

    書かないと古い `today.json` が残り、**昨日の試合が「今日の試合」として
    配信され続ける**（詳細設計 4.2 のステップ5）。
    """
    result = daily_ingest.InferenceResult(data_as_of="2026-10-01T12:05:00Z")
    written = daily_ingest.write_json(
        snapshot_dataset(game_date="2026-12-01"), result,
        today="2026-10-05", status="SUCCESS", root=tmp_path, log=lambda _m: None)
    assert written == 1 + 7 + 1 + 1      # 詳細は0件。索引は毎回書く（3.7）
    today = json.loads((tmp_path / "today.json").read_text(encoding="utf-8"))
    assert today["data"]["games"] == []
    assert today["data"]["gameDate"] == "2026-10-05"


def test_the_game_index_accumulates_through_the_job(tmp_path: Path) -> None:
    """**窓から出た試合のIDが索引に残ること**（詳細設計 3.7 / 5.6）。

    これが無いと、昨日の試合を押したときに 404 になる（2026-10-08 に運営者が
    実際に踏んだ）。`write_static_json` 単体では検査済みだが、**ジョブが
    `prediction` を渡している経路**をここで固定する。
    """
    def run(game_date: str, today: str) -> None:
        daily_ingest.write_json(
            snapshot_dataset(game_date=game_date),
            daily_ingest.InferenceResult(
                predicted=1, data_as_of="2026-10-01T12:05:00Z",
                model_versions={"WINNER": "winner-v1.0.0"},
                rows=[PredictedGame("g1", 0.68, 84.6, 77.4, "winner-v1.0.0")],
            ),
            today=today, status="SUCCESS", root=tmp_path, log=lambda _m: None)

    run("2026-10-05", "2026-10-05")
    assert read_game_index(tmp_path) == ["g1"]
    # 窓が進んで g1 が外に出ても、索引からは消えない
    run("2026-12-01", "2026-11-20")
    assert read_game_index(tmp_path) == ["g1"]
    assert not (tmp_path / "games" / "g1.json").exists()


def test_a_game_without_a_prediction_is_not_indexed(tmp_path: Path) -> None:
    """**予測の無い試合を索引に入れない**（詳細設計 3.7）。

    入れても「この試合の予測はまだありません」と出るだけで、**この画面の用途
    （予想と実際の対比）が成立しない**。
    """
    result = daily_ingest.InferenceResult(data_as_of="2026-10-01T12:05:00Z")
    daily_ingest.write_json(
        snapshot_dataset(game_date="2026-10-05"), result,
        today="2026-10-05", status="SUCCESS", root=tmp_path, log=lambda _m: None)
    assert read_game_index(tmp_path) == []


def test_a_game_without_a_prediction_keeps_the_key(tmp_path: Path) -> None:
    """予測が無い試合は `prediction: null`。**キーごと消さない**（詳細設計 3.3）。"""
    result = daily_ingest.InferenceResult(data_as_of="2026-10-01T12:05:00Z")
    daily_ingest.write_json(
        snapshot_dataset(game_date="2026-10-05"), result,
        today="2026-10-05", status="SUCCESS", root=tmp_path, log=lambda _m: None)
    today = json.loads((tmp_path / "today.json").read_text(encoding="utf-8"))
    assert today["data"]["games"][0]["prediction"] is None


def test_details_cover_only_today(tmp_path: Path) -> None:
    """詳細は**当日の試合だけ**（7日窓にすると年460MB 積む。詳細設計 3.7）。"""
    result = daily_ingest.InferenceResult(data_as_of="2026-10-01T12:05:00Z")
    daily_ingest.write_json(
        snapshot_dataset(game_date="2026-10-08"), result,
        today="2026-10-05", status="SUCCESS", root=tmp_path, log=lambda _m: None)
    # **索引（`index.json`）は窓の対象外である**（詳細設計 3.7）。溜まるファイルで
    # あり、「当日の詳細だけ」という規則の外にある
    assert [p.name for p in (tmp_path / "games").glob("*.json")] == ["index.json"]
    schedule = json.loads(
        (tmp_path / "schedule" / "2026-10-08.json").read_text(encoding="utf-8"))
    assert [g["gameId"] for g in schedule["data"]["games"]] == ["g1"]


def test_club_names_are_null_when_the_season_slice_is_empty(tmp_path: Path) -> None:
    """**`clubs.name` で埋めない。** 当季の `club_seasons` は空でありうる。

    公開APIも null を返す（既知の判断待ち）。ここで現在の表示名を入れると、
    過去試合の表示が遡って変わる経路ができる（詳細設計 1.2）。
    """
    result = daily_ingest.InferenceResult(data_as_of="2026-10-01T12:05:00Z")
    daily_ingest.write_json(
        snapshot_dataset(game_date="2026-10-05"), result,
        today="2026-10-05", status="SUCCESS", root=tmp_path, log=lambda _m: None)
    today = json.loads((tmp_path / "today.json").read_text(encoding="utf-8"))
    home = today["data"]["games"][0]["home"]
    assert home["slug"] == "club-703"
    assert home["name"] is None
    assert home["shortName"] is None


def test_written_files_satisfy_the_contract(tmp_path: Path) -> None:
    """**実際に書いたファイル**が契約（`contracts/public-shapes.json`）に適合する。

    `test_static_json.py` は `builder` の出力を見ており、こちらは
    **`build_inputs` → `builder` → `writer` の経路**を通した結果を見る。
    入力の組み立てでキーが落ちないことを固定する。

    **契約は「全項目が埋まった形」である。** 入れ子のオブジェクトが null のときは
    その下のキーが消えるため（`accuracy: null` など）、**消えたキーには null の
    祖先があること**を要求する。これは 3.7 の「`null` を取りうるキーも常に存在する」
    と同じ要求を、入れ子に対して言い直したものである。
    """
    contract = json.loads(
        (Path(__file__).resolve().parents[2] / "contracts" / "public-shapes.json")
        .read_text(encoding="utf-8"))
    result = daily_ingest.InferenceResult(
        predicted=1, data_as_of="2026-10-01T12:05:00Z",
        model_versions={"WINNER": "winner-v1.0.0"},
        rows=[PredictedGame("g1", 0.68, 84.6, 77.4, "winner-v1.0.0")],
    )
    daily_ingest.write_json(
        snapshot_dataset(game_date="2026-10-05"), result,
        today="2026-10-05", status="SUCCESS", root=tmp_path, log=lambda _m: None)

    def walk(value: object, prefix: str = "") -> tuple[set[str], set[str]]:
        """(キーのパス, 値が null のパス)。"""
        if isinstance(value, dict):
            found: set[str] = set()
            empty: set[str] = set()
            for key, child in value.items():
                a, b = walk(child, f"{prefix}.{key}" if prefix else key)
                found |= a
                empty |= b
            return found, empty
        if isinstance(value, list):
            if not value:
                return {f"{prefix}[]"}, set()
            found = set()
            empty = set()
            for child in value:
                a, b = walk(child, f"{prefix}[]")
                found |= a
                empty |= b
            return found, empty
        return {prefix}, ({prefix} if value is None else set())

    for name, shape in (
        ("today.json", "gamesByDate"),
        ("games/g1.json", "gameDetail"),
        ("meta.json", "meta"),
    ):
        loaded = json.loads((tmp_path / name).read_text(encoding="utf-8"))
        out, nulls = walk(loaded)
        expected = set(contract[shape]["paths"])

        # **余分なキーを出していない。** 畳まれたもの（`accuracy: null` /
        # `reasons: []`）は、その下に契約のキーがあるパスとして許す
        for path in out - expected:
            assert any(e.startswith(path) for e in expected), \
                f"{name}: 契約に無いキー {path}"
        # **契約のキーが消えているなら、畳まれた祖先が出力にあること**
        for path in expected - out:
            assert any(path.startswith(o) for o in out), \
                f"{name}: {path} が消えているが畳まれた祖先が無い"
        assert nulls <= out


# --- ステップ2: スナップショット更新 ---

def test_the_sent_rows_are_written_to_the_snapshot() -> None:
    """**D1 に送ったのと同じ本文から写す**（基本設計 2.2）。

    これが無いと**推論が未実施の試合を見られない** — 推論の入力はスナップショット
    だけであり（絶対ルール3）、D1 にだけ書くと「取り込んだのに予測が作られない」
    状態になる。座標140件で踏んだのと同じ形である。
    """
    ds = snapshot_dataset()
    body = upcoming_games_payload(
        [game("new-1", "2026-10-07")], season=SEASON_REF,
        club_ids={"703": "703", "704": "704"},
        series_game_no={"new-1": 1}, fetched_at="2026-10-05T00:00:00Z")
    written = daily_ingest.apply_to_snapshot(ds, snapshot_rows(body))
    assert written == {"games": 1, "team_games": 2}
    games = ds.table("games")
    assert "new-1" in set(games["id"])
    row = games[games["id"] == "new-1"].iloc[0]
    assert row["status"] == "SCHEDULED"
    assert row["tipoff_at"] == "2026-10-07T10:05:00Z"
    # **DDL の DEFAULT を写す**（D1 が入れる値をこちらでも入れる）
    assert int(row["is_primary_venue"]) == 1
    assert int(row["result_revision"]) == 0


def test_the_upsert_replaces_by_primary_key() -> None:
    """**主キーで置き換える。** 2回流して行が増えないこと（冪等。詳細設計 4.4）。"""
    ds = snapshot_dataset()
    before = len(ds.table("games"))
    body = upcoming_games_payload(
        [game("g1", "2026-10-06")], season=SEASON_REF,
        club_ids={"703": "703", "704": "704"},
        series_game_no={"g1": 1}, fetched_at="2026-10-05T00:00:00Z")
    daily_ingest.apply_to_snapshot(ds, snapshot_rows(body))
    daily_ingest.apply_to_snapshot(ds, snapshot_rows(body))
    assert len(ds.table("games")) == before


def test_a_column_that_does_not_exist_is_rejected() -> None:
    """スナップショットに無い列を書こうとしたら落とす。

    **黙って列を増やさない** — `write_snapshot` が書いた parquet の列が変わると、
    次に読む側（特徴量・学習）が気づかないまま別の形を受け取る。
    """
    ds = snapshot_dataset()
    with pytest.raises(SnapshotError):
        daily_ingest.apply_to_snapshot(ds, {"games": [{"id": "x", "unknown": 1}]})


def test_an_unknown_table_is_rejected() -> None:
    """写し方（主キー）が未定のテーブルは落とす。"""
    ds = snapshot_dataset()
    with pytest.raises(SnapshotError):
        daily_ingest.apply_to_snapshot(ds, {"venues": [{"id": "x"}]})


def test_game_columns_match_the_ddl(db: sqlite3.Connection) -> None:
    """`GAME_COLUMNS` が DDL と一致すること。**二重管理を検査で止める。**

    `batch_limits` を DDL と突き合わせているのと同じ理由である（詳細設計 3.4）。
    列を1つ足したときにこの一覧だけが古いまま残ると、**本物では通る操作が
    テストでだけ落ちる**（あるいはその逆）。
    """
    actual = [row[1] for row in db.execute("PRAGMA table_info(games)")]
    assert list(GAME_COLUMNS) == actual


# --- 月で絞らず、窓の終わりで止める（2026-10-05 の実測） ---

def test_the_walk_stops_after_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """**ページの最終日が窓の終わりを超えたら、以降は取らない。**

    設計は `mon` で月に絞ることを想定していたが、**`mon=10` は読める行を1つも
    返さなかった**（行0件 / 日付不明2件。`mon=all` は20件/ページ）。
    想定の「効かなくてもページ数が増えるだけ」のどちらでもなかった。

    代わりにページを辿って早めに止める。**次を取る前に判定する** — 取得してから
    捨てるのは無駄な1リクエストである（絶対ルール6）。
    """
    from batch.jobs.schedule_walk import walk_schedule
    from batch.parser.schedule_parser import SchedulePage

    pages = [
        SchedulePage(games=(game("1", "2026-10-05"),), next_index=20,
                     last_date="2026-10-05"),
        SchedulePage(games=(game("2", "2026-10-12"),), next_index=40,
                     last_date="2026-10-12"),
        SchedulePage(games=(game("3", "2026-10-20"),), next_index=60,
                     last_date="2026-10-20"),
    ]
    asked: list[str] = []

    class FakeClient:
        def get(self, url: str) -> str:
            asked.append(url)
            return url

    def parse(_body: str, **kwargs: object) -> SchedulePage:
        return pages[int(str(kwargs["index"])) // 20]

    monkeypatch.setattr("batch.jobs.schedule_walk.parse_schedule", parse)
    got = list(walk_schedule(
        FakeClient(),  # type: ignore[arg-type]
        year=2026, event=2, clubs_by_name={}, on_page=lambda _p: None,
        stop=lambda p: p.last_date is not None and p.last_date > "2026-10-12",
    ))

    # 2ページ目で窓を越えたので3ページ目は取らない
    assert len(asked) == 3
    assert [g.game_id for g in got] == ["1", "2", "3"]


def test_the_walk_takes_everything_without_a_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`stop` を渡さなければ終端まで辿る（`backfill` はこちら）。"""
    from batch.jobs.schedule_walk import walk_schedule
    from batch.parser.schedule_parser import SchedulePage

    pages = [
        SchedulePage(games=(game("1", "2026-10-05"),), next_index=20,
                     last_date="2026-10-05"),
        SchedulePage(games=(game("2", "2026-12-20"),), next_index=None,
                     last_date="2026-12-20"),
    ]

    class FakeClient:
        def get(self, url: str) -> str:
            return url

    monkeypatch.setattr(
        "batch.jobs.schedule_walk.parse_schedule",
        lambda _b, **kw: pages[int(str(kw["index"])) // 20])
    got = list(walk_schedule(
        FakeClient(),  # type: ignore[arg-type]
        year=2026, event=2, clubs_by_name={}, on_page=lambda _p: None))
    assert [g.game_id for g in got] == ["1", "2"]


# ---------------------------------------------------------------------------
# 試合前の会場の解決（詳細設計 4.2 のステップ1b）
#
# **前のブランチではここに検査が無く、パーサだけを見ていた。** 分岐（既知 /
# 未知で名称あり / 未知で名称なし / ページに無い）はこの層にある。
# ---------------------------------------------------------------------------


class VenuePages:
    """会場の印だけを返す偽クライアント。**取得した URL を記録する。**"""

    def __init__(self, by_game: Mapping[str, str]) -> None:
        self.by_game = by_game
        self.asked: list[str] = []

    def get(self, url: str) -> str:
        self.asked.append(url)
        for game_id, body in self.by_game.items():
            if f"ScheduleKey={game_id}" in url:
                return body
        return "<html></html>"


def _mark(arena_cd: str, name: str | None) -> str:
    inner = "" if name is None else f'<span class="link-line">{name}</span>'
    return (
        f'<span class="stadium-name">会場：<a href="?ArenaCD={arena_cd}">{inner}</a></span>'
    )


def test_a_known_venue_is_used_as_is() -> None:
    from batch.jobs.daily_ingest import resolve_venues
    result = Result()
    got = resolve_venues(
        VenuePages({"1": _mark("186", "ゼビオアリーナ仙台")}),  # type: ignore[arg-type]
        [game("1", "2026-10-09")],
        known_venue_ids={"186"}, already={}, result=result, log=lambda _m: None)
    assert got.ids == {"1": "186"}
    # **既にある会場を登録し直さない**（`venues.name` は初出で固定する。1.2）
    assert got.venues == {}
    assert (result.venues_resolved, result.venues_registered) == (1, 0)


def test_an_unknown_venue_is_registered_with_its_name() -> None:
    """**改称で新しい ArenaCD が振られた会場**を試合前に登録する。"""
    from batch.jobs.daily_ingest import resolve_venues
    result = Result()
    got = resolve_venues(
        VenuePages({"1": _mark("100122", "とちぎん・ブレックスアリーナ宇都宮")}),  # type: ignore[arg-type]
        [game("1", "2026-10-11")],
        known_venue_ids={"7"}, already={}, result=result, log=lambda _m: None)
    assert got.venues == {"100122": "とちぎん・ブレックスアリーナ宇都宮"}
    assert got.ids == {"1": "100122"}
    assert (result.venues_registered, result.venues_unknown) == (1, [])


def test_an_unknown_venue_without_a_name_is_not_used() -> None:
    """**推測で `venues` に行を作らない**（`name` は NOT NULL。規約5）。

    IDも使わない — `games.venue_id` は FK であり、入れると書き込みが落ちる。
    """
    from batch.jobs.daily_ingest import resolve_venues
    result = Result()
    got = resolve_venues(
        VenuePages({"1": _mark("100122", None)}),  # type: ignore[arg-type]
        [game("1", "2026-10-11")],
        known_venue_ids={"7"}, already={}, result=result, log=lambda _m: None)
    assert (got.ids, got.venues) == ({}, {})
    assert result.venues_unknown == ["100122"]
    assert result.venues_registered == 0


def test_the_same_new_venue_is_registered_once() -> None:
    """2試合が同じ新会場でも登録は1件。**件数は会場の数である。**"""
    from batch.jobs.daily_ingest import resolve_venues
    body = _mark("100122", "とちぎん・ブレックスアリーナ宇都宮")
    result = Result()
    got = resolve_venues(
        VenuePages({"1": body, "2": body}),  # type: ignore[arg-type]
        [game("1", "2026-10-11"), game("2", "2026-10-12")],
        known_venue_ids=set(), already={}, result=result, log=lambda _m: None)
    assert got.venues == {"100122": "とちぎん・ブレックスアリーナ宇都宮"}
    assert got.ids == {"1": "100122", "2": "100122"}
    assert (result.venues_registered, result.venues_resolved) == (1, 2)


def test_a_page_without_an_arena_is_counted_not_guessed() -> None:
    from batch.jobs.daily_ingest import resolve_venues
    result = Result()
    got = resolve_venues(
        VenuePages({}),  # type: ignore[arg-type]
        [game("1", "2026-10-09")],
        known_venue_ids={"186"}, already={}, result=result, log=lambda _m: None)
    assert got.ids == {}
    assert result.venues_absent == 1


def test_games_that_already_have_a_venue_are_not_fetched() -> None:
    """要件 5.2「取得は必要最小限のページに限る」。会場IDは変わらない。"""
    from batch.jobs.daily_ingest import resolve_venues
    client = VenuePages({"1": _mark("186", "ゼビオアリーナ仙台")})
    got = resolve_venues(
        client,  # type: ignore[arg-type]
        [game("1", "2026-10-09")],
        known_venue_ids={"186"}, already={"1": "186"}, result=Result(),
        log=lambda _m: None)
    assert (got.ids, client.asked) == ({}, [])


def test_the_registered_venue_reaches_the_payload_and_the_snapshot() -> None:
    """**送った本文からスナップショットへ写る**（基本設計 2.2）。"""
    body = upcoming_games_payload(
        [game("1", "2026-10-11")],
        season=SeasonRef("2026-27-PREMIER", "2026-27", "PREMIER"),
        club_ids={"703": "703", "704": "704"},
        series_game_no={}, fetched_at="2026-10-09T21:00:00Z",
        venue_ids={"1": "100122"},
        venues={"100122": "とちぎん・ブレックスアリーナ宇都宮"},
    )
    assert body["venues"] == [
        {"id": "100122", "name": "とちぎん・ブレックスアリーナ宇都宮"}]
    assert body["venueSourceKeys"] == [
        {"sourceCode": "100122", "venueId": "100122"}]
    rows = snapshot_rows(body)
    assert rows["venues"] == [
        {"id": "100122", "name": "とちぎん・ブレックスアリーナ宇都宮"}]


def test_the_payload_omits_venues_when_there_are_none() -> None:
    """**登録する会場が無いときはキーを置かない**（空配列を送らない）。"""
    body = upcoming_games_payload(
        [game("1", "2026-10-09")],
        season=SeasonRef("2026-27-PREMIER", "2026-27", "PREMIER"),
        club_ids={"703": "703", "704": "704"},
        series_game_no={}, fetched_at="2026-10-09T21:00:00Z",
        venue_ids={"1": "186"},
    )
    assert "venues" not in body and "venueSourceKeys" not in body
