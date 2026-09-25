"""学習行列の構築（基本設計 2.3）。入力はスナップショットだけである。

**`as_of` は必ず `games.tipoff_at` にする。** ここで別の時刻を渡すと、リークテストが
通っているのに学習だけがリークするという最悪の形になる。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd
from numpy.typing import NDArray

from batch.features.builder import FEATURE_KEYS, build_features
from batch.features.dataset import Dataset
from batch.model.params import TIME_DECAY_LAMBDA_INITIAL

type Floats = NDArray[np.float64]


class MatrixError(ValueError):
    """学習行列を組めない入力。"""


@dataclass(frozen=True)
class TrainingData:
    """時系列順に並んだ学習行列。**シャッフルしない**（要件 6.3）。"""

    features: pd.DataFrame
    #: ホーム勝利なら1
    home_win: Floats
    #: ホーム得点 − アウェイ得点
    margin: Floats
    #: 合計得点
    total: Floats
    game_ids: list[str]
    season_ids: list[str]
    #: その試合の JST 暦日（並び順の確認用）
    game_dates: list[str]
    #: 観客制限（1 / 0 / NULL）。**特徴量ではない。** 採否が定まっていないため
    #: 列として運び、重みにも特徴量にも使わない（下記）
    spectator_restricted: list[float | None]

    def __len__(self) -> int:
        return len(self.game_ids)

    @property
    def seasons(self) -> list[str]:
        """出現順のシーズン。**並べ替えない** — 時系列分割の順序がこれで決まる。"""
        seen: list[str] = []
        for season in self.season_ids:
            if season not in seen:
                seen.append(season)
        return seen


def _as_of(value: object) -> datetime:
    text = str(value)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        raise MatrixError("tipoff_at を時刻として読めない") from None


def build_matrix(ds: Dataset) -> TrainingData:
    """終了した試合すべてについて特徴量と目的変数を組む。

    **スコアが欠けている `FINISHED` の試合は落とす。** 0 として扱うと勝敗が反転し、
    Elo と学習の両方が壊れる（値域検証を通っていれば起こらないが、ここでも守る）。
    """
    games = ds.table("games")
    finished = games[games["status"] == "FINISHED"].copy()
    if finished.empty:
        raise MatrixError("終了した試合が1件もない")
    finished = finished.sort_values(["tipoff_at", "id"], kind="stable")

    rows: list[dict[str, float]] = []
    home_win: list[float] = []
    margin: list[float] = []
    total: list[float] = []
    game_ids: list[str] = []
    season_ids: list[str] = []
    game_dates: list[str] = []
    restricted: list[float | None] = []

    for game in finished.itertuples():
        if pd.isna(game.home_score) or pd.isna(game.away_score):
            continue
        # pandas の itertuples は列の型を Any にしないため、ここで数値へ寄せる
        home = float(str(game.home_score))
        away = float(str(game.away_score))
        rows.append(build_features(str(game.id), as_of=_as_of(game.tipoff_at), ds=ds))
        home_win.append(1.0 if home > away else 0.0)
        margin.append(home - away)
        total.append(home + away)
        game_ids.append(str(game.id))
        season_ids.append(str(game.season_id))
        game_dates.append(str(game.game_date))
        value = getattr(game, "spectator_restricted", None)
        restricted.append(None if pd.isna(value) else float(value))

    if not rows:
        raise MatrixError("スコアの揃った終了試合が1件もない")
    features = pd.DataFrame(rows, columns=list(FEATURE_KEYS))
    return TrainingData(
        features=features,
        home_win=np.asarray(home_win, dtype=np.float64),
        margin=np.asarray(margin, dtype=np.float64),
        total=np.asarray(total, dtype=np.float64),
        game_ids=game_ids,
        season_ids=season_ids,
        game_dates=game_dates,
        spectator_restricted=restricted,
    )


def time_decay_weights(
    season_ids: list[str], seasons: list[str], *,
    lam: float = TIME_DECAY_LAMBDA_INITIAL,
) -> Floats:
    """`weight = exp(-λ × 経過シーズン数)`（要件 6.6）。

    経過は `seasons` の末尾（最新）からの距離で数える。10年前の試合と先週の試合を
    同じ重みで学習しないための措置であり、ルール変更とリーグ再編の両方に効く。

    **λ は探索の対象である**（`TIME_DECAY_LAMBDA_GRID`）。ここでの既定値は出発点で
    あって確定値ではない。
    """
    if lam < 0:
        raise MatrixError("時間減衰の λ が負である")
    order = {season: index for index, season in enumerate(seasons)}
    newest = len(seasons) - 1
    unknown = [s for s in season_ids if s not in order]
    if unknown:
        raise MatrixError("シーズンの一覧にない試合がある")
    age = np.asarray([newest - order[s] for s in season_ids], dtype=np.float64)
    return np.exp(-lam * age)


def constant_columns(features: pd.DataFrame) -> list[str]:
    """分散が0の列。**採用ゲートの欠損率では捕まらない**（2026-09-25 に実測）。

    `minutes_lost_diff` / `top_players_out_diff` / `entry_is_official` は
    `game_entries` が空のとき**定数0**になる。NULL ではないため
    `feature_null_rates` は 0% と出て、「実装したのに効いていない特徴量」が
    静かに残る。LightGBM は分割に使えないため予測は壊れないが、**効いていない
    ことを知らないまま採用判定を通すのは別の問題である。**
    """
    return [str(name) for name in features.columns if features[name].nunique(dropna=False) <= 1]


def null_rates(features: pd.DataFrame) -> dict[str, float]:
    """列ごとの欠損率（詳細設計 4.6 の `feature_null_rates`）。"""
    return {str(name): float(features[name].isna().mean()) for name in features.columns}
