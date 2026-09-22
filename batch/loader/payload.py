"""解析結果を `/internal/*` の本文へ変換する。**取得もDB書き込みもしない純粋な変換。**

対応表の正本は詳細設計 4.4、本文の形は同 3.4。ここでは次を決める。

- 公式 `TeamID` を `club_source_ids` で内部 `club_id` に解決する（旧IDのまま保存しない）
- `series_game_no`（同一カード連戦の何戦目か）をシーズンの日程から導出する
- `spectator_restricted` を取り込み時に判定して列に書く（特徴量生成で計算し直さない）
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from batch.parser.models import BoxScore

#: 観客制限期間。入場者数が非公開の試合を取りこぼさないため**期間指定で強制的に 1**
#: とする（詳細設計 1.3）。`attendance / capacity < 0.2` の判定より優先する。
SPECTATOR_RESTRICTED_SEASONS = frozenset({"2020-21", "2021-22"})

#: 収容人数が未投入のあいだ動員率を判定できない。**NULL は「判定不能」**であり、
#: Elo では通常のホームアドバンテージを使う（詳細設計 2.5）。
RESTRICTED_RATIO = 0.2


class PayloadError(ValueError):
    """解決できない参照がある（クラブ・シーズンの対応が取れない）。"""


@dataclass(frozen=True)
class SeasonRef:
    season_id: str
    label: str
    league: str


def resolve_club(source_id: str, club_ids: Mapping[str, str]) -> str:
    """公式 `TeamID` → 内部 `club_id`。**旧IDのまま保存しない**（詳細設計 1.2）。"""
    club_id = club_ids.get(source_id)
    if not club_id:
        raise PayloadError(f"公式IDを club_id に解決できない: {source_id}")
    return club_id


def series_numbers(games: Sequence[tuple[str, str, str, str]]) -> dict[str, int]:
    """同一カード連戦の何戦目かを日程から導出する。

    引数は `(game_id, game_date, home_source_id, away_source_id)` の並び。
    **前戦が前日までなら連戦の続きと数える**（B.LEAGUE は土日2連戦が基本編成）。
    順序に依存するため、呼ぶ側がシーズン全体を日付順で渡す。
    """
    numbers: dict[str, int] = {}
    last: dict[frozenset[str], tuple[date, int]] = {}
    for game_id, game_date, home, away in sorted(games, key=lambda row: (row[1], row[0])):
        day = date.fromisoformat(game_date)
        pair = frozenset({home, away})
        previous = last.get(pair)
        continued = previous is not None and (day - previous[0]).days <= 1
        number = previous[1] + 1 if continued and previous is not None else 1
        numbers[game_id] = number
        last[pair] = (day, number)
    return numbers


def spectator_restricted(season_label: str, attendance: int | None) -> int | None:
    """取り込み時に判定して列に書く（詳細設計 1.3）。

    収容人数が未投入のため、通常期は判定不能（NULL）になる。観客制限期間は
    **期間指定で強制的に 1** とするので、コロナ期の Elo には影響しない。
    """
    if season_label in SPECTATOR_RESTRICTED_SEASONS:
        return 1
    if attendance is None:
        return None
    return None  # capacity が未投入のあいだは判定できない


def games_payload(
    box: BoxScore,
    *,
    season: SeasonRef,
    club_ids: Mapping[str, str],
    short_names: Mapping[str, str],
    series_game_no: int | None,
    source_url: str,
    fetched_at: str,
) -> dict[str, object]:
    """`POST /internal/games` の本文。FK 順は API 側が保証する（詳細設計 3.4）。"""
    game = box.game
    home = resolve_club(game.home_club_id, club_ids)
    away = resolve_club(game.away_club_id, club_ids)

    payload: dict[str, object] = {
        "games": [
            {
                "id": game.id,
                "seasonId": season.season_id,
                "league": season.league,
                "competition": game.competition,
                "gameDate": game.game_date,
                "tipoffAt": game.tipoff_at,
                "finishedAt": game.finished_at,
                "finishedAtIsEstimated": game.finished_at_is_estimated,
                "homeClubId": home,
                "awayClubId": away,
                "venueId": game.venue_id,
                "seriesGameNo": series_game_no,
                "status": game.status,
                "homeScore": game.home_score,
                "awayScore": game.away_score,
                "attendance": game.attendance,
                "spectatorRestricted": spectator_restricted(season.label, game.attendance),
                "sourceUrl": source_url,
                "fetchedAt": fetched_at,
            }
        ],
        "teamGames": [
            {
                "gameId": game.id,
                "clubId": club,
                "opponentId": opponent,
                "seasonId": season.season_id,
                "gameDate": game.game_date,
                "finishedAt": game.finished_at,
                "isHome": is_home,
                "competition": game.competition,
                "result": 1 if score > opponent_score else 0,
                "margin": score - opponent_score,
            }
            for club, opponent, is_home, score, opponent_score in (
                (home, away, 1, game.home_score, game.away_score),
                (away, home, 0, game.away_score, game.home_score),
            )
        ],
        "players": [
            {"id": player.player_id, "name": player.name} for player in box.players
        ],
    }

    if game.venue_id is not None:
        payload["venues"] = [{"id": game.venue_id, "name": game.venue_name or game.venue_id}]
        payload["venueSourceKeys"] = [{"sourceCode": game.venue_id, "venueId": game.venue_id}]

    club_seasons = []
    for source_id, club_id, name in (
        (game.home_club_id, home, game.home_name),
        (game.away_club_id, away, game.away_name),
    ):
        short = short_names.get(source_id)
        if short is None:
            # 短縮名は日程ページの選択肢から取る。取れないまま推測で埋めない
            continue
        club_seasons.append(
            {
                "clubId": club_id,
                "seasonId": season.season_id,
                "name": name,
                "shortName": short,
                "league": season.league,
            }
        )
    if club_seasons:
        payload["clubSeasons"] = club_seasons
    return payload


def stats_payload(box: BoxScore, *, club_ids: Mapping[str, str], fetched_at: str) -> dict[str, object]:
    """`POST /internal/stats` の本文。対応表にある列だけを送る（詳細設計 4.4）。"""
    game = box.game
    team_stats = [
        {
            "gameId": team.game_id,
            "clubId": resolve_club(team.club_id, club_ids)
            if team.club_id in club_ids
            else team.club_id,
            "gameDate": game.game_date,
            "isHome": team.is_home,
            "possessions": team.possessions,
            "fetchedAt": fetched_at,
            **{name: getattr(team.stats, name) for name in _COUNT_FIELDS},
        }
        for team in box.teams
    ]
    player_stats = [
        {
            "gameId": player.game_id,
            "playerId": player.player_id,
            "clubId": player.club_id,
            "gameDate": game.game_date,
            "started": player.started,
            "minutes": player.minutes,
            "plusMinus": player.plus_minus,
            "fetchedAt": fetched_at,
            **{name: getattr(player.stats, name) for name in _COUNT_FIELDS},
        }
        for player in box.players
    ]
    return {"teamGameStats": team_stats, "playerGameStats": player_stats}


_COUNT_FIELDS = (
    "pts", "fg2m", "fg2a", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb",
    "ast", "tov", "stl", "blk", "pf", "fd",
)
