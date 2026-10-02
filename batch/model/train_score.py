"""得点差（Margin）と合計得点（Total）の回帰（基本設計 2.3 / 要件 6.1）。

**予想スコアは得点差と合計得点から導出する。**

    home = (total + margin) / 2
    away = (total - margin) / 2

各チーム得点を独立に回帰すると、「ホーム68%」なのに「82–85でアウェイ勝ち」という
出力が**原理的に起こる**（要件 6.1）。接戦ではほぼ確実に発生し、試合詳細画面で
勝率と並ぶため直接ユーザーに見える。上の導出なら整合が構造的に保証される。

**Margin は勝率のもう1つの経路でもある**（要件 6.1 の経路B）。

    P(home) = Φ(margin / σ)

σ は**残差の標準偏差を out-of-fold で実測する**。要件は「12〜13点というのは他リーグ
からの見当であって本データの実測値ではない。**実測前に定数として書かない**」と
定めている（P0-11）。
"""

from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np
import pandas as pd
from numpy.typing import NDArray

from batch.model.params import (
    EARLY_STOPPING_ROUND,
    NUM_BOOST_ROUND_MAX,
    SCORE_PARAMS,
)

type Floats = NDArray[np.float64]


class ScoreError(ValueError):
    """得点の導出を組めない入力。"""


def _booster():  # type: ignore[no-untyped-def]
    """LightGBM を遅延インポートする（`train_winner._booster` と同じ理由）。

    指標とベースラインのテストを LightGBM 抜きで走らせるため（macOS では
    `libomp` が必要で、環境の都合でインポートできないことがある）。
    """
    # 遅延インポートが目的
    import lightgbm

    return lightgbm


def learn_score(
    train_x: pd.DataFrame, train_y: Floats, train_w: Floats,
    valid_x: pd.DataFrame, valid_y: Floats,
    *, params: dict[str, object] | None = None,
    num_boost_round: int = NUM_BOOST_ROUND_MAX,
) -> tuple[Callable[[pd.DataFrame], Floats], int]:
    """1 fold を学習し、(予測関数, best_iteration) を返す。

    **Margin と Total で同じ関数を使う。** 目的変数が違うだけで、木の形も seed も
    同じにする（詳細設計 4.7 は回帰用の別のグリッドを定めていない）。
    `walk_forward(target=...)` に目的変数を渡して使い分ける。
    """
    lgb = _booster()
    settings = dict(SCORE_PARAMS if params is None else params)
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


def scores(margin: Floats, total: Floats) -> tuple[Floats, Floats]:
    """得点差と合計得点から両チームの得点を導出する。

    **丸めない。** 画面に出すときに整数へ丸める（要件 8.3 / 詳細設計 5.5）。
    ここで丸めると `home + away = total` が崩れ、整合の検査が通らなくなる。
    """
    margin = np.asarray(margin, dtype=np.float64)
    total = np.asarray(total, dtype=np.float64)
    if margin.shape != total.shape:
        raise ScoreError("得点差と合計得点の件数が合わない")
    return (total + margin) / 2.0, (total - margin) / 2.0


def win_prob_from_margin(margin: Floats, sigma: float) -> Floats:
    """経路B。`P(home) = Φ(margin / σ)`（要件 6.1）。

    σ は `Evaluation.residual_sigma`（out-of-fold の残差）から渡す。
    **定数を既定値にしない** — 要件が「実測前に定数として書かない」と定めている。
    """
    if not math.isfinite(sigma) or sigma <= 0:
        raise ScoreError("σ が正の有限値でない（残差から実測した値を渡す）")
    z = np.asarray(margin, dtype=np.float64) / sigma
    # Φ(z) = (1 + erf(z / √2)) / 2。scipy を入れずに標準ライブラリで出す
    return np.asarray(
        [(1.0 + math.erf(float(v) / math.sqrt(2.0))) / 2.0 for v in z],
        dtype=np.float64,
    )
