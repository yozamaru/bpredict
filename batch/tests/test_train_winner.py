"""勝敗モデルの後処理（`batch/model/train_winner.py`）。

**LightGBM をインポートしない。** `learn_winner` は遅延インポートで GBDT を読むが、
ここで検証するのはクランプだけである（モジュールを読むだけでは lightgbm は不要）。
"""
from __future__ import annotations

import numpy as np
import pytest

from batch.model.train_winner import (
    WIN_PROB_CEILING,
    WIN_PROB_FLOOR,
    clamp_win_prob,
)


def test_all_starters_out_probability_clamped() -> None:
    """外挿域の確率を `[0.05, 0.95]` に収める（詳細設計 6.4）。

    全選手欠場のように学習域の外へ出た入力では較正も効かない。
    「ホーム 100% – アウェイ 0%」と表示して外すと信頼が一度で失われる。
    """
    clamped = clamp_win_prob(np.array([0.0, 0.01, 0.5, 0.99, 1.0]))
    assert clamped.min() == pytest.approx(WIN_PROB_FLOOR)
    assert clamped.max() == pytest.approx(WIN_PROB_CEILING)
    assert clamped[2] == pytest.approx(0.5)


def test_the_clamp_leaves_the_interior_untouched() -> None:
    """区間の内側は動かさない。**較正の一部ではなく外挿域の保険である。**"""
    inside = np.array([0.05, 0.2, 0.68, 0.95])
    assert clamp_win_prob(inside) == pytest.approx(inside)


def test_the_clamp_keeps_the_inverse_normal_finite() -> None:
    """クランプが `Φ⁻¹(p)` の有限性の保証でもある（要件 6.1.1）。

    予想スコアは `σ · Φ⁻¹(p)` から作るため、`p = 0` を通すと得点差が −∞ になる。
    """
    from batch.model.train_score import margin_from_win_prob

    margin = margin_from_win_prob(clamp_win_prob(np.array([0.0, 1.0])), 12.93)
    assert np.isfinite(margin).all()
