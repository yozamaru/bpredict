"""評価指標（要件 6.4 / 詳細設計 4.6）。"""
from __future__ import annotations

import numpy as np
import pytest

from batch.model import metrics
from batch.model.params import ECE_BINS, ECE_MIN_PER_BIN


def test_brier_and_log_loss_on_known_values() -> None:
    probs = np.array([1.0, 0.0, 0.5, 0.5])
    actual = np.array([1.0, 0.0, 1.0, 0.0])
    assert metrics.brier(probs, actual) == pytest.approx((0 + 0 + 0.25 + 0.25) / 4)
    # 完全に当てた2件は 0、0.5 の2件は -log(0.5)
    assert metrics.log_loss(probs, actual) == pytest.approx(2 * np.log(2) / 4, abs=1e-9)


def test_accuracy_counts_the_half_boundary_as_away() -> None:
    """`p > 0.5` で判定する。**0.5 をホーム勝ちに数えない**（表示専用の指標）。"""
    assert metrics.accuracy(np.array([0.5]), np.array([1.0])) == 0.0
    assert metrics.accuracy(np.array([0.5]), np.array([0.0])) == 1.0


@pytest.mark.parametrize(("probs", "actual"), [
    (np.array([0.5]), np.array([0.5])),      # 実績が 0/1 でない
    (np.array([1.5]), np.array([1.0])),      # 確率が範囲外
    (np.array([np.nan]), np.array([1.0])),   # 有限でない
    (np.array([]), np.array([])),            # 空
    (np.array([0.5, 0.5]), np.array([1.0])),  # 件数が合わない
])
def test_bad_input_raises_instead_of_returning_nan(probs, actual) -> None:
    """**黙って NaN を返さない。** NaN は「良い値」として比較を通り抜ける。"""
    with pytest.raises(metrics.MetricError):
        metrics.brier(probs, actual)


def test_ece_is_none_below_the_minimum_per_bin() -> None:
    """要件 6.4 は `n < 500` で ECE をゲートに使わないと定める。

    **0 を返さない。** 0 は「較正が完璧」と誤読される。
    """
    n = ECE_BINS * ECE_MIN_PER_BIN - 1
    rng = np.random.default_rng(0)
    probs = rng.random(n)
    actual = (rng.random(n) < probs).astype(float)
    assert metrics.ece(probs, actual) is None
    # ちょうど 500 件なら測れる
    probs = rng.random(ECE_BINS * ECE_MIN_PER_BIN)
    actual = (rng.random(probs.size) < probs).astype(float)
    assert metrics.ece(probs, actual) is not None


def test_ece_is_zero_when_every_bin_matches() -> None:
    """各ビンの平均予測と実現率が一致すれば 0。"""
    # 10ビン × 50件。各ビンで p を一定にし、実現率を p に正確に合わせる
    probs, actual = [], []
    for index in range(ECE_BINS):
        p = 0.05 + index * 0.09
        wins = round(p * ECE_MIN_PER_BIN)
        probs.extend([p] * ECE_MIN_PER_BIN)
        actual.extend([1.0] * wins + [0.0] * (ECE_MIN_PER_BIN - wins))
    value = metrics.ece(np.array(probs), np.array(actual))
    assert value is not None
    assert value < 0.01


def test_ece_detects_systematic_overconfidence() -> None:
    """全試合で 0.9 と言って実際は半分しか勝たないなら、ECE は 0.4 前後になる。"""
    n = ECE_BINS * ECE_MIN_PER_BIN
    probs = np.full(n, 0.9)
    actual = np.array([1.0, 0.0] * (n // 2))
    assert metrics.ece(probs, actual) == pytest.approx(0.4, abs=0.01)


def test_brier_difference_is_significant_only_when_the_interval_excludes_zero() -> None:
    n = 1000
    rng = np.random.default_rng(7)
    actual = (rng.random(n) < 0.6).astype(float)
    good = np.where(actual > 0.5, 0.75, 0.25)     # はっきり良い
    bad = np.full(n, 0.5)
    better = metrics.brier_difference(bad, good, actual)
    assert better.point > 0
    assert better.significant

    same = metrics.brier_difference(bad, bad.copy(), actual)
    assert same.point == pytest.approx(0.0)
    assert not same.significant, "同じ予測に差があってはならない"


def test_brier_difference_resamples_games_not_metrics() -> None:
    """**対応のある差**として再標本化すること。

    同じ試合に対する2つの予測の差を取ってから再標本化しないと、
    「どちらのモデルも同じ試合で外した」という構造が消えて区間が広がりすぎる。
    """
    n = 600
    rng = np.random.default_rng(11)
    actual = (rng.random(n) < 0.5).astype(float)
    base = rng.uniform(0.2, 0.8, n)
    # 挑戦側は一律にわずかに良い（各試合で必ず 0.01 だけ実績に近い）
    challenger = base + np.where(actual > 0.5, 0.01, -0.01)
    diff = metrics.brier_difference(base, challenger, actual)
    assert diff.point > 0
    assert diff.significant, "毎試合わずかに良いなら有意になる"


def test_ece_noise_floor_shrinks_as_n_grows() -> None:
    """完全較正でも出る ECE は n に依存する（要件 6.4 の実測の再現）。

    **固定閾値 0.05 を使わない理由がこれである。**
    """
    rng = np.random.default_rng(3)
    small = metrics.ece_noise_floor(rng.beta(4, 3, 500), resamples=200)
    large = metrics.ece_noise_floor(rng.beta(4, 3, 3000), resamples=200)
    assert small is not None and large is not None
    assert small > large, "n が小さいほどノイズフロアは高い"
    assert 0.02 < small < 0.12
    assert large < 0.05


def test_ece_noise_floor_is_none_when_ece_cannot_be_measured() -> None:
    assert metrics.ece_noise_floor(np.full(100, 0.5)) is None
