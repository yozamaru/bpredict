"""未実施の試合の取り込み（詳細設計 4.2 のステップ1b / 基本設計 4.2 の3b）。

| テスト | どの規約か |
|---|---|
| `test_scores_are_null` | NULL = 未実施（詳細設計 1.3）。0 を入れると引き分けの意味になる |
| `test_no_venue_is_sent` | **日程ページに公式の会場IDが無い**。NULL のままにする |
| `test_no_club_seasons_are_sent` | **クラブ名は略称のことがある**。正式名称を上書きしない |
| `test_two_team_rows_per_game` | チーム視点の行は1試合2本 |
| `test_payload_matches_the_zod_schema` | 本文のキーが api 側の Zod と対応する |
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from batch.loader.payload import SeasonRef, upcoming_games_payload
from batch.parser.schedule_parser import ScheduleGame

SEASON = SeasonRef("2026-27-PREMIER", "2026-27", "PREMIER")
CLUBS = {"703": "703", "704": "704"}


def game(**over: object) -> ScheduleGame:
    base: dict[str, object] = {
        "game_id": "600001",
        "competition": "REGULAR",
        "game_date": "2026-10-10",
        "tipoff_at": "2026-10-10T10:05:00Z",
        "home_source_id": "703",
        "away_source_id": "704",
        # **略称が来ることがある**（2020-21 の `千葉J`）。だから送らない
        "home_name": "架空ブ",
        "away_name": "架空タ",
        "home_score": None,
        "away_score": None,
        "status": "SCHEDULED",
        "source_url": "https://example.invalid/schedule/",
    }
    base.update(over)
    return ScheduleGame(**base)  # type: ignore[arg-type]


def payload(*games: ScheduleGame, series: dict[str, int] | None = None) -> dict[str, object]:
    return upcoming_games_payload(
        list(games) or [game()],
        season=SEASON,
        club_ids=CLUBS,
        series_game_no=series or {},
        fetched_at="2026-10-04T00:00:00Z",
    )


def rows(body: dict[str, object], key: str) -> list[dict[str, object]]:
    value = body[key]
    assert isinstance(value, list)
    return [row for row in value if isinstance(row, dict)]


# --- 未実施であることの表し方 ---

def test_scores_are_null() -> None:
    """**NULL = 未実施**（詳細設計 1.3）。0 を入れると引き分けの意味になる。"""
    row = rows(payload(), "games")[0]
    assert row["homeScore"] is None
    assert row["awayScore"] is None
    assert row["attendance"] is None


def test_finished_at_is_null_and_not_estimated() -> None:
    """未実施は `finished_at` が NULL、推定フラグは 0（詳細設計 1.3 の表）。"""
    row = rows(payload(), "games")[0]
    assert row["finishedAt"] is None
    assert row["finishedAtIsEstimated"] == 0


def test_team_rows_have_no_result_or_margin() -> None:
    """`result` / `margin` も NULL。**0 を入れない。**"""
    for row in rows(payload(), "teamGames"):
        assert row["result"] is None
        assert row["margin"] is None
        assert row["finishedAt"] is None


def test_two_team_rows_per_game() -> None:
    """チーム視点の行は1試合2本（ホームとアウェイ）。"""
    body = payload()
    assert len(rows(body, "teamGames")) == 2 * len(rows(body, "games"))
    assert {r["isHome"] for r in rows(body, "teamGames")} == {0, 1}


def test_opponents_are_crossed() -> None:
    for row in rows(payload(), "teamGames"):
        assert row["clubId"] != row["opponentId"]


# --- 送らないもの ---

def test_no_venue_is_sent() -> None:
    """**日程ページに公式の会場IDが無い**（詳細設計 2.2）。

    会場名で名寄せしない（1.1）ため NULL のままにし、試合後にボックススコアの
    取り込みが埋める。**`venues` の行を作らない** — 作ると ID の無い会場が増える。
    """
    body = payload()
    assert rows(body, "games")[0]["venueId"] is None
    assert rows(body, "games")[0]["venueNameAtGame"] is None
    assert "venues" not in body
    assert "venueSourceKeys" not in body


def test_no_club_seasons_are_sent() -> None:
    """**クラブ名は略称のことがある**（詳細設計 4.4）。

    `club_seasons.name` の出典はボックススコアの `TeamNameJ`（その試合時点の
    正式名称）である。**ここで略称を入れると正式名称を上書きする。**
    """
    assert "clubSeasons" not in payload()


def test_no_players_are_sent() -> None:
    """試合前には出場者が分からない（エントリーは別の口）。"""
    assert "players" not in payload()


# --- 状態 ---

@pytest.mark.parametrize("status", ["SCHEDULED", "POSTPONED", "CANCELLED"])
def test_non_finished_statuses_are_carried(status: str) -> None:
    """**`POSTPONED` と `CANCELLED` も送る。**

    `SCHEDULED` だけを入れると、**中止になった試合が `SCHEDULED` のまま残り、
    予測が作られ続ける**（詳細設計 1.3 の「ゴースト試合」）。
    """
    assert rows(payload(game(status=status)), "games")[0]["status"] == status


def test_series_game_no_is_passed_through() -> None:
    body = payload(game(), series={"600001": 2})
    assert rows(body, "games")[0]["seriesGameNo"] == 2


def test_series_game_no_is_none_when_unknown() -> None:
    """**推測で埋めない。** 連戦番号が決まらなければ NULL。"""
    assert rows(payload(), "games")[0]["seriesGameNo"] is None


# --- api 側との対応 ---

def test_payload_matches_the_zod_schema() -> None:
    """本文のキーが `api/src/schemas/facts.ts` に存在すること。

    片方だけ直すと本番で 400 を受けて初めて分かる（詳細設計 3.7 と同じ問題）。
    """
    schema = (Path(__file__).resolve().parents[2]
              / "api" / "src" / "schemas" / "facts.ts").read_text(encoding="utf-8")
    body = payload()
    assert set(body) <= set(re.findall(r"^\s{4}(\w+):", schema, re.MULTILINE)) | {
        "games", "teamGames"}
    for key in rows(body, "games")[0]:
        assert f"{key}:" in schema, f"games に Zod に無いキーがある: {key}"
    for key in rows(body, "teamGames")[0]:
        assert f"{key}:" in schema, f"teamGames に Zod に無いキーがある: {key}"


def test_body_is_json_serialisable() -> None:
    json.dumps(payload(), ensure_ascii=False)
