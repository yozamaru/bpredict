"""許可した項目だけを保持する解析結果。DB投入は別工程。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Counts:
    pts: int | None
    fg2m: int | None
    fg2a: int | None
    fg3m: int | None
    fg3a: int | None
    ftm: int | None
    fta: int | None
    oreb: int | None
    dreb: int | None
    ast: int | None
    tov: int | None
    stl: int | None
    blk: int | None
    pf: int | None
    fd: int | None


@dataclass(frozen=True)
class GameRecord:
    id: str
    year: int
    competition: str
    game_date: str
    tipoff_at: str
    finished_at: str
    finished_at_is_estimated: int
    home_club_id: str
    away_club_id: str
    home_name: str
    away_name: str
    venue_id: str | None
    venue_name: str | None
    home_score: int
    away_score: int
    attendance: int | None
    status: str = "FINISHED"


@dataclass(frozen=True)
class PlayerStats:
    game_id: str
    player_id: str
    club_id: str
    name: str
    number: str | None
    started: int | None
    minutes: float | None
    plus_minus: int | None
    stats: Counts


@dataclass(frozen=True)
class TeamStats:
    game_id: str
    club_id: str
    is_home: int
    stats: Counts
    possessions: float | None


@dataclass(frozen=True)
class BoxScore:
    game: GameRecord
    players: tuple[PlayerStats, ...]
    teams: tuple[TeamStats, ...]
