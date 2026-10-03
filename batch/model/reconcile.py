"""整合化（要件 6.8.5 / 詳細設計 2.4）。

**選手ごとに独立予測した値をそのまま表示してはならない。** 5人の得点合計がチームの
予想スコアと一致せず、同じ画面に矛盾した数字が並ぶ。

前段（チーム目標の整合化）と、選手側の整合化の両方を持つ。

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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import brentq

type Floats = NDArray[np.float64]

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


# --- 選手側の整合化（要件 6.8.5 / 詳細設計 2.4） ---
#
# **選手ごとに独立予測した値をそのまま表示してはならない。** 5人の得点合計がチームの
# 予想スコアと一致せず、同じ画面に矛盾した数字が並ぶ。
#
# 手順は6つ。**4〜5 の反復は1回でよい**（2回目以降は何も変えない。`ITERATIONS`）。
#
#   1. 期待出場時間 `m = P(出場) × E[MIN | 出場]`
#   2. 総出場時間を正規化（5人 × 40分 = 200分。延長は +25分/回）
#   3. レート × 出場時間 → カウント
#   4. 試投数・その他カウントをチーム目標へ比例スケール
#   5. 成功率をチーム目標へ**ロジット空間シフト**
#   6. 得点・成功数は恒等式で導出（独立に持たない）

#: 反復回数（詳細設計 2.4）。**1回で厳密に合う。**
#:
#: 手順4で各項目の合計を目標に一致させ、手順5は**試投数を触らずに**成功率だけを
#: 動かす。したがって2回目の手順4は倍率1、手順5はシフト0になり、結果は完全に同一である
#: （`test_iterations_beyond_the_first_change_nothing` が固定する）。
#:
#: **旧版は 3 だった。** 根拠の実測表は、検証スクリプトが `pf` を 5.0 でクリップした
#: 状態で測ったものだった — 設計自身が禁じている処理である。外すと全反復で誤差 0 に
#: なる（2026-10-03。運営者の承認を得て訂正。`verification/RESULTS.md`）。
ITERATIONS = 1

#: 1チームの総出場時間（5人 × 40分）
TEAM_MINUTES = 200.0

#: 延長1回あたりの追加分。**予測時点で延長を仮定しない**（既定は 0 回）
OVERTIME_MINUTES = 25.0


@dataclass(frozen=True)
class PlayerRates:
    """1人ぶんの予測。整合化の入力。

    **シュートは「試投数のレート + 成功率」で持つ。** 成功数を独立に持つと、
    整合化の段階で `成功数 > 試投数` が生じ、それを防ぐクリップが収束を壊す
    （実測で失敗ケースの73%が FT 成功率）。
    """

    player_id: str
    avail_prob: float
    #: 出場する場合の出場時間（分）
    minutes_if_plays: float
    #: 単位時間あたりのレート。`ATTEMPTS` と `COUNTS` の11項目
    rate: Mapping[str, float]
    #: 成功率3項目。レートではなくそのまま使う
    pct: Mapping[str, float]


@dataclass(frozen=True)
class Reconciled:
    """整合化の結果。**得点と成功数は恒等式で導出した値**であり、独立に持たない。

    詳細設計 2.4 の擬似コードは `(m, x, pct, pts)` のタプルを返すが、
    中身は同じである（呼び出し側が位置で取り違えないよう名前を付けた）。
    """

    player_ids: tuple[str, ...]
    minutes: Floats
    #: 試投数とカウント（11項目）
    counts: Mapping[str, Floats]
    #: 成功率（3項目）
    pcts: Mapping[str, Floats]
    points: Floats

    def made(self, pct: str, attempt: str) -> Floats:
        """成功数 = 率 × 試投数。**構造的に `成功数 ≤ 試投数` が成立する。**"""
        return self.pcts[pct] * self.counts[attempt]


def shift_to_target(pct: Floats, att: Floats, target_made: float) -> Floats:
    """成功率をロジット空間で一律シフトし `Σ(pct × att) = target_made` を満たす。

    **クリップを使わない。** ロジットは `(0,1)` を `(-∞,∞)` に写すため、どれだけ
    シフトしても `[0,1]` を出ない。`f(d)` は `d` について単調増加なので解は一意。

    **試投数が 0 の項目は触らない**（割る相手がない）。
    """
    attempts = float(np.asarray(att, dtype=np.float64).sum())
    if attempts <= 0:
        return np.asarray(pct, dtype=np.float64)
    base = np.log(
        np.clip(np.asarray(pct, dtype=np.float64), EPS, 1.0 - EPS)
        / (1.0 - np.clip(np.asarray(pct, dtype=np.float64), EPS, 1.0 - EPS)),
    )
    weights = np.asarray(att, dtype=np.float64)

    def made(shift: float) -> float:
        return float((_sigmoid_array(base + shift) * weights).sum()) - target_made

    if made(-SHIFT_BOUND) > 0 or made(SHIFT_BOUND) < 0:
        raise InfeasibleTargetError(target_made, attempts)
    solved = float(brentq(made, -SHIFT_BOUND, SHIFT_BOUND))
    return _sigmoid_array(base + solved)


def _sigmoid_array(z: Floats) -> Floats:
    """**桁溢れしない書き方にする。** シフト量は ±50 まで動く。"""
    out = np.empty_like(z, dtype=np.float64)
    positive = z >= 0
    out[positive] = 1.0 / (1.0 + np.exp(-z[positive]))
    exp_z = np.exp(z[~positive])
    out[~positive] = exp_z / (1.0 + exp_z)
    return out


def reconcile(
    players: Sequence[PlayerRates], target: Mapping[str, float],
    *, overtime: int = 0, iterations: int = ITERATIONS,
) -> Reconciled:
    """選手予測をチーム目標に整合させる（詳細設計 2.4）。

    `target` は `reconcile_team_targets()` を通した**後**の値である。生の TeamRates を
    渡すと「選手の合計＝チーム目標」は満たされるが「チーム目標＝予想スコア」が崩れる。

    **延長を仮定しない。** `overtime` の既定は 0 回。
    """
    if not players:
        raise ValueError("選手が1人もいない")

    minutes = np.array(
        [float(p.avail_prob) * float(p.minutes_if_plays) for p in players],
        dtype=np.float64,
    )
    total_minutes = float(minutes.sum())
    if total_minutes <= 0:
        raise ValueError("期待出場時間の合計が 0（全員が欠場）")
    minutes = minutes * (TEAM_MINUTES + OVERTIME_MINUTES * overtime) / total_minutes

    counts: dict[str, Floats] = {
        stat: np.array([float(p.rate[stat]) for p in players], dtype=np.float64) * minutes
        for stat in (*ATTEMPTS, *COUNTS)
    }
    pcts: dict[str, Floats] = {
        pct: np.clip(
            np.array([float(p.pct[pct]) for p in players], dtype=np.float64),
            EPS, 1.0 - EPS,
        )
        for pct, _ in PCTS
    }

    for _ in range(iterations):
        for stat in (*ATTEMPTS, *COUNTS):
            current = float(counts[stat].sum())
            if current > 0:
                counts[stat] = counts[stat] * float(target[stat]) / current
        for pct, attempt in PCTS:
            # チームの成功数 = チームの率 × チームの試投数
            target_made = float(target[pct]) * float(target[attempt])
            pcts[pct] = shift_to_target(pcts[pct], counts[attempt], target_made)

    made = {pct: pcts[pct] * counts[attempt] for pct, attempt in PCTS}
    points = (
        made["fg2_pct"] * 2.0 + made["fg3_pct"] * 3.0 + made["ft_pct"]
    )
    return Reconciled(
        player_ids=tuple(p.player_id for p in players),
        minutes=minutes, counts=counts, pcts=pcts, points=points,
    )
