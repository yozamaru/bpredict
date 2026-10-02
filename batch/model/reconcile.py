"""整合化（要件 6.8.5 / 詳細設計 2.4）。

**選手ごとに独立予測した値をそのまま表示してはならない。** 5人の得点合計がチームの
予想スコアと一致せず、同じ画面に矛盾した数字が並ぶ。

この版では**前段（チーム目標の整合化）だけ**を持つ。選手側の整合化は工程12c。

## 前段がなぜ必要か

TeamRates（試投数・成功率の回帰）と Margin / Total は**別のモデル**であり、
TeamRates から導出した得点が予想スコアと一致する保証がない。一致しないまま選手側へ
渡すと「選手の合計＝チーム目標」は満たされるが「チーム目標＝画面に出る予想スコア」が
崩れ、**矛盾の位置が一段ずれるだけ**になる。

## 予想スコアを正とし、成功率だけを動かす

    pts(d) = 2·σ(l₂ + d)·A₂ + 3·σ(l₃ + d)·A₃ + 1·σ(l_f + d)·A_f

`σ` は単調増加、係数（2 / 3 / 1）と試投数 `A` は非負であるため `pts(d)` は `d` について
**単調増加**であり、`pts(d) = S` の解は一意に存在する。**反復は不要**（1回で厳密一致）。

| 守ること | 理由 |
|---|---|
| **試投数を動かさない** | ペースと配分の予測をそのまま残し、得点の帳尻は成功率だけで合わせる |
| **クリップを使わない** | ロジット空間で動かすため `[0,1]` を原理的に出ない。クリップは収束を壊す（実測で失敗ケースの73%が FT 成功率） |
| **カウント8項目を触らない** | 得点の恒等式に関与しない |
| **到達不能なら例外を投げる** | 無言でクリップして辻褄を合わせない。当該試合の個人スタッツを破棄する |
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
from scipy.optimize import brentq

#: 試投数（カウント）。**整合化で動かさない**
ATTEMPTS: tuple[str, ...] = ("fg2a", "fg3a", "fta")

#: 得点の恒等式に関与しないカウント。前段では触らない
COUNTS: tuple[str, ...] = ("oreb", "dreb", "ast", "tov", "stl", "blk", "pf", "fd")

#: (成功率, 対応する試投数)。**この3つだけを動かす**
PCTS: tuple[tuple[str, str], ...] = (
    ("fg2_pct", "fg2a"),
    ("fg3_pct", "fg3a"),
    ("ft_pct", "fta"),
)

#: 得点の重み。2点・3点・フリースロー1点
POINT_WEIGHT: Mapping[str, float] = {"fg2a": 2.0, "fg3a": 3.0, "fta": 1.0}

#: ロジット変換の定義域を確保するためのクリップ（詳細設計 2.4）。
#: **これは成功率の値域を守るためのものではない** — 整合化そのものは
#: ロジット空間で動かすためクリップを必要としない
EPS = 1e-4

#: シフト量の探索範囲（詳細設計 2.4）。σ の値域の端まで届く
SHIFT_BOUND = 50.0


class InfeasibleTargetError(ValueError):
    """到達不能な目標。**整合化の失敗ではなく上流の欠陥である**（詳細設計 2.4）。

    当該試合の個人スタッツ予測を破棄し、チーム予測（勝率・予想スコア）のみを
    保存する。画面には「この試合の個人スタッツ予測は算出できませんでした」と出す。
    **無言でクリップして辻褄を合わせない。**
    """

    def __init__(self, target: float, reachable: float) -> None:
        super().__init__(
            f"目標 {target:.2f} は到達不能（最大 {reachable:.2f}）",
        )
        self.target = target
        self.reachable = reachable


def logit(p: float) -> float:
    value = min(max(float(p), EPS), 1.0 - EPS)
    return float(np.log(value / (1.0 - value)))


def sigmoid(z: float) -> float:
    """**桁溢れしない書き方にする。** `d` は ±50 まで動くため `exp(50)` が出る。"""
    if z >= 0:
        return float(1.0 / (1.0 + np.exp(-z)))
    exp_z = float(np.exp(z))
    return exp_z / (1.0 + exp_z)


def reconcile_team_targets(
    rates: Mapping[str, float], pred_score: float,
) -> dict[str, float]:
    """TeamRates の出力を予想スコア（Margin / Total 由来）に整合させる。

    `rates` は14項目（試投数3 + 成功率3 + カウント8）。返すのは同じキーの辞書で、
    **成功率3項目だけが変わる。**

    ホーム・アウェイそれぞれについて独立に呼ぶ（`pred_score` は
    `(total + margin) / 2` と `(total - margin) / 2`）。
    """
    missing = sorted(
        key for key in (*ATTEMPTS, *COUNTS, *(p for p, _ in PCTS))
        if key not in rates
    )
    if missing:
        raise ValueError(f"TeamRates の出力に項目が足りない: {missing}")

    attempts = {name: float(rates[name]) for name in ATTEMPTS}
    if any(value < 0 for value in attempts.values()):
        raise ValueError("試投数が負である（TeamRates の出力が不正）")

    base = {pct: logit(rates[pct]) for pct, _ in PCTS}

    def points(shift: float) -> float:
        return sum(
            POINT_WEIGHT[att] * sigmoid(base[pct] + shift) * attempts[att]
            for pct, att in PCTS
        )

    # 値域は (0, 2·A₂ + 3·A₃ + A_f)。この外の目標は到達不能
    reachable = sum(POINT_WEIGHT[att] * attempts[att] for att in ATTEMPTS)
    target = float(pred_score)
    if points(-SHIFT_BOUND) > target or points(SHIFT_BOUND) < target:
        raise InfeasibleTargetError(target, reachable)

    shift = float(brentq(lambda x: points(x) - target, -SHIFT_BOUND, SHIFT_BOUND))

    out = {key: float(value) for key, value in rates.items()}
    for pct, _ in PCTS:
        # **クリップしない。** ロジット空間で動かすため [0,1] を出ない
        out[pct] = sigmoid(base[pct] + shift)
    return out


def derived_points(targets: Mapping[str, float]) -> float:
    """`2·2FGM + 3·3FGM + FTM`。**得点を独立に持たず、恒等式で導出する。**"""
    return sum(
        POINT_WEIGHT[att] * float(targets[pct]) * float(targets[att])
        for pct, att in PCTS
    )
