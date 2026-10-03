"""選手視点の特徴量（詳細設計 2.3 / 2.3.1）。

**1行は「1試合 × 1選手」である。** 段ごとに行の集合が違う（2.3.1）。

| 段 | 行の集合 | いま組めるか |
|---|---|---|
| 第1段 PlayerAvail | 試合 × 出場しうる選手 | **組めない**（候補を列挙できない） |
| 第2段 PlayerMinutes | 試合 × **出場した選手** | **組める。本モジュールの対象** |
| 第3段 PlayerRates | 同上 | **組めない**（`k` / `prior` / `usage_l10` / `position` が未定義） |

**`player_game_stats` に行があることが「出場した」の定義である。** 第2段は
`E[出場時間 | 出場]` を学習するため、この表がそのまま行の集合になり、追加の
前提を要しない。

**所属の判定は `player_game_stats.club_id`（実績）で行う**（2.1 の規約6）。
`players` の現在の所属で判定すると、移籍した選手が過去の所属クラブから消え、
**リークテストでも検出されない静かなバグ**になる。
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from batch.features import player as player_features
from batch.features.base import Context, build_context
from batch.features.constants import RECENT_WINDOWS
from batch.features.dataset import Dataset
from batch.features.errors import FeatureError
from batch.features.prepared import Prepared

#: 直近N試合の窓。**新しい定数を増やさない**（`RECENT_WINDOWS` の 5 と 10）。
SHORT_WINDOW, LONG_WINDOW = RECENT_WINDOWS

#: 欠損時の既定値（2.3.1 の表）。`minutes_l5_player` は既定値を持たない
#: （欠けたら行を落とす）ため、ここには入らない。
DEFAULTS: dict[str, float] = {"is_starter_l5": 0.0, "team_minutes_lost": 0.0}

#: 第2段（PlayerMinutes）の列。順序は固定する。
MINUTES_KEYS: tuple[str, ...] = (
    "minutes_l5_player", "minutes_l10_player", "is_starter_l5", "team_minutes_lost",
)


def _by_player(context: Context, club_id: str) -> dict[str, pd.DataFrame]:
    """そのクラブの過去試合を選手ごとに分け、**新しい順**に並べて返す。

    1試合に24人ぶん呼ばれるため、**クラブごとに1回だけ**グループ化する
    （`Context.cached`）。選手ごとに絞り込むと同じ切り出しを24回繰り返す。

    **並べ替えは安定ソートで行う。** 同じ日に複数試合があると順序が入力の並びで
    決まるが（2.1.1 の注記3）、索引が原順序を保つため実行ごとには変わらない。
    """
    def build() -> dict[str, pd.DataFrame]:
        rows = context.player_history(club_id, season_only=True)
        if rows.empty:
            return {}
        ordered = rows.sort_values("game_date", ascending=False, kind="stable")
        return {
            str(player_id): group
            for player_id, group in ordered.groupby("player_id", sort=False)
        }

    return context.cached(("player_rate.by_player", club_id), build)


def _recent(context: Context, club_id: str, player_id: str, window: int) -> pd.DataFrame:
    return _by_player(context, club_id).get(player_id, pd.DataFrame()).head(window)


def minutes_recent(
    context: Context, club_id: str, player_id: str, window: int,
) -> float | None:
    """その選手の直近N試合の平均出場時間。

    **シーズン境界を越えない**（2.2.1 と同じ。`winrate_recent` の規約に合わせる）。
    季の序盤は窓に届かないが、**ある分だけで集計する**。
    """
    rows = _recent(context, club_id, player_id, window)
    if rows.empty:
        return None
    values = rows["minutes"].dropna()
    return None if values.empty else float(values.mean())


def starter_rate(context: Context, club_id: str, player_id: str) -> float | None:
    """直近5試合のスターター率（`started` の平均）。"""
    rows = _recent(context, club_id, player_id, SHORT_WINDOW)
    if rows.empty:
        return None
    values = rows["started"].dropna()
    return None if values.empty else float(values.mean())


def minutes_row(
    context: Context, club_id: str, player_id: str,
) -> dict[str, float] | None:
    """第2段（PlayerMinutes）の1行を、**できあいの `Context` から**作る。

    **1試合ぶんの選手をまとめるときは、`Context` を試合ごとに1回だけ作って
    この関数を呼ぶ。** 行ごとに `build_context` を呼ぶと、`Context` の記憶
    （`_by_player` のグループ化と `minutes_lost` の集計）が毎行捨てられる。
    1試合16人で16倍の無駄になり、実データ 146,463行では**19分かかる**
    （2026-10-03 の実測。2.1.1 で同じ罠を踏んでいる）。

    **`minutes_l5_player` が欠けたら `None` を返す**（行を落とす）。その選手の
    過去が1試合も無ければ出場時間を予測する材料が無く、既定値で埋めると
    「平均を出しているだけ」の行が学習に混ざる（2.3.1）。

    `club_id` は対象試合の出場クラブでなければならない。別のクラブを渡すのは
    呼び出し側の誤りであり、黙って計算しない。
    """
    if club_id not in (context.home_club_id, context.away_club_id):
        raise FeatureError(f"この試合に出場しないクラブ: {club_id}")

    short = minutes_recent(context, club_id, player_id, SHORT_WINDOW)
    if short is None:
        return None
    long = minutes_recent(context, club_id, player_id, LONG_WINDOW)
    if long is None:
        return None

    started = starter_rate(context, club_id, player_id)
    lost = player_features.minutes_lost(context, club_id)
    row: dict[str, float] = {
        "minutes_l5_player": short,
        "minutes_l10_player": long,
        "is_starter_l5": (
            DEFAULTS["is_starter_l5"] if started is None else started),
        "team_minutes_lost": (
            DEFAULTS["team_minutes_lost"] if lost is None else lost),
    }
    if set(row) != set(MINUTES_KEYS):
        raise ValueError(
            f"特徴量のキーが定義と一致しない: {sorted(set(row) ^ set(MINUTES_KEYS))}")
    return {key: row[key] for key in MINUTES_KEYS}


def build_minutes_features(
    game_id: str, as_of: datetime, ds: Dataset, club_id: str, player_id: str,
    prepared: Prepared | None = None,
) -> dict[str, float] | None:
    """1行だけ作る入口。**多数の行を回すときは使わない**（上記）。"""
    return minutes_row(
        build_context(game_id, as_of, ds, prepared), club_id, player_id)
