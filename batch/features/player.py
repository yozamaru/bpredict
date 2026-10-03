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

    **クラブごとに1回だけ計算する**（`Context.cached`）。選手モデルの第2段は
    1試合に16行あり、行ごとに呼ぶと同じ並べ替えと集計を16回繰り返す
    （`player_rate.minutes_row`）。勝敗モデルからは1試合につきクラブごと1回
    しか呼ばれないため、そちらには影響しない。
    """
    def build() -> pd.Series:
        rows = context.player_history(club_id, season_only=True)
        if rows.empty:
            return pd.Series(dtype="float64")
        ordered = rows.sort_values("game_date", ascending=False, kind="stable")
        recent = ordered.groupby("player_id", sort=False).head(MINUTES_LOST_WINDOW)
        return recent.groupby("player_id")["minutes"].mean().dropna()

    return context.cached(("player.recent_minutes", club_id), build)


def _absent_players(context: Context, club_id: str) -> set[str]:
    """対象試合に出場登録されていない選手。

    エントリーは試合前に公開される情報であり（要件 5.5）、対象試合の
    `player_game_stats` を見るわけではない。
    """
    def build() -> set[str]:
        entries = context.entries
        if entries.empty:
            return set()
        club_players = set(
            context.player_history(club_id, season_only=False)["player_id"])
        entered = set(entries[entries["status"] == "ENTRY"]["player_id"])
        listed = set(entries["player_id"])
        # 登録の記載がある選手のうち、ENTRY でないもの。記載のない選手は判断材料がない
        return {p for p in club_players & listed if p not in entered}

    return context.cached(("player.absent", club_id), build)


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
    listed = set(entries["player_id"])
    for club_id in (context.home_club_id, context.away_club_id):
        club_players = set(context.player_history(club_id, season_only=False)["player_id"])
        if club_players and not (club_players & listed):
            return 0
    return 1
