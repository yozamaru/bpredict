"""得点差・合計得点と、勝率の経路B（`batch/model/train_score.py`）。

**最重要は「勝率とスコアが矛盾しないこと」**（受け入れ基準 A-11）。各チーム得点を
独立に回帰すると「ホーム68%」なのに「82–85でアウェイ勝ち」が原理的に起こるため、
得点差と合計得点から導出する（要件 6.1）。
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from batch.model import train_score
from batch.model.train_score import (
    ScoreError,
    scores,
    win_prob_from_margin,
)

# --- 得点の導出 ---

def test_scores_sum_to_total_and_differ_by_margin() -> None:
    """`home + away = total` と `home − away = margin` が厳密に成立すること。"""
    margin = np.array([6.0, -3.5, 0.0, 21.0])
    total = np.array([160.0, 155.5, 170.0, 148.0])
    home, away = scores(margin, total)
    assert home + away == pytest.approx(total)
    assert home - away == pytest.approx(margin)


def test_scores_are_not_rounded() -> None:
    """**丸めない。** 画面に出すときに整数へ丸める（要件 8.3）。

    ここで丸めると `home + away = total` が崩れ、整合の検査が通らなくなる。
    """
    home, away = scores(np.array([5.0]), np.array([161.0]))
    assert home[0] == pytest.approx(83.0)
    assert away[0] == pytest.approx(78.0)
    # 合計が奇数なら 0.5 が出る。整数に寄せていないことの確認
    home, away = scores(np.array([4.0]), np.array([161.0]))
    assert home[0] == pytest.approx(82.5)


def test_scores_reject_mismatched_lengths() -> None:
    with pytest.raises(ScoreError):
        scores(np.array([1.0, 2.0]), np.array([160.0]))


# --- 経路B ---

def test_route_b_is_a_half_at_zero_margin() -> None:
    """得点差 0 は勝率 50%。σ に依らない。"""
    for sigma in (8.0, 12.0, 20.0):
        assert win_prob_from_margin(np.array([0.0]), sigma)[0] == pytest.approx(0.5)


def test_route_b_matches_the_normal_cdf() -> None:
    """`Φ(margin / σ)` であること。**定義を固定する。**"""
    margin = np.array([-10.0, -1.0, 3.0, 12.0])
    sigma = 11.5
    got = win_prob_from_margin(margin, sigma)
    want = [(1.0 + math.erf(float(v) / sigma / math.sqrt(2.0))) / 2.0 for v in margin]
    assert got == pytest.approx(want)


def test_route_b_is_monotone_in_margin() -> None:
    """得点差が大きいほど勝率が高いこと。**符号が勝率と矛盾しない根拠**。"""
    margin = np.array([-30.0, -10.0, -1.0, 0.0, 1.0, 10.0, 30.0])
    probs = win_prob_from_margin(margin, 11.5)
    assert list(probs) == sorted(probs)


def test_route_b_requires_a_measured_sigma() -> None:
    """**σ に既定値を置かない。** 要件は「実測前に定数として書かない」と定める。"""
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ScoreError):
            win_prob_from_margin(np.array([5.0]), bad)


# --- A-11: 勝率とスコアが矛盾しない ---

def test_win_prob_and_score_agree_on_route_b() -> None:
    """**経路B では構造的に矛盾しない**（要件 6.1）。

    勝率が 50% を超えるのは得点差が正のときだけで、そのとき導出したホーム得点は
    必ずアウェイ得点より大きい。**同じ `margin` から両方を作るため**である。
    """
    rng = np.random.default_rng(42)
    margin = rng.normal(1.4, 11.5, size=2000)
    total = rng.normal(160.0, 14.0, size=2000)
    probs = win_prob_from_margin(margin, 11.5)
    home, away = scores(margin, total)

    favored_home = probs > 0.5
    assert np.all(home[favored_home] > away[favored_home])
    assert np.all(home[~favored_home] <= away[~favored_home])


def test_rounding_for_display_can_tie_but_not_contradict() -> None:
    """**表示の丸めで順序が逆転しないこと。**

    画面には整数で出す（要件 8.3）。丸めて同点になることはあるが、
    **勝率が高い側の得点が低くなってはならない。**
    """
    rng = np.random.default_rng(7)
    margin = rng.normal(1.4, 11.5, size=5000)
    total = rng.normal(160.0, 14.0, size=5000)
    probs = win_prob_from_margin(margin, 11.5)
    home, away = scores(margin, total)
    shown_home, shown_away = np.round(home), np.round(away)

    favored_home = probs > 0.5
    assert np.all(shown_home[favored_home] >= shown_away[favored_home])
    assert np.all(shown_home[~favored_home] <= shown_away[~favored_home])


# --- 予想スコアを勝率から導く（要件 6.1.1） ---

def test_margin_from_win_prob_is_the_inverse_of_win_prob_from_margin() -> None:
    """`margin → 勝率 → margin` が元に戻ること。**向きを変えただけである。**"""
    sigma = 12.93
    margin = np.array([-20.0, -5.0, 0.0, 3.5, 18.0])
    back = train_score.margin_from_win_prob(
        train_score.win_prob_from_margin(margin, sigma), sigma)
    assert back == pytest.approx(margin, abs=1e-9)


def test_margin_from_win_prob_is_zero_at_even_odds() -> None:
    """50% なら得点差0。符号の食い違いが原理的に起きないことの核心である。"""
    assert train_score.margin_from_win_prob(
        np.array([0.5]), 12.93) == pytest.approx([0.0])


def test_margin_from_win_prob_rejects_the_closed_interval() -> None:
    """`Φ⁻¹(0)` は −∞。**クランプを通す前の確率を渡させない。**"""
    for bad in (0.0, 1.0):
        with pytest.raises(train_score.ScoreError):
            train_score.margin_from_win_prob(np.array([bad]), 12.93)


def test_margin_from_win_prob_rejects_a_bad_sigma() -> None:
    """σ は実測値である。**既定値を持たせない**（要件 6.1）。"""
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(train_score.ScoreError):
            train_score.margin_from_win_prob(np.array([0.6]), bad)


def test_the_derived_score_agrees_with_the_win_probability() -> None:
    """**A-11 が構造的に成立すること。** 勝率が 0.5 より大きければホームが勝つ。

    旧経路（Margin 回帰）では実測で 4.7% の試合が食い違っていた。
    """
    sigma = 12.93
    probs = np.array([0.12, 0.49, 0.50, 0.51, 0.88])
    margin = train_score.margin_from_win_prob(probs, sigma)
    home, away = train_score.scores(margin, np.full(probs.size, 160.0))
    for p, h, a in zip(probs, home, away, strict=True):
        if p > 0.5:
            assert h > a
        elif p < 0.5:
            assert h < a
        else:
            assert h == pytest.approx(a)
