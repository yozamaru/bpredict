"""選手モデル（基本設計 2.3 / 詳細設計 2.3.1 / 4.5）。

**いま実装してあるのは第2段（PlayerMinutes）だけである。**

| 段 | 状態 |
|---|---|
| 第1段 PlayerAvail | **未実装。** 「出場しうる選手」を列挙できない（2.3.1） |
| 第2段 PlayerMinutes | **本モジュール。** `E[出場時間 | 出場]` |
| 第3段 PlayerRates | **未実装。** `k` / `prior` / `usage_l10` / `position` が未定義（2.3.1） |
| 第4段 Reconciliation | `batch/model/reconcile.py`（実装済み） |

**出場時間は非負に収める。** 回帰は負を出しうるが、`player_predictions.pred_minutes`
には `CHECK (pred_minutes >= 0)` があり（詳細設計 1.5）、整合化は出場時間を
200分へ正規化するため符号が反転すると配分が壊れる（2.4 の手順2）。

**上限は設けない。** 延長戦では40分を超える（要件 6.8.5 は `200 + 25 × 延長回数`）。
値域検証は `minutes` 0–60 を課すが（基本設計 2.1）、それは**取り込みの検証**で
あって予測の上限ではない。
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd
from numpy.typing import NDArray

from batch.model.dataset import PlayerMinutesData
from batch.model.evaluate import MAX_FOLDS, Evaluation, walk_forward
from batch.model.params import NUM_BOOST_ROUND_MAX
from batch.model.train_score import learn_score

type Floats = NDArray[np.float64]


class PlayerModelError(ValueError):
    """選手モデルを学習・推論できない入力。"""


def clip_minutes(values: Floats) -> Floats:
    """出場時間を非負に収める（上限は設けない。上記）。"""
    return np.maximum(np.asarray(values, dtype=np.float64), 0.0)


def learn_minutes(
    *, num_boost_round: int = NUM_BOOST_ROUND_MAX,
) -> Callable[..., tuple[Callable[[pd.DataFrame], Floats], int]]:
    """第2段の学習関数。**木の形は Margin / Total と同じ**（4.7 は回帰用の
    別のグリッドを定めていない）。出力の丸めだけが違う。

    `num_boost_round` の既定は 4.7 の上限（1,200）である。**本番でこれを下げない。**
    """
    def learn(
        train_x: pd.DataFrame, train_y: Floats, train_w: Floats,
        valid_x: pd.DataFrame, valid_y: Floats,
    ) -> tuple[Callable[[pd.DataFrame], Floats], int]:
        predict, best = learn_score(
            train_x, train_y, train_w, valid_x, valid_y,
            num_boost_round=num_boost_round)

        def bounded(features: pd.DataFrame) -> Floats:
            return clip_minutes(predict(features))

        return bounded, best

    return learn


def mean_learner() -> Callable[..., tuple[Callable[[pd.DataFrame], Floats], int]]:
    """**ベースライン。学習データの平均をそのまま返す。**

    要件 6.4 はベースラインとの比較を求めている。**`MAE / 実績SD` で代用しない**
    （その比が 0.798 で平均と同等になるのは目的変数が正規分布のときだけで、
    出場時間は 0〜40分に収まる有界な分布である）。
    """
    def learn(
        train_x: pd.DataFrame, train_y: Floats, train_w: Floats,
        valid_x: pd.DataFrame, valid_y: Floats,
    ) -> tuple[Callable[[pd.DataFrame], Floats], int]:
        value = float(np.asarray(train_y, dtype=np.float64).mean())

        def predict(features: pd.DataFrame) -> Floats:
            return np.full(len(features), value, dtype=np.float64)

        return predict, 0

    return learn


def recent_learner(column: str = "minutes_l5_player") -> Callable[
        ..., tuple[Callable[[pd.DataFrame], Floats], int]]:
    """**もう1つのベースライン。その選手の直近N試合の平均をそのまま返す。**

    学習しない（列をそのまま出す）。**平均ベースラインだけでは足りない** —
    出場時間は選手ごとに大きく違うため、リーグ全体の平均（約19分）に対する
    改善は「選手ごとの水準を知っているか」をほとんど測っていない。
    **モデルが「直近の平均を出すだけ」を超えているかは、こちらで測る。**
    """
    def learn(
        train_x: pd.DataFrame, train_y: Floats, train_w: Floats,
        valid_x: pd.DataFrame, valid_y: Floats,
    ) -> tuple[Callable[[pd.DataFrame], Floats], int]:
        if column not in train_x.columns:
            raise PlayerModelError(f"ベースラインの列がない: {column}")

        def predict(features: pd.DataFrame) -> Floats:
            return clip_minutes(features[column].to_numpy(dtype=np.float64))

        return predict, 0

    return learn


def evaluate_minutes(
    data: PlayerMinutesData, *, max_folds: int = MAX_FOLDS,
    num_boost_round: int = NUM_BOOST_ROUND_MAX,
) -> Evaluation:
    """第2段の walk-forward 評価。**分割器を2つ作らない**（`evaluate.py`）。

    指標は `mae` である。**`brier` や `ece` を呼ばない** — 0/1 の目的変数では
    なく、`Evaluation._require_binary` が落とす。
    """
    if len(data) == 0:
        raise PlayerModelError("学習行が1件もない")
    return walk_forward(
        data.as_training_data(),
        learn_minutes(num_boost_round=num_boost_round),
        target=data.minutes,
        max_folds=max_folds,
    )


def evaluate_baseline(
    data: PlayerMinutesData, *, max_folds: int = MAX_FOLDS,
    learner: Callable[..., tuple[Callable[[pd.DataFrame], Floats], int]] | None = None,
) -> Evaluation:
    """同じ fold でベースラインを測る。**同一の分割で比べる**（4.6）。

    既定は平均（`mean_learner`）。`recent_learner()` を渡すと「直近N試合の平均を
    そのまま出す」ベースラインになる。**両方を測る** — 前者だけでは
    「選手ごとの水準を知っているか」しか見えない。
    """
    if len(data) == 0:
        raise PlayerModelError("学習行が1件もない")
    return walk_forward(
        data.as_training_data(),
        mean_learner() if learner is None else learner,
        target=data.minutes,
        max_folds=max_folds,
    )
