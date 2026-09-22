"""特徴量の共通の足場。

**すべての特徴量は `as_of` 時点で確定している情報だけから作る**（CLAUDE.md 絶対ルール1）。
そのための絞り込みをここに1か所で実装し、各特徴量は `Context` を通してしか
データを見ない。絞り込みが各関数に散ると、一箇所直し忘れただけでリークが復活する。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from batch.features.dataset import Dataset


class FeatureError(RuntimeError):
    """対象試合が見つからない、または必要な列が欠けている。"""


@dataclass(frozen=True)
class Context:
    """対象試合と、`as_of` 時点で参照してよい過去データ。"""

    game_id: str
    as_of: datetime
    game: pd.Series
    home_club_id: str
    away_club_id: str
    season_id: str
    game_date: str
    dataset: Dataset

    #: 終了済みかつ `finished_at <= as_of` の `team_games`。対象試合は除外済み
    finished_team_games: pd.DataFrame
    #: 同条件の `player_game_stats`
    finished_player_stats: pd.DataFrame
    #: 対象試合のエントリー（試合前に公開される情報。要件 5.5）
    entries: pd.DataFrame

    def club_history(self, club_id: str, *, season_only: bool) -> pd.DataFrame:
        """あるクラブの過去試合。新しい順に並べて返す。"""
        rows = self.finished_team_games[self.finished_team_games["club_id"] == club_id]
        if season_only:
            rows = rows[rows["season_id"] == self.season_id]
        return rows.sort_values(["finished_at", "game_id"], ascending=False)


def _to_utc(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, format="ISO8601")


def build_context(game_id: str, as_of: datetime, dataset: Dataset) -> Context:
    """`as_of` による絞り込みを1か所で行う。

    - 絞り込みは **`finished_at <= as_of`**。`tipoff_at` ではない（17:05開始・19:05終了の
      試合が19:05開始の試合の確定情報として混入する）
    - `finished_at` が NULL の試合（`SCHEDULED` / `POSTPONED` / `CANCELLED`）は入れない
    - **対象試合自身を明示的に除外する。** `as_of` の比較だけに頼らない
    """
    if as_of.tzinfo is None:
        raise FeatureError("as_of にタイムゾーンが必要")

    games = dataset.table("games")
    target = games[games["id"] == game_id]
    if len(target) != 1:
        raise FeatureError("対象試合が1件に定まらない")
    game = target.iloc[0]

    team_games = dataset.table("team_games")
    finished = team_games[team_games["finished_at"].notna()].copy()
    finished["finished_at_utc"] = _to_utc(finished["finished_at"])
    finished = finished[
        (finished["finished_at_utc"] <= as_of) & (finished["game_id"] != game_id)
    ]

    player_stats = dataset.table("player_game_stats")
    allowed_games = set(finished["game_id"])
    finished_players = player_stats[player_stats["game_id"].isin(allowed_games)]

    entries = dataset.table("game_entries")
    entries = entries[entries["game_id"] == game_id]

    return Context(
        game_id=game_id,
        as_of=as_of,
        game=game,
        home_club_id=str(game["home_club_id"]),
        away_club_id=str(game["away_club_id"]),
        season_id=str(game["season_id"]),
        game_date=str(game["game_date"]),
        dataset=dataset,
        finished_team_games=finished,
        finished_player_stats=finished_players,
        entries=entries,
    )
