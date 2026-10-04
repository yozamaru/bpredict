"""ステップ1: 前日の結果取得（詳細設計 4.2 のステップ1 / 2）。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from batch.features.dataset import Dataset, SnapshotError
from batch.jobs import daily_ingest
from batch.jobs.daily_ingest import (
    NEVER_UPDATE,
    apply_to_snapshot,
    record_exclusions,
    run_yesterday,
    yesterday_jst,
)
from batch.jobs.game_ingest import Sent
from batch.jobs.seed_master import Season
from batch.loader.payload import snapshot_rows
from batch.parser.errors import ValidationError
from batch.parser.schedule_parser import (
    ExcludedGame,
    ScheduleGame,
    SchedulePage,
)
from batch.scraper.client import ResponseError

SEASON = Season("2026-27-PREMIER", "2026-27", "PREMIER", "2026-09-01", "2027-06-30")
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
YESTERDAY = "2026-10-04"


def game(game_id: str, game_date: str, status: str = "FINISHED") -> ScheduleGame:
    return ScheduleGame(
        game_id=game_id, competition="REGULAR", game_date=game_date,
        tipoff_at=f"{game_date}T10:05:00Z",
        home_source_id="703", away_source_id="704",
        home_name="架空ブ", away_name="架空タ",
        home_score=88 if status == "FINISHED" else None,
        away_score=81 if status == "FINISHED" else None,
        status=status, source_url=f"https://example.test/{game_id}",
    )


class FakeClient:
    """`verify_policy` と `get` だけを持つ。**本物の作法は持たない。**"""

    def verify_policy(self, **_k: object) -> None:
        return None

    def get(self, _url: str) -> str:
        return "<html></html>"


class FakeApi:
    def __init__(self) -> None:
        self.posted: list[tuple[str, Mapping[str, object]]] = []

    def post(self, path: str, payload: Mapping[str, object]) -> object:
        self.posted.append((path, payload))
        return {"data": {}}


def wire(
    monkeypatch: pytest.MonkeyPatch,
    games: list[ScheduleGame],
    *,
    ingest: Any = None,
    excluded: list[ExcludedGame] | None = None,
) -> FakeApi:
    """日程の walk と1試合ごとの取り込みを差し替える。"""
    monkeypatch.setattr(daily_ingest, "load_seasons", lambda: [SEASON])
    monkeypatch.setattr(
        daily_ingest, "load_club_source_ids",
        lambda: [type("R", (), {"source_id": "703", "club_id": "703"})(),
                 type("R", (), {"source_id": "704", "club_id": "704"})()])
    monkeypatch.setattr(daily_ingest, "parse_club_options", lambda _h: {"架空ブ": "703"})

    def collect(_client: object, season: object, result: Any, **_k: object) -> Any:
        for found in excluded or []:
            result.excluded.setdefault(SEASON.id, []).append(found)
        return iter(games)

    monkeypatch.setattr(daily_ingest, "_collect", collect)

    api = FakeApi()
    sent = Sent(games={"games": [{"id": "x"}]}, stats={"teamGameStats": []})
    monkeypatch.setattr(
        daily_ingest, "ingest_game", ingest or (lambda *a, **k: sent))
    return api


# --- 対象の選び方 ---


def test_yesterday_is_the_jst_calendar_day() -> None:
    """**`game_date` は JST の暦日である**（CLAUDE.md 時刻の扱い）。"""
    # 2026-10-05 00:05 JST = 2026-10-04 15:05 UTC → 前日は 10-04
    assert yesterday_jst(datetime(2026, 10, 4, 15, 5, tzinfo=UTC)) == "2026-10-04"
    # 2026-10-04 23:55 JST = 同日 14:55 UTC → 前日は 10-03
    assert yesterday_jst(datetime(2026, 10, 4, 14, 55, tzinfo=UTC)) == "2026-10-03"


def test_only_yesterdays_finished_games_are_ingested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**開幕から辿るため、他の日の試合が必ず出る。** 数えるだけで取り込まない。"""
    taken: list[str] = []

    def ingest(_c: object, _a: object, g: ScheduleGame, **_k: object) -> Sent:
        taken.append(g.game_id)
        return Sent(games={}, stats={})

    api = wire(monkeypatch, [
        game("old", "2026-10-01"),
        game("yes", YESTERDAY),
        game("today", "2026-10-05"),
    ], ingest=ingest)
    result = run_yesterday(FakeClient(), api, now=NOW)  # type: ignore[arg-type]
    assert taken == ["yes"]
    assert result.ingested == 1
    assert result.other_days == 2


def test_an_unfinished_game_on_yesterday_is_counted_not_ingested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """延期・中止・開始前はステップ1b が状態として入れる。**ここでは取らない。**"""
    api = wire(monkeypatch, [game("post", YESTERDAY, status="POSTPONED")])
    result = run_yesterday(FakeClient(), api, now=NOW)  # type: ignore[arg-type]
    assert (result.ingested, result.unfinished) == (0, 1)


