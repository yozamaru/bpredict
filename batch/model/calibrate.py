"""確率の較正（要件 6.7 / 詳細設計 2.3.1）。

**Platt scaling（パラメータ2個）を第一候補とする**（要件 6.7）。Isotonic は
非パラメトリックで自由度が高く、n=800 程度の較正セットでは裾のブロックが
数サンプルになり悪化しうる。

**logit 空間の1次変換にする。**

    補正後 = sigmoid(a × logit(p) + b)

`p` そのものの1次変換（`a·p + b`）は `[0,1]` を出てクリップが必要になる。
**整合化がクリップを禁じているのと同じ理由**で避ける（2.4）— logit 空間なら
`a` と `b` が何であれ**数学的には** `(0,1)` を出ない。

**float64 では厳密に成り立たない。** `|a × logit(p) + b|` が 37 を超えると
`sigmoid` が 0.0 / 1.0 に丸まる（倍精度の分解能の限界）。実測の係数は
`a ≈ 0.97〜0.99` / `b ≈ -0.24〜-0.04` で、`logit` の定義域が `EPS` で
抑えられているため `|z| < 10` に収まり、この限界には届かない。
**出力はこの後 `clamp_avail_prob` で `[0.01, 0.99]` に収まる**ため、
ここでクリップを足さない（機構を2つ持たない）。

**当てはめは fold の検証セットで行う**（2.3.1）。学習データそのもので fit すると、
学習データ上の過剰な分離を「正しい」と学習し、本番では過信が増幅される
（要件 6.7）。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

type Floats = NDArray[np.float64]

#: logit の定義域を確保する（`[0,1]` の端を避ける）。整合化と同じ値（2.4 の `EPS`）
EPS = 1e-4

#: 当てはめの反復上限と収束条件。**乱数を使わないモデルでも収束条件を記録する**
#: （4.7 と同じ理由 — 固定しないと当てはめが実行ごとに微妙に動く）
MAX_ITERATIONS = 200
TOLERANCE = 1e-8


class CalibrationError(ValueError):
    """較正器を当てはめられない入力。"""


@dataclass(frozen=True)
class Platt:
    """`sigmoid(a × logit(p) + b)`。**2個しか持たない。**"""

    a: float
    b: float

    def apply(self, probs: Floats) -> Floats:
        return _sigmoid(self.a * _logit(probs) + self.b)


#: 何も変えない較正器。**「入れない」を明示的に表せるようにする**
IDENTITY = Platt(a=1.0, b=0.0)


def _logit(probs: Floats) -> Floats:
    clipped = np.clip(np.asarray(probs, dtype=np.float64), EPS, 1.0 - EPS)
    return np.log(clipped / (1.0 - clipped))


def _sigmoid(z: Floats) -> Floats:
    """桁溢れしない書き方にする（2.4 の `sigmoid` と同じ理由）。"""
    out = np.empty_like(z, dtype=np.float64)
    positive = z >= 0
    out[positive] = 1.0 / (1.0 + np.exp(-z[positive]))
    exp_z = np.exp(z[~positive])
    out[~positive] = exp_z / (1.0 + exp_z)
    return out


def fit_platt(probs: Floats, actual: Floats) -> Platt:
    """log loss を最小にする `(a, b)` を解く（Platt の原法）。

    **実測値が 0/1 でなければ落とす。** 回帰の出力に較正器を当てるのは
    呼び出し側の誤りであり、黙って意味のない値を返さない（`Evaluation` が
    `brier` を回帰に対して拒むのと同じ）。

    **Platt の平滑化（目的値を 0/1 から少し内側へ寄せる）を入れる。** 入れないと
    完全に分離できる fold で `a` が発散する — 実装は原法のとおり、正例を
    `(n+1)/(n+2)`、負例を `1/(m+2)` にする。
    """
    p = np.asarray(probs, dtype=np.float64)
    y = np.asarray(actual, dtype=np.float64)
    if p.ndim != 1 or p.shape != y.shape or p.size == 0:
        raise CalibrationError("確率と実測の形が合わない")
    if not np.isfinite(p).all() or ((p < 0) | (p > 1)).any():
        raise CalibrationError("確率が [0,1] に収まっていない")
    if not np.isin(y, (0.0, 1.0)).all():
        raise CalibrationError("実測が 0/1 でない")

    positives = float(y.sum())
    negatives = float(y.size - positives)
    if positives == 0 or negatives == 0:
        # 片方しか無い fold では傾きが決まらない。**何も変えない較正器を返す**
        return IDENTITY

    high = (positives + 1.0) / (positives + 2.0)
    low = 1.0 / (negatives + 2.0)
    target = np.where(y > 0.5, high, low)
    f = _logit(p)

    from scipy.optimize import minimize

    def loss(params: Floats) -> float:
        z = params[0] * f + params[1]
        # log(1 + exp(-z)) を桁溢れせずに書く
        softplus = np.logaddexp(0.0, -z)
        return float((target * softplus + (1.0 - target) * (softplus + z)).sum())

    result = minimize(
        loss,
        x0=np.array([1.0, 0.0]),
        method="L-BFGS-B",
        options={"maxiter": MAX_ITERATIONS, "ftol": TOLERANCE},
    )
    if not np.isfinite(result.x).all():
        return IDENTITY
    return Platt(a=float(result.x[0]), b=float(result.x[1]))


def dump(model: Platt) -> str:
    """`model_versions.calibrator` に入れる形（要件 6.7）。"""
    return json.dumps({"kind": "platt", "version": 1, "a": model.a, "b": model.b},
                      ensure_ascii=False, separators=(",", ":"))


def load(payload: str) -> Platt:
    """**種類と版を照合する。** 黙って別の較正器として読まない。"""
    try:
        parsed = json.loads(payload)
    except ValueError:
        raise CalibrationError("較正器を JSON として読めない") from None
    if not isinstance(parsed, dict) or parsed.get("kind") != "platt":
        raise CalibrationError("較正器の種類が platt でない")
    if parsed.get("version") != 1:
        raise CalibrationError("較正器の版が一致しない")
    a, b = parsed.get("a"), parsed.get("b")
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        raise CalibrationError("較正器の係数が数値でない")
    if not math.isfinite(a) or not math.isfinite(b):
        raise CalibrationError("較正器の係数が有限でない")
    return Platt(a=float(a), b=float(b))
