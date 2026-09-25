"""3段のベースライン（要件 6.4）。

1. ホームチームが必ず勝つ（Accuracy の比較用）
2. **Elo差単体のロジスティック回帰**（GBDT の真の対抗馬）
3. 全特徴のロジスティック回帰

**2 を Brier で有意に上回れなければ、LightGBM を使う理由がない。**

**scikit-learn を入れない。** 必要なのは重み付きロジスティック回帰1本で、
IRLS（ニュートン法）で30行に収まる。CLAUDE.md「依存を追加するとき」の1番目
（標準ライブラリか数十行の自前実装で足りないか）に照らして自前で持つ。
LightGBM は GBDT であり自前実装の対象にならないため入れる。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

type Floats = NDArray[np.float64]

#: 予測が 0 / 1 に張り付くと log loss が無限になる。外挿域のクランプとは別物で、
#: これは数値計算のための下限である
_EPS = 1e-12


class FitError(ValueError):
    """当てはめができない入力。黙って定数を返さない。"""


def sigmoid(z: Floats) -> Floats:
    # オーバーフローを避けるため、正負で式を分ける
    out = np.empty_like(z)
    positive = z >= 0
    out[positive] = 1.0 / (1.0 + np.exp(-z[positive]))
    exp_z = np.exp(z[~positive])
    out[~positive] = exp_z / (1.0 + exp_z)
    return out


@dataclass(frozen=True)
class Logistic:
    """重み付きロジスティック回帰。切片つき。"""

    intercept: float
    coefficients: Floats

    def predict(self, features: Floats) -> Floats:
        matrix = np.asarray(features, dtype=np.float64)
        if matrix.ndim != 2 or matrix.shape[1] != self.coefficients.size:
            raise FitError("特徴量の列数が学習時と違う")
        return np.clip(
            sigmoid(self.intercept + matrix @ self.coefficients), _EPS, 1.0 - _EPS,
        )


def fit_logistic(
    features: Floats, actual: Floats, *,
    weights: Floats | None = None, l2: float = 1e-4,
    max_iterations: int = 100, tolerance: float = 1e-8,
) -> Logistic:
    """IRLS で当てはめる。**標準化してから解く。**

    Elo差は数百のオーダー、勝率差は 0〜1 のオーダーで、桁が3つ違う。標準化せずに
    解くとヘッセ行列の条件数が悪化し、`l2` が実質的に Elo差だけに効く。
    係数は元の尺度に戻して返すため、呼び出し側は標準化を知らなくてよい。

    `l2` は数値の安定のための小さなリッジである（完全分離のときに係数が発散する）。
    **正則化の強さを探索の対象にはしない** — ベースラインは「超えるべき下限」であり、
    ここを詰めても比較の意味が増えない。
    """
    X = np.asarray(features, dtype=np.float64)
    y = np.asarray(actual, dtype=np.float64)
    if X.ndim != 2:
        raise FitError("特徴量が2次元でない")
    if y.ndim != 1 or y.size != X.shape[0]:
        raise FitError("実績の件数が特徴量と合わない")
    if X.shape[0] == 0 or X.shape[1] == 0:
        raise FitError("標本または特徴量が空である")
    if not np.isfinite(X).all() or not np.isfinite(y).all():
        raise FitError("特徴量または実績に有限でない値がある")
    if not np.isin(y, (0.0, 1.0)).all():
        raise FitError("実績が 0 / 1 ではない")
    w = np.ones(y.size) if weights is None else np.asarray(weights, dtype=np.float64)
    if w.size != y.size or (w < 0).any() or not np.isfinite(w).all():
        raise FitError("重みが不正である")

    center = X.mean(axis=0)
    scale = X.std(axis=0)
    # 定数列（σ=0）は標準化できない。1で割って0のまま通す
    scale = np.where(scale > 0, scale, 1.0)
    Z = np.hstack([np.ones((X.shape[0], 1)), (X - center) / scale])

    beta = np.zeros(Z.shape[1])
    ridge = np.eye(Z.shape[1]) * l2
    ridge[0, 0] = 0.0  # 切片は正則化しない
    for _ in range(max_iterations):
        p = np.clip(sigmoid(Z @ beta), 1e-10, 1.0 - 1e-10)
        gradient = Z.T @ (w * (y - p)) - ridge @ beta
        hessian = (Z * (w * p * (1.0 - p))[:, None]).T @ Z + ridge
        try:
            step = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError:
            raise FitError("ヘッセ行列が特異である") from None
        beta = beta + step
        if np.max(np.abs(step)) < tolerance:
            break

    coefficients = beta[1:] / scale
    intercept = float(beta[0] - float(np.dot(beta[1:] / scale, center)))
    return Logistic(intercept=intercept, coefficients=coefficients)


def home_always(count: int) -> Floats:
    """1段目。ホームが必ず勝つ。

    **確率としては 1.0 ではなく 0.5 を返さない。** Accuracy の比較用であり、
    `p > 0.5` が常に真になる値を返す。Brier では当然不利になるが、それは
    このベースラインの性質であって調整の対象ではない。
    """
    if count <= 0:
        raise FitError("件数が0以下である")
    return np.full(count, 1.0 - _EPS)
