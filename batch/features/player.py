"""選手関連の特徴量（詳細設計 2.2 の採用区分）。

**チーム所属の判定に「現在の所属」を使わない。** `player_game_stats.club_id`（実績）を
使う。`players` の現在の所属で判定すると、移籍した選手が過去の所属チームから消え、
新チームへ過去の出場時間ごと移動する。`as_of` 規約には違反しないため、
**リークテストでも検出されない静かなバグ**になる（CLAUDE.md 絶対ルール1）。
"""

from __future__ import annotations

import pandas as pd

from batch.features.base import Context
from batch.features.constants import MINUTES_LOST_WINDOW, TOP_PLAYERS


def _recent_minutes(context: Context, club_id: str) -> pd.Series:
    """クラブに属した実績のある選手ごとの、直近N試合の平均出場時間。

    所属は `player_game_stats.club_id`（その試合で実際にどのクラブで出たか）で判定する。
    当季に限るのは、前季の別クラブでの出場時間を混ぜないため。
    """
    stats = context.finished_player_stats
    if stats.empty:
        return pd.Series(dtype="float64")
    season_games = context.finished_team_games
    season_game_ids = set(season_games[season_games["season_id"] == context.season_id]["game_id"])
    rows = stats[(stats["club_id"] == club_id) & (stats["game_id"].isin(season_game_ids))]
    if rows.empty:
        return pd.Series(dtype="float64")
    rows = rows.sort_values("game_date", ascending=False)
    recent = rows.groupby("player_id", sort=False).head(MINUTES_LOST_WINDOW)
    return recent.groupby("player_id")["minutes"].mean().dropna()


def _absent_players(context: Context, club_id: str) -> set[str]:
    """対象試合に出場登録されていない選手。

    エントリーは試合前に公開される情報であり（要件 5.5）、対象試合の
    `player_game_stats` を見るわけではない。
    """
    entries = context.entries
    if entries.empty:
        return set()
    stats = context.finished_player_stats
    club_players = set(stats[stats["club_id"] == club_id]["player_id"])
    entered = set(entries[entries["status"] == "ENTRY"]["player_id"])
    listed = set(entries["player_id"])
    # 登録の記載がある選手のうち、ENTRY でないもの。記載のない選手は判断材料がない
    return {player for player in club_players & listed if player not in entered}


def minutes_lost(context: Context, club_id: str) -> float | None:
    """欠場者の直近平均出場時間の合計。"""
    minutes = _recent_minutes(context, club_id)
    if minutes.empty:
        return None
    absent = _absent_players(context, club_id)
    return float(minutes[minutes.index.isin(absent)].sum())


def top_players_out(context: Context, club_id: str) -> int | None:
    """直近出場時間上位N名のうち欠場している人数（詳細設計 2.2 は「上位5名」）。"""
    minutes = _recent_minutes(context, club_id)
    if minutes.empty:
        return None
    top = set(minutes.sort_values(ascending=False).head(TOP_PLAYERS).index)
    return len(top & _absent_players(context, club_id))


def entry_is_official(context: Context) -> int:
    """両チームのエントリーが公式確定なら1（詳細設計 2.2）。

    推定（`ESTIMATED`）の行が1件でも残る試合は 0。両チームぶんの行が
    揃っていない場合も 0（片側だけ公式でも「確定」ではない）。
    """
    entries = context.entries
    if entries.empty or (entries["source"] != "OFFICIAL").any():
        return 0
    stats = context.finished_player_stats
    listed = set(entries["player_id"])
    for club_id in (context.home_club_id, context.away_club_id):
        club_players = set(stats[stats["club_id"] == club_id]["player_id"])
        if club_players and not (club_players & listed):
            return 0
    return 1
