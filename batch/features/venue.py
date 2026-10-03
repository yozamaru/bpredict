"""会場に関する特徴量（詳細設計 2.2 の検証区分）。

いまあるのは #15（メイン会場か代替会場か）だけである。#16（移動距離）と
#29（動員）はまだ実装していない。

**`games.is_primary_venue` を読まない。** あの列は公開API（`venue.isPrimary`）の
ための派生値で、`rebuild_derived` が洗い替える（詳細設計 4.12）。特徴量は
**`club_seasons.primary_venue_id` と `games.venue_id` から直に計算する** —
列が古いまま学習に入ることを防ぐためで、マスタを直せばその場で反映される。
"""

from __future__ import annotations

import pandas as pd

from batch.features.base import Context


def is_primary_venue(context: Context) -> float | None:
    """対象試合が**ホームクラブの本拠会場**で行われるなら 1、代替会場なら 0。

    **`primary_venue_id` が無いクラブ×シーズンは None を返す**（関数内で埋めない。
    規約5）。「本拠が分からない」と「代替会場である」は違う。

    **対象試合自身の `venue_id` を読む。** これはリークではない — 会場は日程として
    試合前に確定している情報であり、`team_game_stats` のような結果ではない
    （詳細設計 2.1 の規約3が禁じているのは結果の参照である）。
    """
    venue_id = context.game.get("venue_id")
    if venue_id is None or pd.isna(venue_id):
        return None
    seasons = context.dataset.table("club_seasons")
    if "primary_venue_id" not in seasons.columns:
        return None
    row = seasons[
        (seasons["club_id"].astype(str) == context.home_club_id)
        & (seasons["season_id"].astype(str) == context.season_id)
    ]
    if row.empty:
        return None
    primary = row.iloc[0]["primary_venue_id"]
    if primary is None or pd.isna(primary):
        return None
    return 1.0 if str(venue_id) == str(primary) else 0.0
