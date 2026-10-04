"""終了した1試合を取り込む（`backfill` と `daily_ingest` が共有する）。

**`jobs/` に置く。** 取得（`scraper/`）と解釈（`parser/`）と書き込み（`loader/`）の
組み合わせであり、どの層の責務でもない（`schedule_walk.py` と同じ事情）。

**2つのジョブが同じ取り込みをするため、1か所に置く** — 片方だけ直すと、
**過去データと日次で入る列が違うことになる**（詳細設計 4.2 のステップ1）。

**送った本文を返す。** `daily_ingest` のステップ2 がそれをスナップショットへ
写すためである（`backfill` は使わない）。**別に組み直さない** — D1 に送ったのと
同じ値であることが要点である（基本設計 2.2）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from batch.loader.api import Poster
from batch.loader.payload import SeasonRef, games_payload, stats_payload
from batch.parser.boxscore_parser import parse_boxscore
from batch.parser.schedule_parser import ScheduleGame
from batch.scraper.boxscore import boxscore_url
from batch.scraper.client import RateLimitedClient


@dataclass(frozen=True)
class Sent:
    """送った本文。**ステップ2 がこれをスナップショットへ写す。**"""

    games: Mapping[str, object]
    stats: Mapping[str, object]


def ingest_game(
    client: RateLimitedClient,
    api: Poster,
    game: ScheduleGame,
    *,
    event: int,
    season: SeasonRef,
    club_ids: Mapping[str, str],
    short_names: Mapping[str, str],
    series_game_no: int | None,
    now: datetime | None = None,
) -> Sent:
    """ボックススコアを取得して `games` と `stats` を送る。

    **順序を変えない。** `POST /internal/games` が先で、`POST /internal/stats` が
    後である（スタッツは `games` と `players` を参照する）。
    """
    url = boxscore_url(game.game_id)
    box = parse_boxscore(
        client.get(url), event=event, clubs=club_ids, expected_game_id=game.game_id)
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    fetched_at = moment.isoformat(timespec="seconds").replace("+00:00", "Z")

    games = games_payload(
        box,
        season=season,
        club_ids=club_ids,
        short_names=short_names,
        series_game_no=series_game_no,
        source_url=url,
        fetched_at=fetched_at,
    )
    api.post("games", games)
    stats = stats_payload(box, club_ids=club_ids, fetched_at=fetched_at)
    api.post("stats", stats)
    return Sent(games=games, stats=stats)