def test_no_games_yesterday_is_a_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """**オフシーズンでも落ちない。** 取得を1回も行わずに SUCCESS で終わる。"""
    api = wire(monkeypatch, [])
    result = run_yesterday(FakeClient(), api, now=NOW)  # type: ignore[arg-type]
    assert (result.status, result.ingested) == ("SUCCESS", 0)


# --- 失敗したときの挙動（詳細設計 4.3） ---


def test_a_failed_fetch_is_counted_apart_from_invalid_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**取得失敗とデータの欠陥を混ぜない**（詳細設計 4.4）。

    前者は相手側の事情で再実行すれば直り、後者は再実行しても直らない。
    """
    def ingest(_c: object, _a: object, g: ScheduleGame, **_k: object) -> Sent:
        if g.game_id == "a":
            raise ResponseError("未対応のHTTPステータス: 502")
        raise ValidationError("値域")

    api = wire(monkeypatch, [game("a", YESTERDAY), game("b", YESTERDAY)],
               ingest=ingest)
    result = run_yesterday(FakeClient(), api, now=NOW)  # type: ignore[arg-type]
    assert (result.unfetched, result.invalid) == (1, 1)
    # **件数だけでなく試合IDと理由を出す**（詳細設計 4.4）
    assert [row[0] for row in result.skipped] == ["a", "b"]


def test_three_consecutive_failures_stop_the_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**非200 が続くのは遮断の疑いであり、叩き続けない**（絶対ルール6）。"""
    def ingest(*_a: object, **_k: object) -> Sent:
        raise ResponseError("未対応のHTTPステータス: 502")

    api = wire(
        monkeypatch, [game(str(i), YESTERDAY) for i in range(5)], ingest=ingest)
    result = run_yesterday(FakeClient(), api, now=NOW)  # type: ignore[arg-type]
    assert result.status == "PARTIAL"
    assert result.unfetched == 3          # 4件目には進まない


