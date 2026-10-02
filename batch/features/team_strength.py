"""チーム力の特徴量（詳細設計 2.2 の採用区分）。

Elo は**スナップショットの `team_ratings` を読む**。その場で計算しない
（全試合を走査する過程で対象試合を含めてしまう事故が起きる。詳細設計 1.4）。
"""

from __future__ import annotations

import pandas as pd

from batch.features.base import Context
from batch.features.constants import SEASON_REGRESSION_INITIAL, SHRINK_K


def elo(context: Context, club_id: str) -> float | None:
    """`as_of_date < 対象試合日` の最新行の Elo。

    `team_ratings` の1行はその試合日の**終了時点**の値なので、不等号は `<` で正しい
    （詳細設計 1.4）。`<=` にすると当日の結果が混入する。

    **索引があれば二分探索で引く**（`EloIndex`）。無い場合（索引を持たない
    `Context` を直接作ったテストなど）は同じ意味の走査で同じ値を返す。
    """
    if context.prepared is not None:
        return context.prepared.elo_index.at(club_id, context.game_date)

    ratings = context.dataset.table("team_ratings")
    rows = ratings[(ratings["club_id"] == club_id) & (ratings["as_of_date"] < context.game_date)]
    if rows.empty:
        return None
    latest = rows.sort_values("as_of_date").iloc[-1]
    return float(latest["elo"])


def winrate_recent(context: Context, club_id: str, window: int) -> float | None:
    """直近N試合の勝率。**シーズン境界を越えない**（詳細設計 6.4 のテスト）。"""
    history = context.club_history(club_id, season_only=True).head(window)
    results = history["result"].dropna()
    return None if results.empty else float(results.mean())


def margin_recent(context: Context, club_id: str, window: int) -> float | None:
    """直近N試合の平均得失点差。シーズン境界を越えない。"""
    history = context.club_history(club_id, season_only=True).head(window)
    margins = history["margin"].dropna()
    return None if margins.empty else float(margins.mean())


def margin_season(context: Context, club_id: str) -> float | None:
    """当季の平均得失点差。"""
    margins = context.club_history(club_id, season_only=True)["margin"].dropna()
    return None if margins.empty else float(margins.mean())


def off_rating(context: Context, club_id: str) -> float | None:
    """当季の 100ポゼッションあたり得点（詳細設計 2.2 の `ortg_diff`・**検証区分**）。

    **窓は当季とする。** 詳細設計 1.4 は「集計窓は本文書で定義されていない。工程8で
    採否を判断するときに決める」としている。採用済みの類似物（`margin_season_diff`）に
    合わせ、**新しい定数を増やさない**。縮約も入れない — `margin_season` も入れていない。

    **合計で割る**（試合ごとのレートを平均しない）。ORtg は「得点 ÷ ポゼッション」で
    あり、試合ごとの率の単純平均は、ポゼッションの少ない試合を過大に重みづける。

    `possessions` が NULL の試合は分母に入らない（詳細設計 1.3 の値域外は NULL）。
    1試合も残らなければ None を返す（関数内で0埋めしない。規約5）。
    """
    history = context.club_history(club_id, season_only=True)
    rows = context.stats_of(history, club_id)
    return _per_hundred(rows)


def def_rating(context: Context, club_id: str) -> float | None:
    """当季に**相手へ**許した 100ポゼッションあたり得点（詳細設計 2.2 の `drtg_diff`）。

    窓と集計の仕方は `off_rating` と同じ。**相手のポゼッションで割る** — 同じ試合の
    両チームのポゼッションはほぼ等しいが、推定値であり厳密には一致しないため、
    分子（相手の得点）と分母を同じ行から取る。

    要件 6.2 は #05 を「オフェンス/ディフェンスレーティング」として**1項目**で
    扱っている。採否は `ortg_diff` と対で判断する（片方だけでは、既にある
    `margin_season_diff` が純収支を持っているため増分が出ない）。
    """
    history = context.club_history(club_id, season_only=True)
    rows = context.stats_of_opponents(history)
    return _per_hundred(rows)


def _per_hundred(rows: pd.DataFrame) -> float | None:
    """`100 × Σ得点 ÷ Σポゼッション`。どちらかが欠ける行は落とす。"""
    if rows.empty:
        return None
    usable = rows[rows["pts"].notna() & rows["possessions"].notna()]
    if usable.empty:
        return None
    possessions = float(usable["possessions"].sum())
    if possessions <= 0:
        return None
    return 100.0 * float(usable["pts"].sum()) / possessions


def _previous_season_winrate(context: Context, club_id: str) -> float | None:
    """前季の勝率。縮約の prior を作るために使う。"""
    seasons = context.dataset.table("seasons").sort_values("start_date")
    order = list(seasons["id"])
    if context.season_id not in order:
        return None
    index = order.index(context.season_id)
    if index == 0:
        return None
    previous = order[index - 1]
    rows = context.finished_team_games
    rows = rows[(rows["club_id"] == club_id) & (rows["season_id"] == previous)]
    results = rows["result"].dropna()
    return None if results.empty else float(results.mean())


def winrate_season_shrunk(context: Context, club_id: str) -> float | None:
    """当季通算勝率を明示式で縮約する（詳細設計 2.6）。

        winrate = (w + k * prior) / (n + k)

    `prior` は前季勝率をシーズン間回帰させた値。前季がなければ 0.5 を使う
    （「平均方向へ回帰させた値」の極限であり、新規参入クラブの扱いと整合する）。
    """
    history = context.club_history(club_id, season_only=True)
    results = history["result"].dropna()
    previous = _previous_season_winrate(context, club_id)
    prior = 0.5 if previous is None else 0.5 + (previous - 0.5) * SEASON_REGRESSION_INITIAL
    played = len(results)
    if played == 0 and previous is None:
        return None
    wins = float(results.sum())
    return (wins + SHRINK_K * prior) / (played + SHRINK_K)
