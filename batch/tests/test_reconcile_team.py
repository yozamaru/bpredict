"""チーム目標の整合化（`batch/model/reconcile.py`。要件 6.8.5 前段 / 詳細設計 2.4）。

受け入れ基準 A-16 に対応する。

| テスト | A-16 のどの条件か |
|---|---|
| `test_team_targets_are_feasible` | `成功数 ≤ 試投数`、恒等式 |
| `test_team_targets_match_predicted_score` | 導出得点が予想スコアと ±0.5点以内 |
| `test_team_reconcile_raises_on_infeasible` | 到達不能なら `InfeasibleTargetError` |
"""
from __future__ import annotations

import numpy as np
import pytest

from batch.model.reconcile import (
    ATTEMPTS,
    COUNTS,
    PCTS,
    InfeasibleTargetError,
    derived_points,
    reconcile_team_targets,
)


def rates(**overrides: float) -> dict[str, float]:
    """TeamRates の出力の形（14項目）。実データの水準に近い値を既定にする。"""
    base = {
        "fg2a": 43.0, "fg3a": 24.0, "fta": 18.0,
        "fg2_pct": 0.52, "fg3_pct": 0.34, "ft_pct": 0.75,
        "oreb": 10.0, "dreb": 26.0, "ast": 19.0, "tov": 13.0,
        "stl": 6.0, "blk": 2.5, "pf": 18.0, "fd": 18.0,
    }
    base.update(overrides)
    return base


def random_rates(rng: np.random.Generator) -> dict[str, float]:
    return rates(
        fg2a=float(rng.uniform(30, 55)),
        fg3a=float(rng.uniform(12, 38)),
        fta=float(rng.uniform(8, 32)),
        fg2_pct=float(rng.uniform(0.38, 0.64)),
        fg3_pct=float(rng.uniform(0.24, 0.45)),
        ft_pct=float(rng.uniform(0.60, 0.88)),
    )


# --- A-16: 予想スコアと一致する ---

@pytest.mark.parametrize("score", [62.0, 72.5, 80.0, 84.0, 95.0, 110.0])
def test_team_targets_match_predicted_score(score: float) -> None:
    """**導出得点が予想スコアと一致すること。** 反復なしで厳密に合う。

    許容は A-16 の ±0.5点だが、1回の `brentq` で解くため実際には桁落ち程度しか
    ずれない。**緩い許容で通しても、厳しい許容で確かめる。**
    """
    out = reconcile_team_targets(rates(), score)
    assert derived_points(out) == pytest.approx(score, abs=1e-6)
    assert abs(derived_points(out) - score) < 0.5


def test_team_targets_match_on_many_random_inputs() -> None:
    """無作為な入力でも一致すること（1,000件）。"""
    rng = np.random.default_rng(42)
    worst = 0.0
    for _ in range(1000):
        source = random_rates(rng)
        reachable = sum(
            w * source[a] for a, w in (("fg2a", 2.0), ("fg3a", 3.0), ("fta", 1.0))
        )
        score = float(rng.uniform(0.25, 0.75)) * reachable
        out = reconcile_team_targets(source, score)
        worst = max(worst, abs(derived_points(out) - score))
    assert worst < 1e-6, f"最大の得点誤差 {worst}"


# --- A-16: 制約と恒等式 ---

def test_team_targets_are_feasible() -> None:
    """**`成功数 ≤ 試投数` と成功率の値域。** 「率 × 試投数」の構造から自動で成立する。"""
    rng = np.random.default_rng(7)
    for _ in range(500):
        source = random_rates(rng)
        reachable = sum(
            w * source[a] for a, w in (("fg2a", 2.0), ("fg3a", 3.0), ("fta", 1.0))
        )
        out = reconcile_team_targets(source, float(rng.uniform(0.2, 0.8)) * reachable)
        for attempt in ATTEMPTS:
            assert out[attempt] >= 0
        for pct, attempt in PCTS:
            assert 0.0 <= out[pct] <= 1.0
            assert out[pct] * out[attempt] <= out[attempt] + 1e-12


