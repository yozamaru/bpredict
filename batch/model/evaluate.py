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
        return brier(self.probs, self.actual)

    @property
    def accuracy(self) -> float:
        return accuracy(self.probs, self.actual)

    @property
    def log_loss(self) -> float:
        return log_loss(self.probs, self.actual)

    @property
    def ece(self) -> float | None:
        return ece(self.probs, self.actual)

    @property
    def best_iterations(self) -> list[int]:
        return [f.best_iteration for f in self.folds]


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
    weights: Floats | None = None, max_folds: int = MAX_FOLDS,
) -> Evaluation:
    seasons = data.seasons
    positions = folds_of(seasons, max_folds=max_folds)
    if not positions:
        raise EvaluationError(
            f"walk-forward には3シーズン以上が必要（いまは {len(seasons)}）",
        )
    season_ids = np.asarray(data.season_ids)
    w = np.ones(len(data)) if weights is None else np.asarray(weights, dtype=np.float64)
    if w.size != len(data):
        raise EvaluationError("重みの件数が学習行列と合わない")

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
            data.features[train], data.home_win[train], w[train],
            data.features[valid], data.home_win[valid],
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
            actual=data.home_win[test],
        ))
    return Evaluation(folds=tuple(results))
