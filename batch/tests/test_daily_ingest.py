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

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pandas as pd
import pytest

from batch.features.dataset import Dataset, SnapshotError
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
from batch.loader.payload import (
    SeasonRef,
    snapshot_rows,
    upcoming_games_payload,
)
from batch.model.predict import Prediction
from batch.parser.schedule_parser import ScheduleGame
from batch.static_json.from_snapshot import PredictedGame


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
    monkeypatch.setattr(daily_ingest, "build_features", lambda *a, **k: {"x": 1.0})
    monkeypatch.setattr(daily_ingest, "load_active", lambda *a, **k: FakeModels())
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
    api = FakeApi(posted=[])
    result = daily_ingest.run_inference(
        api, ds=snapshot_dataset(), run_id="daily-test",  # type: ignore[arg-type]
        now=datetime(2026, 10, 5, 12, 0, tzinfo=UTC), log=lambda _m: None)
    assert result.skipped_features == ["g1"]
    assert api.posted == []


def test_the_run_id_is_shared_with_the_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
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
    # today.json ＋ schedule 7日 ＋ 当日の詳細1件 ＋ meta.json
    assert written == 1 + 7 + 1 + 1
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
    assert written == 1 + 7 + 1          # 詳細は0件
    today = json.loads((tmp_path / "today.json").read_text(encoding="utf-8"))
    assert today["data"]["games"] == []
    assert today["data"]["gameDate"] == "2026-10-05"


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
    assert not list((tmp_path / "games").glob("*.json"))
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