def test_team_targets_never_leave_the_unit_interval() -> None:
    """**クリップを使っていないこと。** 極端な目標でも成功率が `[0,1]` を出ない。

    ロジット空間で動かすため原理的に出ない。クリップに頼っていたら、
    端に張り付いた時点で得点を合わせられなくなる。
    """
    source = rates()
    reachable = 2 * source["fg2a"] + 3 * source["fg3a"] + source["fta"]
    for fraction in (0.001, 0.01, 0.5, 0.99, 0.999):
        out = reconcile_team_targets(source, fraction * reachable)
        for pct, _ in PCTS:
            assert 0.0 < out[pct] < 1.0


# --- 動かしてよいもの / いけないもの ---

def test_team_targets_attempts_unchanged_by_reconcile() -> None:
    """**試投数を動かさない。** ペースと配分の予測をそのまま残す（詳細設計 2.4）。"""
    source = rates()
    out = reconcile_team_targets(source, 95.0)
    for attempt in ATTEMPTS:
        assert out[attempt] == source[attempt]


def test_team_targets_counts_unchanged_by_reconcile() -> None:
    """**カウント8項目も触らない。** 得点の恒等式に関与しない。"""
    source = rates()
    out = reconcile_team_targets(source, 95.0)
    for count in COUNTS:
        assert out[count] == source[count]


def test_team_targets_shift_all_percentages_in_the_same_direction() -> None:
    """**共通のシフト量を1つだけ入れる。** 項目ごとに別の倍率を掛けない。

    得点を上げるなら3項目すべてが上がる。片方だけ動かすと、ペースと配分の
    予測を壊したことになる。
    """
    source = rates()
    low = reconcile_team_targets(source, 70.0)
    high = reconcile_team_targets(source, 100.0)
    for pct, _ in PCTS:
        assert low[pct] < source[pct] or high[pct] > source[pct]
        assert low[pct] < high[pct]


# --- 到達不能 ---

def test_team_reconcile_raises_on_infeasible() -> None:
    """**無言でクリップしない**（詳細設計 2.4）。

    値域は `(0, 2·A₂ + 3·A₃ + A_f)`。この外の目標は到達不能である。
    """
    source = rates()
    reachable = 2 * source["fg2a"] + 3 * source["fg3a"] + source["fta"]
    for impossible in (reachable + 1.0, reachable * 2, -1.0):
        with pytest.raises(InfeasibleTargetError):
            reconcile_team_targets(source, impossible)


def test_infeasible_error_carries_the_numbers() -> None:
    """例外に目標と到達可能な上限を持たせる（調査できるようにする）。"""
    source = rates()
    reachable = 2 * source["fg2a"] + 3 * source["fg3a"] + source["fta"]
    with pytest.raises(InfeasibleTargetError) as caught:
        reconcile_team_targets(source, reachable + 10.0)
    assert caught.value.target == pytest.approx(reachable + 10.0)
    assert caught.value.reachable == pytest.approx(reachable)


def test_missing_items_are_rejected() -> None:
    """14項目すべてを要求する。**足りないまま黙って整合化しない。**"""
    source = rates()
    del source["oreb"]
    with pytest.raises(ValueError, match="足りない"):
        reconcile_team_targets(source, 84.0)


def test_negative_attempts_are_rejected() -> None:
    with pytest.raises(ValueError, match="負"):
        reconcile_team_targets(rates(fta=-1.0), 84.0)


# --- ホームとアウェイを独立に解く ---

def test_home_and_away_are_solved_independently() -> None:
    """`pred_score` は `(total ± margin) / 2`。**両側で別の解になる。**"""
    total, margin = 162.0, 8.0
    home = reconcile_team_targets(rates(), (total + margin) / 2)
    away = reconcile_team_targets(rates(), (total - margin) / 2)
    assert derived_points(home) == pytest.approx(85.0, abs=1e-6)
    assert derived_points(away) == pytest.approx(77.0, abs=1e-6)
    assert derived_points(home) + derived_points(away) == pytest.approx(total, abs=1e-6)
