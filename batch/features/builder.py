"""特徴量ベクトルの組み立て（詳細設計 2.1 / 2.2）。

各特徴量関数は**欠損時に None を返す**。0埋めは関数内で行わず、ここで
文書が定める既定値へ変換する（詳細設計 2.1 の規約5）。既定値を関数内に
散らすと「欠損」と「本当に0」が区別できなくなる。

命名は `{カテゴリ}_{内容}_{対象}` で、対象は `home` / `away` / `diff`。
**可能な限り `diff` に集約する**（列数が半減し、対称性の事前知識が入る）。
`is_home` は特徴量にしない（ホーム視点固定で学習するため常に1の定数列になる）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from batch.features import player, schedule_ctx, team_strength, venue
from batch.features.base import Context, build_context
from batch.features.constants import ELO_DEFAULT, RECENT_WINDOWS
from batch.features.dataset import Dataset
from batch.features.prepared import Prepared

#: 欠損時の既定値（詳細設計 2.2 の表）。**キーの集合はこの辞書が正**。
DEFAULTS: dict[str, float] = {
    "elo_diff": 0.0,
    "elo_home": ELO_DEFAULT,
    "elo_away": ELO_DEFAULT,
    "winrate_l5_diff": 0.0,
    "winrate_l10_diff": 0.0,
    "winrate_season_diff": 0.0,
    "margin_l5_diff": 0.0,
    "margin_season_diff": 0.0,
    # 検証区分（詳細設計 2.2）。効かなければ落とす
    "ortg_diff": 0.0,
    "drtg_diff": 0.0,
    "tov_rate_diff": 0.0,
    "oreb_rate_diff": 0.0,
    # #15: 本拠が分からないクラブ×シーズンは**本拠として扱う**。
    # `games.is_primary_venue INTEGER NOT NULL DEFAULT 1`（詳細設計 1.3）と同じ既定で、
    # 新しい値を決めていない
    "is_primary_venue": 1.0,
    "series_game_no": 1.0,
    "prev_result_diff": 0.0,
    "prev_margin_diff": 0.0,
    "rest_days_diff": 0.0,
    "away_streak_away": 0.0,
    "minutes_lost_diff": 0.0,
    "top_players_out_diff": 0.0,
    "entry_is_official": 0.0,
}

FEATURE_KEYS = tuple(DEFAULTS)


def _diff(
    context: Context,
    compute: Callable[[Context, str], float | int | None],
) -> float | None:
    """ホームとアウェイの差。**片側でも欠けたら差は作れない**ので None を返す。"""
    home = compute(context, context.home_club_id)
    away = compute(context, context.away_club_id)
    if home is None or away is None:
        return None
    return float(home) - float(away)


def _winrate_window(window: int) -> Callable[[Context, str], float | None]:
    def compute(context: Context, club_id: str) -> float | None:
        return team_strength.winrate_recent(context, club_id, window)

    return compute


def _margin_window(window: int) -> Callable[[Context, str], float | None]:
    def compute(context: Context, club_id: str) -> float | None:
        return team_strength.margin_recent(context, club_id, window)

    return compute


def build_features(
    game_id: str, as_of: datetime, ds: Dataset,
    prepared: Prepared | None = None,
) -> dict[str, float]:
    """`as_of` 時点で確定している情報のみから特徴量を生成する。

    as_of    : 参照してよい情報の上限時刻（= `games.tipoff_at`）
    ds       : `batch/snapshot/*.parquet` から読み込んだメモリ内データセット
               （**D1 は読まない**。CLAUDE.md 絶対ルール3）
    prepared : 前処理の索引（`prepare(ds)`）。**多数の試合を回すときは外で1回
               作って渡す。** 省略すると試合ごとに組み直すため、6,270試合で
               43分かかる（2026-10-02 の実測）。渡しても**値は変わらない**
               （絞り込みは `build_context` が行う）
    """
    context = build_context(game_id, as_of, ds, prepared)
    raw: dict[str, float | int | None] = {
        "elo_diff": _diff(context, team_strength.elo),
        "elo_home": team_strength.elo(context, context.home_club_id),
        "elo_away": team_strength.elo(context, context.away_club_id),
        "winrate_season_diff": _diff(context, team_strength.winrate_season_shrunk),
        "margin_season_diff": _diff(context, team_strength.margin_season),
        "ortg_diff": _diff(context, team_strength.off_rating),
        "drtg_diff": _diff(context, team_strength.def_rating),
        "tov_rate_diff": _diff(context, team_strength.turnover_rate),
        "oreb_rate_diff": _diff(context, team_strength.offensive_reb_rate),
        "is_primary_venue": venue.is_primary_venue(context),
        "series_game_no": schedule_ctx.series_game_no(context),
        "prev_result_diff": _diff(context, schedule_ctx.previous_result),
        "prev_margin_diff": _diff(context, schedule_ctx.previous_margin),
        "rest_days_diff": _diff(context, schedule_ctx.rest_days),
        "away_streak_away": schedule_ctx.away_streak(context, context.away_club_id),
        "minutes_lost_diff": _diff(context, player.minutes_lost),
        "top_players_out_diff": _diff(context, player.top_players_out),
        "entry_is_official": player.entry_is_official(context),
    }
    for window in RECENT_WINDOWS:
        raw[f"winrate_l{window}_diff"] = _diff(context, _winrate_window(window))
    raw["margin_l5_diff"] = _diff(context, _margin_window(RECENT_WINDOWS[0]))

    if set(raw) != set(DEFAULTS):
        missing = set(DEFAULTS) ^ set(raw)
        raise ValueError(f"特徴量のキーが定義と一致しない: {sorted(missing)}")
    # 返す順序は DEFAULTS の順に固定する（列順が実行ごとに変わらないようにする）
    features: dict[str, float] = {}
    for key, default in DEFAULTS.items():
        value = raw[key]
        features[key] = default if value is None else float(value)
    return features
