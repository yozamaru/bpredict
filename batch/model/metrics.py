"""評価指標（要件 6.4 / 詳細設計 4.6）。

**Accuracy は表示専用である。** 採用ゲート・モデル比較・アラートに使わない。
ホーム勝率60%の下では良い確率モデルでも Accuracy はベースラインとほぼ並ぶため、
Brier で明確に優れているモデルが誤って棄却される。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from batch.model.params import ECE_BINS, ECE_MIN_PER_BIN

type Floats = NDArray[np.float64]


class MetricError(ValueError):
    """指標を計算できない入力。黙って NaN を返さない。"""


def _checked(probs: Floats, actual: Floats) -> tuple[Floats, Floats]:
    p = np.asarray(probs, dtype=np.float64)
    y = np.asarray(actual, dtype=np.float64)
    if p.ndim != 1 or y.ndim != 1 or p.size != y.size:
        raise MetricError("確率と実績の形が合わない")
    if p.size == 0:
        raise MetricError("標本が空である")
    if not np.isfinite(p).all() or ((p < 0) | (p > 1)).any():
        raise MetricError("確率が [0,1] に収まっていない")
    if not np.isin(y, (0.0, 1.0)).all():
        raise MetricError("実績が 0 / 1 ではない")
    return p, y


def brier(probs: Floats, actual: Floats) -> float:
    """**主要な改善指標**（要件 6.4）。0に近いほど良い。"""
    p, y = _checked(probs, actual)
    return float(np.mean((p - y) ** 2))


def brier_per_game(probs: Floats, actual: Floats) -> Floats:
    """試合ごとの Brier。ブートストラップ信頼区間に使う（詳細設計 4.6）。"""
    p, y = _checked(probs, actual)
    return (p - y) ** 2


def log_loss(probs: Floats, actual: Floats, *, eps: float = 1e-15) -> float:
    """学習時の目的関数。0 / 1 の確率で無限大にならないよう eps でクリップする。"""
    p, y = _checked(probs, actual)
    q = np.clip(p, eps, 1.0 - eps)
    return float(-np.mean(y * np.log(q) + (1.0 - y) * np.log(1.0 - q)))


def accuracy(probs: Floats, actual: Floats) -> float:
    """**表示専用。** 採用ゲートに使わない（要件 6.4）。"""
    p, y = _checked(probs, actual)
    return float(np.mean((p > 0.5) == (y > 0.5)))


def ece(
    probs: Floats, actual: Floats, *,
    bins: int = ECE_BINS, min_per_bin: int = ECE_MIN_PER_BIN,
) -> float | None:
    """較正誤差。**等頻度10ビン・1ビン最低50件**（要件 6.4）。

    **1ビンが `min_per_bin` を満たせないときは None を返す。** 0 を返すと
    「較正が完璧」と誤読される。要件は `n < 500` では ECE をゲートに使わないと
    定めており、その判断材料をここで返す。
    """
    p, y = _checked(probs, actual)
    if bins < 1:
        raise MetricError("ビン数が1未満である")
    if p.size < bins * min_per_bin:
        return None
    order = np.argsort(p, kind="stable")
    p, y = p[order], y[order]
    total = p.size
    value = 0.0
    for part in np.array_split(np.arange(total), bins):
        if part.size == 0:
            continue
        value += part.size / total * abs(float(y[part].mean()) - float(p[part].mean()))
    return value


@dataclass(frozen=True)
class Difference:
    """2モデルの Brier 差。`base - challenger` が正なら挑戦側が良い。"""

    point: float
    ci_low: float
    ci_high: float

    @property
    def significant(self) -> bool:
        """信頼区間が0を跨がないこと（詳細設計 4.6）。"""
        return self.ci_low > 0.0 or self.ci_high < 0.0


def brier_difference(
    base_probs: Floats, challenger_probs: Floats, actual: Floats, *,
    resamples: int = 2000, seed: int = 42, level: float = 0.95,
) -> Difference:
    """試合ごとの Brier 差をブートストラップする（詳細設計 4.6）。

    **試合を単位に再標本化する。** 指標そのものを再計算しないと、対応のある差
    （同じ試合に対する2つの予測の差）という構造が失われる。
    """
    base = brier_per_game(base_probs, actual)
    challenger = brier_per_game(challenger_probs, actual)
    if base.size != challenger.size:
        raise MetricError("2つの予測の件数が合わない")
    diff = base - challenger
    rng = np.random.default_rng(seed)
    n = diff.size
    draws = rng.integers(0, n, size=(resamples, n))
    means = diff[draws].mean(axis=1)
    tail = (1.0 - level) / 2.0
    return Difference(
        point=float(diff.mean()),
        ci_low=float(np.quantile(means, tail)),
        ci_high=float(np.quantile(means, 1.0 - tail)),
    )


def ece_noise_floor(
    probs: Floats, *, quantile: float = 0.95, resamples: int = 1000, seed: int = 42,
    bins: int = ECE_BINS, min_per_bin: int = ECE_MIN_PER_BIN,
) -> float | None:
    """**そのモデルの予測分布**で、完全に較正されていても出る ECE の水準。

    固定閾値 0.05 を使わない理由（要件 6.4 の実測）。完全較正モデルでも ECE は
    サンプリングノイズだけで有限の値を取り、その大きさは n に強く依存する
    （95%点は n=200 で 0.1163、n=1,200 で 0.0479）。固定閾値は小標本では良い
    モデルを落とし、大標本では素通りする。

    **一様分布での参考値を定数表にしない。** 予測確率 `probs` をそのまま真の確率と
    みなして `y ~ Bernoulli(p)` を引き、そこから出る ECE の分布を測る。
    実装は `verification/01_ece_noise_floor.py` と同じ考え方である。
    """
    p = np.asarray(probs, dtype=np.float64)
    if p.ndim != 1 or p.size == 0:
        raise MetricError("確率の形が不正である")
    if not np.isfinite(p).all() or ((p < 0) | (p > 1)).any():
        raise MetricError("確率が [0,1] に収まっていない")
    if p.size < bins * min_per_bin:
        return None
    rng = np.random.default_rng(seed)
    values = np.empty(resamples, dtype=np.float64)
    for index in range(resamples):
        drawn = (rng.random(p.size) < p).astype(np.float64)
        measured = ece(p, drawn, bins=bins, min_per_bin=min_per_bin)
        # 件数は変えていないので None にはならない
        values[index] = 0.0 if measured is None else measured
    return float(np.quantile(values, quantile))
