"""選手側の整合化（`batch/model/reconcile.py`。要件 6.8.5 / 詳細設計 2.4）。

受け入れ基準 A-13 / A-15 に対応する。

**設計の実測表（2.4）は古い。** あの表は `pf` を 5.0 でクリップした状態で測ったもので、
同じ 2.4 が後に「**`pf` の 5.0 クリップも行わない**」と定めている。クリップを外すと
**項目誤差は反復1回で厳密に 0** になる（`verification/RESULTS.md`）。
`test_iterations_beyond_the_first_change_nothing` がこれを固定する。
"""
from __future__ import annotations

import numpy as np
import pytest

from batch.model.reconcile import (
    ATTEMPTS,
    COUNTS,
    PCTS,
    TEAM_MINUTES,
    InfeasibleTargetError,
    PlayerRates,
    Reconciled,
    derived_points,
    reconcile,
    reconcile_team_targets,
    shift_to_target,
)

RATE_RANGE = {
    "fg2a": (0.05, 0.35), "fg3a": (0.0, 0.25), "fta": (0.0, 0.20),
    "oreb": (0.0, 0.12), "dreb": (0.05, 0.25), "ast": (0.0, 0.25),
    "tov": (0.02, 0.15), "stl": (0.0, 0.08), "blk": (0.0, 0.06),
    "pf": (0.03, 0.15), "fd": (0.02, 0.18),
}


def player(rng: np.random.Generator, index: int) -> PlayerRates:
    return PlayerRates(
        player_id=f"p{index}",
        avail_prob=float(rng.uniform(0.5, 1.0)),
        minutes_if_plays=float(rng.uniform(8, 34)),
        rate={k: float(rng.uniform(*v)) for k, v in RATE_RANGE.items()},
        pct={
            "fg2_pct": float(rng.uniform(0.30, 0.70)),
            "fg3_pct": float(rng.uniform(0.15, 0.50)),
            "ft_pct": float(rng.uniform(0.45, 0.95)),
        },
    )


def team_target(rng: np.random.Generator) -> tuple[dict[str, float], float]:
    """`reconcile_team_targets()` を通した目標と、その予想スコア。"""
    raw = {
        "fg2a": float(rng.uniform(30, 55)), "fg3a": float(rng.uniform(12, 38)),
        "fta": float(rng.uniform(8, 32)),
        "fg2_pct": float(rng.uniform(0.40, 0.62)),
        "fg3_pct": float(rng.uniform(0.26, 0.44)),
        "ft_pct": float(rng.uniform(0.62, 0.86)),
        "oreb": float(rng.uniform(5, 15)), "dreb": float(rng.uniform(20, 34)),
        "ast": float(rng.uniform(12, 28)), "tov": float(rng.uniform(8, 20)),
        "stl": float(rng.uniform(3, 11)), "blk": float(rng.uniform(1, 6)),
        "pf": float(rng.uniform(12, 24)), "fd": float(rng.uniform(12, 24)),
    }
    reachable = 2 * raw["fg2a"] + 3 * raw["fg3a"] + raw["fta"]
    score = min(float(rng.uniform(62, 105)), reachable * 0.95)
    return reconcile_team_targets(raw, score), score


def case(seed: int, size: int = 12) -> tuple[list[PlayerRates], dict[str, float], float]:
    rng = np.random.default_rng(seed)
    players = [player(rng, i) for i in range(size)]
    target, score = team_target(rng)
    return players, target, score


def fixed() -> tuple[list[PlayerRates], dict[str, float], float]:
    return case(1)


# --- A-13: チーム予測と一致する ---

def test_minutes_sum_to_200() -> None:
    """**1チームあたり 200分**（5人 × 40分）。両チームを足して 400分にしない。"""
    players, target, _ = fixed()
    out = reconcile(players, target)
    assert float(out.minutes.sum()) == pytest.approx(TEAM_MINUTES, abs=1e-9)


def test_counts_sum_to_the_team_target() -> None:
    """11項目の合計がチーム目標に一致すること（A-13 の許容は ±2%）。"""
    players, target, _ = fixed()
    out = reconcile(players, target)
    for stat in (*ATTEMPTS, *COUNTS):
        assert float(out.counts[stat].sum()) == pytest.approx(target[stat], rel=1e-9)