def test_a_validation_error_does_not_count_toward_the_streak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**値域違反で打ち切らない**（`backfill` と同じ扱い）。データの欠陥は続く。"""
    def ingest(*_a: object, **_k: object) -> Sent:
        raise ValidationError("値域")

    api = wire(
        monkeypatch, [game(str(i), YESTERDAY) for i in range(5)], ingest=ingest)
    result = run_yesterday(FakeClient(), api, now=NOW)  # type: ignore[arg-type]
    assert (result.status, result.invalid) == ("SUCCESS", 5)


# --- 取り込まない試合の一覧（要件 5.3） ---


def test_the_exclusion_list_is_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """**黙って消さない。** `backfill` は artifact で持ち帰るしかないが、
    このジョブは `contents: write` であり自分でコミットできる（詳細設計 4.4）。
    """
    found = ExcludedGame(
        game_id="500609", game_date="2023-04-12", home_name="架空ブ",
        away_name="架空タ", home_score="0", away_score="20",
        reason="状態欄が空で得点がある（不戦敗の形）")
    api = wire(monkeypatch, [game("yes", YESTERDAY)], excluded=[found])
    result = run_yesterday(FakeClient(), api, now=NOW)  # type: ignore[arg-type]
    # **試合IDで重ねる。** 同じ試合が `event=3` と `event=2` の両方に現れるため、
    # 素朴に足すと件数が二重になる（日程は区分ごとに2回辿る）
    assert result.excluded == {SEASON.id: [found]}

    path = tmp_path / "excluded.json"
    assert record_exclusions(result.excluded, path=path, log=lambda _m: None) == 1
    assert "500609" in path.read_text(encoding="utf-8")


# --- ステップ2: スナップショットの写し方 ---


def dataset(**tables: pd.DataFrame) -> Dataset:
    return Dataset(tables=dict(tables))


def test_a_column_absent_from_the_body_is_left_alone() -> None:
    """**本文に無い列を触らない。** D1 側の `preserve` と同じ効果である（3.4）。

    取り込みが送るのは `venues` の `{id, name}` だけで、行ごと置き換えると
    **座標が消える**（2026-10-05 に本番で踏んだのと同じ形）。
    """
    ds = dataset(venues=pd.DataFrame([
        {"id": "3", "name": "旧名称", "prefecture": "東京都",
         "lat": 35.6, "lng": 139.7},
    ]))
    apply_to_snapshot(ds, {"venues": [{"id": "3", "name": "当時の名称"}]})
    row = ds.table("venues").iloc[0]
    assert (row["prefecture"], row["lat"], row["lng"]) == ("東京都", 35.6, 139.7)


def test_the_venue_name_is_not_overwritten() -> None:
    """**本文にあるのに更新しない唯一の列**（詳細設計 1.2 / 4.2）。

    初出の名称で固定する — 当時の名称で現在の表示名を上書きしない。
    """
    ds = dataset(venues=pd.DataFrame([
        {"id": "3", "name": "初出の名称", "prefecture": None,
         "lat": None, "lng": None},
    ]))
    apply_to_snapshot(ds, {"venues": [{"id": "3", "name": "当時の名称"}]})
    assert ds.table("venues").iloc[0]["name"] == "初出の名称"


def test_the_never_update_list_has_exactly_one_entry() -> None:
    """**例外を名前で固定する。** 増えたらここで落ちる。"""
    assert dict(NEVER_UPDATE) == {"venues": frozenset({"name"})}


def test_a_new_row_keeps_every_column_including_the_name() -> None:
    """**挿入では `venues.name` も入れる**（D1 の INSERT と同じ）。"""
    ds = dataset(venues=pd.DataFrame(
        columns=["id", "name", "prefecture", "lat", "lng"]))
    apply_to_snapshot(ds, {"venues": [{"id": "9", "name": "新しい会場"}]})
    assert ds.table("venues").iloc[0]["name"] == "新しい会場"


def test_an_existing_game_is_updated_in_place_by_column() -> None:
    """ステップ1b が `SCHEDULED` で入れた行に、結果が乗ること。"""
    ds = dataset(games=pd.DataFrame([
        {"id": "g1", "status": "SCHEDULED", "home_score": None,
         "away_score": None, "venue_id": None, "created_at": "作成時刻"},
    ]))
    apply_to_snapshot(ds, {"games": [
        {"id": "g1", "status": "FINISHED", "home_score": 88,
         "away_score": 81, "venue_id": "3"},
    ]})
    row = ds.table("games").iloc[0]
    assert (row["status"], row["home_score"], row["venue_id"]) == ("FINISHED", 88, "3")
    # **本文が送らない列は残る**（D1 が入れる時刻をこちらで書かない）
    assert row["created_at"] == "作成時刻"
    assert len(ds.table("games")) == 1


def test_a_table_without_a_defined_key_is_refused() -> None:
    """**写し方が未定のテーブルを黙って書かない**（詳細設計 4.2 のステップ2）。"""
    ds = dataset(foo=pd.DataFrame(columns=["id"]))
    with pytest.raises(SnapshotError):
        apply_to_snapshot(ds, {"foo": [{"id": "1"}]})


def test_a_column_missing_from_the_snapshot_is_refused() -> None:
    """**黙って列を増やさない。** parquet の列が変わると読む側が別の形を受け取る。"""
    ds = dataset(venues=pd.DataFrame(columns=["id", "name"]))
    with pytest.raises(SnapshotError):
        apply_to_snapshot(ds, {"venues": [{"id": "3", "name": "x", "新しい列": 1}]})


def test_the_eight_tables_of_step_one_are_all_mapped() -> None:
    """**ステップ1 が書く8テーブルすべてに写し方がある**（詳細設計 4.2）。

    広げないと `club_seasons` が D1 にだけ入る（基本設計 2.2 が座標で踏んだ形）。
    """
    tables = set(snapshot_rows(
        {"games": [{"id": "g"}], "teamGames": [{"gameId": "g"}],
         "players": [{"id": "p"}], "venues": [{"id": "v"}],
         "venueSourceKeys": [{"sourceCode": "v"}],
         "clubSeasons": [{"clubId": "c"}]},
        {"teamGameStats": [{"gameId": "g"}], "playerGameStats": [{"gameId": "g"}]},
    ))
    assert tables == {
        "games", "team_games", "players", "venues", "venue_source_keys",
        "club_seasons", "team_game_stats", "player_game_stats",
    }
    assert tables <= set(daily_ingest.KEYS)


# --- 日程の walk が返したものを捨てない ---


def test_the_walk_folds_exclusions_into_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**`_collect` の `fold` が取り込まない試合を拾うこと。**

    上の `test_the_exclusion_list_is_kept` は `_collect` ごと差し替えるため、
    **この経路を通らない** — 変異試験で `fold` から `_add_excluded` を外しても
    1件も落ちなかった（詳細設計 4.4「黙って取りこぼさない」）。
    """
    found = ExcludedGame(
        game_id="500609", game_date=YESTERDAY, home_name="架空ブ",
        away_name="架空タ", home_score="0", away_score="20", reason="不戦敗の形")

    def walk(
        _client: object, *, on_page: Any, **_k: object,
    ) -> Any:
        # **同じ試合が `event=3` と `event=2` の両方に現れる。** 日程は区分ごとに
        # 2回辿るため、素朴に足すと件数が二重になる
        on_page(SchedulePage(games=(), next_index=None, last_date=YESTERDAY,
                             excluded=(found,)))
        return iter(())

    monkeypatch.setattr(daily_ingest, "walk_schedule", walk)
    result = daily_ingest.Result()
    list(daily_ingest._collect(
        FakeClient(), SEASON, result,  # type: ignore[arg-type]
        through=YESTERDAY, clubs_by_name={}))
    assert result.excluded == {SEASON.id: [found]}


def test_exclusions_are_merged_by_game_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """**2回辿っても件数が二重にならない**（`EVENTS` は2つある）。"""
    found = ExcludedGame(
        game_id="500609", game_date=YESTERDAY, home_name="a", away_name="b",
        home_score="0", away_score="20", reason="不戦敗の形")
    target: dict[str, list[ExcludedGame]] = {}
    daily_ingest._add_excluded(target, SEASON.id, [found])
    daily_ingest._add_excluded(target, SEASON.id, [found])
    assert target == {SEASON.id: [found]}
