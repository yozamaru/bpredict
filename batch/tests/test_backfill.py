"""backfill の検証（詳細設計 4.8 / 基本設計 4.3）。

**実サイトにもD1にも触れない。** 応答を注入し、投入先は記録用の偽APIに向ける。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from batch.jobs import backfill
from batch.loader.api import InternalApi, Response
from batch.loader.payload import series_numbers, spectator_restricted
from batch.scraper.client import ScrapingStopped
from batch.tests.fixtures.boxscore import boxscore_data, page
from batch.tests.test_schedule_parser import body, game_html

SEASON = "2016-17-B1"
# 日付見出しは要求シーズンの年に収まる必要がある（範囲外は ValidationError）
HEADER = ('<div class="champion-box box-container">'
          '<span class="title">2016.10.01(土)</span></div>')
HEADER_NEXT = ('<div class="champion-box box-container">'
               '<span class="title">2016.10.02(日)</span></div>')
# seasons.csv / club_source_ids.csv の実在の値に合わせる（マスタは工程2で確定済み）
HOME_SOURCE = "704"
AWAY_SOURCE = "703"


def club_selector() -> str:
    return (
        '<select name="club">'
        f'<option value="{HOME_SOURCE}">架空ホーム</option>'
        f'<option value="{AWAY_SOURCE}">架空アウェイ</option>'
        "</select>"
    )


def boxscore_page(game_id: str, *, home: int = 80, away: int = 79) -> str:
    data = boxscore_data()
    data["ScheduleKey"] = game_id
    data["Game"]["ScheduleKey"] = game_id
    data["Game"]["Year"] = 2016
    # 2016-10-01 15:05 JST（日程見出しの日付と一致させる。年度との整合も検証される）
    data["Game"]["GameDateTime"] = "1475301900"
    data["Game"]["HomeTeamID"] = HOME_SOURCE
    data["Game"]["AwayTeamID"] = AWAY_SOURCE
    data["Game"]["HomeTeamScore"] = home
    data["Game"]["AwayTeamScore"] = away
    for side, total in (("Home", home), ("Away", away)):
        for row in data[f"{side}Boxscores"]:
            row["ScheduleKey"] = game_id
            if row.get("TeamID") not in (None, ""):
                row["TeamID"] = HOME_SOURCE if side == "Home" else AWAY_SOURCE
            if row.get("Category") == 3:
                row["Point"] = total
        players = [r for r in data[f"{side}Boxscores"]
                   if r.get("Category") == 1 and r.get("PeriodCategory") == 18]
        # 得点の恒等式と「選手合計 = 公式合計」を保つ
        per = total // len(players)
        for index, row in enumerate(players):
            points = per + (total - per * len(players) if index == 0 else 0)
            row["Point"], row["PT2M"], row["PT3M"], row["FTM"] = points, points // 2, 0, points % 2
            row["PT2A"] = max(row["PT2A"], row["PT2M"])
            row["FTA"] = max(row["FTA"], row["FTM"])
        for row in data[f"{side}Boxscores"]:
            if row.get("Category") == 3 and row.get("PeriodCategory") == 18:
                for key in ("PT2M", "PT3M", "FTM", "PT2A", "PT3A", "FTA"):
                    row[key] = sum(r[key] for r in players)
                row["Point"] = total
    return str(page(data))


class FakeScraper:
    """`RateLimitedClient` の差し替え。URL ごとに応答を返す。"""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.requested: list[str] = []
        self.verified = 0

    def verify_policy(self) -> None:
        self.verified += 1

    def get(self, url: str) -> str:
        self.requested.append(url)
        for key, value in self.responses.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return str(value)
        raise AssertionError(f"想定外の取得: {url}")


class FakeApi:
    """投入内容を記録する。D1 には触れない。"""

    def __init__(self, ingested: set[str] | None = None) -> None:
        self.posted: list[tuple[str, dict[str, Any]]] = []
        self._ingested = ingested or set()

    def ingested_game_ids(self, season_id: str) -> set[str]:
        return self._ingested

    def post(self, path: str, payload: dict[str, Any]) -> None:
        self.posted.append((path, payload))


def schedule_responses(*game_ids: str, terminal_index: int | None = 20) -> dict[str, Any]:
    # 非空のページは次の index を返す（進まない index は ParseError）
    rows = "".join(game_html(gid, state="FINAL", home="80", away="79") for gid in game_ids)
    return {
        "data_format=json&year=2016&mon=all&tab=1&event=2&index=0":
            body(HEADER + rows, index=terminal_index),
        "event=2&index=20": body(index=None),
        "event=3&index=0": body(index=None),
        "/schedule/?year=2016": club_selector(),
        **{f"ScheduleKey={gid}": boxscore_page(gid) for gid in game_ids},
    }


def run(
    responses: dict[str, Any],
    *,
    ingested: set[str] | None = None,
    limit: int | None = None,
) -> tuple[backfill.Result, FakeScraper, FakeApi]:
    scraper, api = FakeScraper(responses), FakeApi(ingested)
    result = backfill.run(SEASON, client=scraper, api=api, limit=limit)  # type: ignore[arg-type]
    return result, scraper, api


def test_ingests_finished_games_and_posts_both_endpoints() -> None:
    result, scraper, api = run(schedule_responses("101", "102"))

    assert result.status == "SUCCESS"
    assert result.ingested == 2
    assert scraper.verified == 1, "取得前確認を必ず通す"
    assert [path for path, _ in api.posted] == ["games", "stats", "games", "stats", "log"]

    games = api.posted[0][1]
    assert games["games"][0]["seasonId"] == SEASON
    assert games["games"][0]["competition"] == "REGULAR"
    # 公式IDを内部 club_id に解決している（旧IDのまま保存しない）
    assert games["games"][0]["homeClubId"] == "704"
    assert {"venues", "venueSourceKeys", "players", "clubSeasons"} <= set(games)
    assert games["clubSeasons"][0]["shortName"] == "架空ホーム"


def test_already_ingested_games_are_skipped() -> None:
    """再開可能であること。既取得の試合は詳細を取りに行かない。"""
    result, scraper, _api = run(schedule_responses("101", "102"), ingested={"101"})

    assert result.ingested == 1
    assert result.skipped_existing == 1
    assert not any("ScheduleKey=101" in url for url in scraper.requested)


def test_unfinished_games_are_not_fetched() -> None:
    responses = {
        "event=2&index=0": body(HEADER + game_html("101", state="見どころ"), index=20),
        "event=2&index=20": body(index=None),
        "event=3&index=0": body(index=None),
        "/schedule/?year=2016": club_selector(),
    }
    result, scraper, _ = run(responses)
    assert result.ingested == 0
    assert result.skipped_unfinished == 1
    assert not any("game_detail" in url for url in scraper.requested)


def test_scraping_stop_degrades_to_partial() -> None:
    """429 / 503 は取得区間のみ中止し、ログは残す（基本設計 4.3）。"""
    responses = schedule_responses("101", "102")
    responses["ScheduleKey=102"] = ScrapingStopped("停止")
    result, _, api = run(responses)

    assert result.status == "PARTIAL"
    assert result.ingested == 1
    assert api.posted[-1][0] == "log"
    assert api.posted[-1][1]["status"] == "PARTIAL"


def test_three_consecutive_parse_errors_abort() -> None:
    """パース失敗が連続3件でジョブを中止する（構造変更の疑い）。"""
    responses = schedule_responses("101", "102", "103", "104")
    for game_id in ("101", "102", "103"):
        responses[f"ScheduleKey={game_id}"] = "<html><script>壊れている</script></html>"
    result, scraper, _ = run(responses)

    assert result.status == "PARTIAL"
    assert result.ingested == 0
    assert not any("ScheduleKey=104" in url for url in scraper.requested), "4件目は取りに行かない"


def test_a_single_invalid_game_does_not_stop_the_job() -> None:
    """値域検証エラーは当該試合をスキップして継続する。"""
    responses = schedule_responses("101", "102")
    broken = boxscore_data()
    broken["Game"]["HomeTeamScore"] = 999  # 値域外
    responses["ScheduleKey=101"] = page(broken)
    result, _, _ = run(responses)

    assert result.status == "SUCCESS"
    assert result.skipped_invalid == 1
    assert result.ingested == 1

    # **どの試合がなぜ落ちたかを残す**（詳細設計 4.4）。件数だけでは調査できず、
    # 2016-17 で9試合が落ちたとき実サイトへ約80件の再取得が必要になった
    assert len(result.skipped) == 1
    game_id, kind, message = result.skipped[0]
    assert game_id == "101"
    assert kind == "ValidationError"
    assert message
    # 例外オブジェクトを残さない（URL や本文が混ざらない。絶対ルール4）
    assert "http" not in message.lower()


def test_limit_stops_after_the_requested_count() -> None:
    result, scraper, _ = run(schedule_responses("101", "102"), limit=1)
    assert result.ingested == 1
    assert sum(1 for url in scraper.requested if "game_detail" in url) == 1


def test_unknown_season_is_rejected_before_any_request() -> None:
    scraper, api = FakeScraper({}), FakeApi()
    with pytest.raises(Exception, match="seasons.csv"):
        backfill.run("9999-00-B1", client=scraper, api=api)  # type: ignore[arg-type]
    assert scraper.requested == []


# --- payload の導出 ---

def test_series_numbers_counts_back_to_back_games() -> None:
    """土日2連戦が 1, 2 になり、間が空いたら 1 に戻ること。"""
    games = [
        ("a", "2016-10-01", "x", "y"),
        ("b", "2016-10-02", "x", "y"),
        ("c", "2016-10-08", "x", "y"),
        ("d", "2016-10-02", "z", "w"),
    ]
    assert series_numbers(games) == {"a": 1, "b": 2, "c": 1, "d": 1}


@pytest.mark.parametrize(
    ("label", "attendance", "expected"),
    [
        ("2020-21", None, 1),
        ("2021-22", 5000, 1),
        ("2016-17", 5000, None),
        ("2016-17", None, None),
    ],
)
def test_spectator_restricted_forces_the_covid_seasons(
    label: str, attendance: int | None, expected: int | None
) -> None:
    """観客制限期間は期間指定で強制的に 1（詳細設計 1.3）。"""
    assert spectator_restricted(label, attendance) == expected


def test_dry_run_posts_nothing(tmp_path: Path) -> None:
    api = InternalApi(
        "http://127.0.0.1:8787",
        "token",
        transport=lambda *args: Response(200, json.dumps({"data": {"gameIds": []}})),
        dry_run=True,
    )
    scraper = FakeScraper(schedule_responses("101"))
    result = backfill.run(SEASON, client=scraper, api=api)  # type: ignore[arg-type]
    assert result.ingested == 1
    assert [path for path, _ in api.skipped] == ["games", "stats", "log"]
    assert datetime.now(UTC).tzinfo is UTC