def test_points_sum_to_the_predicted_score() -> None:
    """得点の合計が予想スコアに一致すること（A-13 の許容は ±0.5点）。"""
    players, target, score = fixed()
    out = reconcile(players, target)
    assert float(out.points.sum()) == pytest.approx(score, abs=1e-6)
    # チーム目標から導いた得点とも一致する
    assert float(out.points.sum()) == pytest.approx(derived_points(target), abs=1e-6)


def test_made_counts_sum_to_the_team_made() -> None:
    """成功数の合計がチームの成功数に一致すること（ロジットシフトの帰結）。"""
    players, target, _ = fixed()
    out = reconcile(players, target)
    for pct, attempt in PCTS:
        want = target[pct] * target[attempt]
        assert float(out.made(pct, attempt).sum()) == pytest.approx(want, rel=1e-9)


@pytest.mark.parametrize("seed", range(60))
def test_reconciliation_converges(seed: int) -> None:
    """無作為な入力で許容内に収まること（A-13）。

    **1,500件の通しは `verification/` で行う**（`RESULTS.md` に記録）。ここでは
    CI の時間に収まる件数で境界を守る。
    """
    players, target, score = case(seed)
    out = reconcile(players, target)
    assert abs(float(out.points.sum()) - score) < 0.5
    for stat in (*ATTEMPTS, *COUNTS):
        if target[stat] > 0:
            error = abs(float(out.counts[stat].sum()) - target[stat]) / target[stat]
            assert error < 0.02


# --- 反復回数（設計の表は古い） ---

def test_iterations_beyond_the_first_change_nothing() -> None:
    """**反復1回で厳密に合う。** 2回目以降は何も変えない。

    手順4で合計を目標に合わせ、手順5は**試投数を触らない**。したがって2回目の
    手順4は倍率1、手順5はシフト0になり、結果は完全に同じである。

    **設計（2.4）が反復3回と定める根拠は、`pf` を 5.0 でクリップした状態で測った
    古い表である。** 同じ 2.4 が後にそのクリップを禁じており、外すと誤差は 0 になる
    （`verification/RESULTS.md`）。実装は設計の指示どおり3回のままにしてあるが、
    **1回で足りることをここで固定する。**
    """
    players, target, _ = fixed()
    once = reconcile(players, target, iterations=1)
    thrice = reconcile(players, target, iterations=3)
    assert once.minutes == pytest.approx(thrice.minutes)
    assert once.points == pytest.approx(thrice.points)
    for stat in (*ATTEMPTS, *COUNTS):
        assert once.counts[stat] == pytest.approx(thrice.counts[stat])
    for pct, _ in PCTS:
        assert once.pcts[pct] == pytest.approx(thrice.pcts[pct])


# --- A-15: 恒等式と制約 ---

@pytest.mark.parametrize("seed", range(40))
def test_player_prediction_identities(seed: int) -> None:
    """`成功数 ≤ 試投数`、成功率の値域、得点の恒等式（A-15）。"""
    players, target, _ = case(seed)
    out = reconcile(players, target)
    for pct, attempt in PCTS:
        assert np.all(out.pcts[pct] >= 0.0)
        assert np.all(out.pcts[pct] <= 1.0)
        # 率 × 試投数 という持ち方から構造的に成立する
        assert np.all(out.made(pct, attempt) <= out.counts[attempt] + 1e-12)
    # 得点は恒等式で導出されている（独立に持たない）
    identity = (
        out.made("fg2_pct", "fg2a") * 2.0
        + out.made("fg3_pct", "fg3a") * 3.0
        + out.made("ft_pct", "fta")
    )
    assert out.points == pytest.approx(identity)
    for stat in (*ATTEMPTS, *COUNTS):
        assert np.all(out.counts[stat] >= 0.0)


def test_reconciliation_no_clipping() -> None:
    """**整合化後の成功率が `[0,1]` を出ないこと。** クリップに頼っていない確認。"""
    rng = np.random.default_rng(99)
    for _ in range(50):
        players = [player(rng, i) for i in range(10)]
        target, _ = team_target(rng)
        out = reconcile(players, target)
        for pct, _ in PCTS:
            assert np.all(out.pcts[pct] > 0.0)
            assert np.all(out.pcts[pct] < 1.0)


