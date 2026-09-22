"""日程・疲労の特徴量（詳細設計 2.2 の採用区分）。

**Bリーグは土日2連戦が基本編成**であり、この区分は効果が見込める（要件 6.2 の #10）。
"""

from __future__ import annotations

from datetime import date

from batch.features.base import Context
from batch.features.constants import REST_DAYS_CLIP


def series_game_no(context: Context) -> int | None:
    """同一カード連戦の何戦目か。日程の属性であり、試合前に確定している。"""
    value = context.game["series_game_no"]
    return None if value is None or value != value else int(value)  # noqa: PLR0124 - NaN 判定


def previous_game(context: Context, club_id: str) -> dict[str, object] | None:
    """当季の直前の試合。なければ None。"""
    history = context.club_history(club_id, season_only=True)
    if history.empty:
        return None
    return dict(history.iloc[0])


def previous_result(context: Context, club_id: str) -> float | None:
    previous = previous_game(context, club_id)
    if previous is None or previous["result"] is None:
        return None
    return float(previous["result"])  # type: ignore[arg-type]


def previous_margin(context: Context, club_id: str) -> float | None:
    previous = previous_game(context, club_id)
    if previous is None or previous["margin"] is None:
        return None
    return float(previous["margin"])  # type: ignore[arg-type]


def rest_days(context: Context, club_id: str) -> int | None:
    """休養日数。**「中N日」の N を返す**（要件 6.2 の #12 が「中0日 / 中1日 / 中2日以上」）。

    暦日の差ではなく試合の間に空いた日数なので、土日2連戦の日曜は 0 になる。
    **シーズン跨ぎは上限でクリップする**（詳細設計 2.2）。シーズンを越えた直前試合も
    対象にするため、クリップしないとオフシーズンの約150日がそのまま入る
    （詳細設計 6.4 のテスト）。
    """
    history = context.club_history(club_id, season_only=False)
    if history.empty:
        return None
    previous_date = date.fromisoformat(str(history.iloc[0]["game_date"]))
    between = (date.fromisoformat(context.game_date) - previous_date).days - 1
    return min(max(between, 0), REST_DAYS_CLIP)


def away_streak(context: Context, club_id: str) -> int:
    """当季で連続しているアウェイ試合数。

    終了済みの試合だけを数える。日程（`is_home`）は試合前に確定しているが、
    未実施の試合を集計に含めない規約（CLAUDE.md 絶対ルール1）に合わせる。
    """
    streak = 0
    for _, row in context.club_history(club_id, season_only=True).iterrows():
        if int(row["is_home"]) == 1:
            break
        streak += 1
    return streak
