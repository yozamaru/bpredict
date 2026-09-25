"""勝敗モデル（経路A）の学習（詳細設計 4.5 / 4.7）。

**経路Aは Winner の出力をそのまま勝率とする。** 経路B（Margin から
`Φ(margin/σ)` で導く）は Margin モデルを要するため `train_score.py` で扱い、
P0-11 の比較はそちらが揃ってから行う。

**LightGBM をここだけに閉じ込める。** 指標・ベースライン・walk-forward は
LightGBM を知らない（テストが GBDT なしで書けるようにするため）。
"""
from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd
from numpy.typing import NDArray

from batch.model.params import (
    EARLY_STOPPING_ROUND,
    NUM_BOOST_ROUND_MAX,
    WINNER_PARAMS,
)

type Floats = NDArray[np.float64]


def _booster():  # type: ignore[no-untyped-def]
    """LightGBM を遅延インポートする。

    指標とベースラインのテストは LightGBM 抜きで走らせたい（macOS では
    `libomp` が必要で、環境の都合でインポートできないことがある）。
    """
    # 遅延インポートが目的（下記）
    import lightgbm

    return lightgbm


def learn_winner(
    train_x: pd.DataFrame, train_y: Floats, train_w: Floats,
    valid_x: pd.DataFrame, valid_y: Floats,
    *, params: dict[str, object] | None = None,
    num_boost_round: int = NUM_BOOST_ROUND_MAX,
) -> tuple[Callable[[pd.DataFrame], Floats], int]:
    """1 fold を学習し、(予測関数, best_iteration) を返す。

    **検証セットを必ず渡す。** early stopping は検証セットがあって初めて成立する。
    `num_boost_round` の上限は 1,200 で、artifact サイズが 1.5MB を超えないための
    制約でもある（詳細設計 4.7）。
    """
    lgb = _booster()
    settings = dict(WINNER_PARAMS if params is None else params)
    train_set = lgb.Dataset(train_x, label=train_y, weight=train_w, free_raw_data=False)
    valid_set = lgb.Dataset(valid_x, label=valid_y, reference=train_set, free_raw_data=False)
    booster = lgb.train(
        settings,
        train_set,
        num_boost_round=num_boost_round,
        valid_sets=[valid_set],
        callbacks=[lgb.early_stopping(EARLY_STOPPING_ROUND, verbose=False)],
    )
    best = int(booster.best_iteration or num_boost_round)

    def predict(features: pd.DataFrame) -> Floats:
        return np.asarray(
            booster.predict(features, num_iteration=best), dtype=np.float64,
        )

    return predict, best


def train_final(
    features: pd.DataFrame, actual: Floats, weights: Floats, *,
    num_boost_round: int, params: dict[str, object] | None = None,
) -> tuple[object, str]:
    """最終モデル。**各 fold の `best_iteration` の中央値で固定学習する**（基本設計 2.3）。

    early stopping を使わない（検証セットを学習に含めるため）。返すのは
    booster と `save_model()` のテキストで、後者を `model_versions.artifact_text`
    として登録する（詳細設計 1.6）。
    """
    lgb = _booster()
    settings = dict(WINNER_PARAMS if params is None else params)
    if num_boost_round < 1 or num_boost_round > NUM_BOOST_ROUND_MAX:
        raise ValueError("num_boost_round が範囲外である")
    train_set = lgb.Dataset(features, label=actual, weight=weights, free_raw_data=False)
    booster = lgb.train(settings, train_set, num_boost_round=num_boost_round)
    return booster, str(booster.model_to_string())


def median_iteration(values: list[int]) -> int:
    """`best_iteration` の中央値。**平均にしない** — 1 fold の外れ値に引かれる。"""
    if not values:
        raise ValueError("best_iteration が空である")
    return int(np.median(np.asarray(values, dtype=np.float64)))