def test_fouls_are_not_clipped() -> None:
    """**`pf` を 5.0 でクリップしない**（詳細設計 2.4）。

    クリップすると合計がチーム目標からずれる。**設計の実測表の残差は、これが
    単独の原因だった**（`verification/RESULTS.md`）。

    **クリップが効く条件を実際に作る。** 無作為な入力では誰も 5.0 を超えず、
    この検査は空振りする（最初に書いた版は、クリップを戻す変異試験で落ちなかった）。
    出場時間を1人に寄せ、反則のレートを高くして `pf > 5` を確実に起こす。
    """
    rng = np.random.default_rng(5)
    rates = {k: float(rng.uniform(*v)) for k, v in RATE_RANGE.items()}
    heavy = PlayerRates(
        player_id="heavy", avail_prob=1.0, minutes_if_plays=38.0,
        rate={**rates, "pf": 0.18},
        pct={"fg2_pct": 0.5, "fg3_pct": 0.35, "ft_pct": 0.75},
    )
    rest = [
        PlayerRates(
            player_id=f"p{i}", avail_prob=1.0, minutes_if_plays=32.4,
            rate={**rates, "pf": 0.08},
            pct={"fg2_pct": 0.5, "fg3_pct": 0.35, "ft_pct": 0.75},
        )
        for i in range(5)
    ]
    players = [heavy, *rest]
    target, _ = team_target(rng)
    target = {**target, "pf": 24.0}

    out = reconcile(players, target)

    # **空振りしないことを確かめる。** 5.0 を超える選手がいなければ、この検査は
    # クリップの有無を区別できない
    assert float(out.counts["pf"].max()) > 5.0, "クリップが効く条件を作れていない"
    assert float(out.counts["pf"].sum()) == pytest.approx(target["pf"], rel=1e-9)


# --- 延長 ---

def test_overtime_minutes_not_assumed() -> None:
    """**予測時点で延長を仮定しない。** 既定は 200分。"""
    players, target, _ = fixed()
    assert float(reconcile(players, target).minutes.sum()) == pytest.approx(200.0)


def test_overtime_adds_25_minutes_each() -> None:
    """延長を渡したときは `200 + 25 × 回数`。"""
    players, target, _ = fixed()
    for overtime, want in ((1, 225.0), (2, 250.0)):
        out = reconcile(players, target, overtime=overtime)
        assert float(out.minutes.sum()) == pytest.approx(want)


# --- 到達不能 ---

def test_reconciliation_raises_on_infeasible_target() -> None:
    """**無言でクリップしない。** チーム目標が不正なら例外（詳細設計 2.4）。"""
    players, target, _ = fixed()
    broken = dict(target)
    # 成功率を 1 より大きくすると、成功数 > 試投数 の目標になる
    broken["ft_pct"] = 1.5
    with pytest.raises(InfeasibleTargetError):
        reconcile(players, broken)


def test_shift_to_target_leaves_zero_attempt_items_alone() -> None:
    """試投数が 0 の項目は触らない（割る相手がない）。"""
    pct = np.array([0.5, 0.6])
    out = shift_to_target(pct, np.zeros(2), 0.0)
    assert out == pytest.approx(pct)


# --- 入力の検査 ---

def test_empty_roster_is_rejected() -> None:
    _, target, _ = fixed()
    with pytest.raises(ValueError, match="1人もいない"):
        reconcile([], target)


def test_all_absent_is_rejected() -> None:
    """全員が欠場（期待出場時間の合計が 0）なら落とす。**0 で割らない。**"""
    _, target, _ = fixed()
    rng = np.random.default_rng(3)
    absent = [
        PlayerRates(
            player_id=f"p{i}", avail_prob=0.0, minutes_if_plays=30.0,
            rate={k: float(rng.uniform(*v)) for k, v in RATE_RANGE.items()},
            pct={"fg2_pct": 0.5, "fg3_pct": 0.35, "ft_pct": 0.75},
        )
        for i in range(5)
    ]
    with pytest.raises(ValueError, match="全員が欠場"):
        reconcile(absent, target)


def test_result_carries_the_player_ids() -> None:
    """**どの選手の行か分かること。** 位置だけで保存すると取り違える。"""
    players, target, _ = fixed()
    out = reconcile(players, target)
    assert isinstance(out, Reconciled)
    assert out.player_ids == tuple(p.player_id for p in players)
    assert len(out.minutes) == len(players)
