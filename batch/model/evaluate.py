"""walk-forward 検証とベースライン比較（基本設計 2.3 / 詳細設計 4.6）。

```
fold i:  train = seasons[:i-1]
         valid = seasons[i-1]     ← early stopping 用
         test  = seasons[i]       ← 一切触れない
```

**ランダムシャッフル分割を使わない**（要件 6.3）。**test に触れない** — 旧設計は
`train_lightgbm(X, y)` に検証セットがなく、early stopping が成立していなかった
（学習データを渡せば止まらず、評価シーズンを渡せばリーク）。
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from numpy.typing import NDArray

from batch.model.dataset import TrainingData
from batch.model.metrics import accuracy, brier, ece, log_loss
from batch.model.params import MAX_FOLDS

type Floats = NDArray[np.float64]

#: (学習X, 学習y, 学習重み, 検証X, 検証y) → (テストXに対する予測を返す関数, best_iteration)
type Learner = Callable[
    [pd.DataFrame, Floats, Floats, pd.DataFrame, Floats],
    tuple[Callable[[pd.DataFrame], Floats], int],
]


class EvaluationError(ValueError):
    """検証を組めない入力。"""


@dataclass(frozen=True)
class Fold:
    test_season: str
    train_seasons: tuple[str, ...]
    valid_season: str
    n_train: int
    n_valid: int
    n_test: int
    best_iteration: int
    #: テスト fold の予測と実績。集約はまとめて行う（fold ごとの平均を平均しない）
    probs: Floats
    actual: Floats

    @property
    def brier(self) -> float:
        return brier(self.probs, self.actual)


@dataclass(frozen=True)
class Evaluation:
    folds: tuple[Fold, ...] = field(default_factory=tuple)

    @property
    def probs(self) -> Floats:
        return np.concatenate([f.probs for f in self.folds]) if self.folds else np.empty(0)

    @property
    def actual(self) -> Floats:
        return np.concatenate([f.actual for f in self.folds]) if self.folds else np.empty(0)

    @property
    def n(self) -> int:
        return int(self.probs.size)

    @property
    def brier(self) -> float:
        """**fold をまとめてから測る。** fold ごとの Brier を平均すると、件数の違う
        fold が同じ重みになり、n の小さい fold のノイズが効きすぎる。"""
        self._require_binary("brier")
        return brier(self.probs, self.actual)

    @property
    def accuracy(self) -> float:
        self._require_binary("accuracy")
        return accuracy(self.probs, self.actual)

    @property
    def log_loss(self) -> float:
        self._require_binary("log_loss")
        return log_loss(self.probs, self.actual)

    @property
    def ece(self) -> float | None:
        self._require_binary("ece")
        return ece(self.probs, self.actual)

    @property
    def mae(self) -> float:
        """平均絶対誤差。**得点差・合計得点の指標**（要件 6.4）。"""
        if not self.folds:
            return float("nan")
        return float(np.abs(self.probs - self.actual).mean())

    @property
    def residual_sigma(self) -> float:
        """残差の標準偏差。**経路B の `Φ(margin / σ)` の σ**（要件 6.1 / P0-11）。

        **out-of-fold の残差から測る。** 学習データの残差で測ると σ が小さく出て、
        経路B の確率が過信になる。
        """
        if not self.folds:
            return float("nan")
        return float(np.std(self.actual - self.probs, ddof=1))

    @property
    def best_iterations(self) -> list[int]:
        return [f.best_iteration for f in self.folds]

    def _require_binary(self, name: str) -> None:
        """**回帰の結果に分類の指標を呼ばせない。**

        `Evaluation` は分割を1つに保つため分類と回帰で共用するが、`brier` や `ece` を
        得点差の評価に対して呼ぶと**意味のない数字が黙って返る**。実測値が 0/1 で
        ないときは落とす。
        """
        if not self.folds:
            return
        unique = np.unique(self.actual)
        if not np.all((unique == 0.0) | (unique == 1.0)):
            raise EvaluationError(
                f"{name} は 0/1 の目的変数にしか意味がない（回帰の結果には mae を使う）",
            )


def folds_of(seasons: Sequence[str], *, max_folds: int = MAX_FOLDS) -> list[int]:
    """テストに使えるシーズンの位置。**学習と検証にそれぞれ1シーズン以上を要する。**

    先頭2シーズンはテストにできない（i=0 は学習も検証もなく、i=1 は検証だけで
    学習がない）。直近 `max_folds` 個に限るのは、シーズンが増えるたびに学習時間が
    単調増加するのを防ぐため（基本設計 2.3）。
    """
    if len(seasons) < 3:
        return []
    usable = list(range(2, len(seasons)))
    return usable[-max_folds:]


def walk_forward(
    data: TrainingData, learn: Learner, *,
    target: Floats | None = None, eval_target: Floats | None = None,
    weights: Floats | None = None, max_folds: int = MAX_FOLDS,
) -> Evaluation:
    """`target` を省略すると勝敗（`home_win`）を学習する。

    **分割器を2つ作らない。** 得点差・合計得点の回帰も同じこの関数を通す。
    P0-11（勝率の経路 A / B の比較）は「**同一の walk-forward ウィンドウ**で
    Brier と ECE を測る」ことを要件が定めており（要件 6.1）、分割の実装が2つあると
    **片方だけ直したときに比較が成り立たなくなる。**

    `eval_target` を渡すと、**学習は `target`、評価は `eval_target`** で行う。
    個人スタッツの成功率3項目で要る — 目的変数がシュリンク済み（`k` に依存する）
    であるため、**`k` を変えると目的変数そのものが変わり MAE が比較できない**
    （2.3.1 の実測）。固定の物差し（実現値）に対して測るために使う。
    **測定のために別の分割を書かない**ためにここへ置く。
    """
    seasons = data.seasons
    positions = folds_of(seasons, max_folds=max_folds)
    if not positions:
        raise EvaluationError(
            f"walk-forward には3シーズン以上が必要（いまは {len(seasons)}）",
        )
    season_ids = np.asarray(data.season_ids)
    y = data.home_win if target is None else np.asarray(target, dtype=np.float64)
    if y.size != len(data):
        raise EvaluationError("目的変数の件数が学習行列と合わない")
    w = np.ones(len(data)) if weights is None else np.asarray(weights, dtype=np.float64)
    if w.size != len(data):
        raise EvaluationError("重みの件数が学習行列と合わない")
    # **評価の目的変数は既定で学習と同じ。** 渡されたときだけ分かれる
    actual = y if eval_target is None else np.asarray(eval_target, dtype=np.float64)
    if actual.size != len(data):
        raise EvaluationError("評価の目的変数の件数が学習行列と合わない")

    results: list[Fold] = []
    for index in positions:
        train_seasons = tuple(seasons[:index - 1])
        valid_season = seasons[index - 1]
        test_season = seasons[index]
        train = np.isin(season_ids, train_seasons)
        valid = season_ids == valid_season
        test = season_ids == test_season
        if not train.any() or not valid.any() or not test.any():
            raise EvaluationError("分割の一方が空になった")
        predict, best = learn(
            data.features[train], y[train], w[train],
            data.features[valid], y[valid],
        )
        results.append(Fold(
            test_season=test_season,
            train_seasons=train_seasons,
            valid_season=valid_season,
            n_train=int(train.sum()),
            n_valid=int(valid.sum()),
            n_test=int(test.sum()),
            best_iteration=best,
            probs=np.asarray(predict(data.features[test]), dtype=np.float64),
            actual=actual[test],
        ))
    return Evaluation(folds=tuple(results))
