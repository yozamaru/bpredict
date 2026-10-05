"""較正器（要件 6.7 / 詳細設計 2.3.1）。

| テスト | どの規約か |
|---|---|
| `test_output_stays_inside_the_open_interval` | **クリップに頼らない**（2.4 と同じ理由） |
| `test_float64_rounds_to_the_endpoints_at_extreme_coefficients` | **保証は数学的であって、倍精度では厳密でない** |
| `test_it_corrects_an_overconfident_model` | 過信を直せること |
| `test_it_leaves_a_calibrated_model_alone` | 既に合っているものを壊さないこと |
| `test_a_single_class_returns_the_identity` | 片方しか無い fold で発散させない |
| `test_perfect_separation_does_not_diverge` | Platt の平滑化が効いていること |
| `test_regression_output_is_rejected` | 0/1 でない実測を黙って通さない |
| `test_the_kind_and_version_are_checked` | 別の較正器として読まない |
"""
from __future__ import annotations

import numpy as np
import pytest

from batch.model.calibrate import (
    EPS,
    IDENTITY,
    CalibrationError,
    Platt,
    dump,
    fit_platt,
    load,
)
from batch.model.metrics import ece


def _draw(probs: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """確率から 0/1 を引く。

    **確率を作った乱数と同じ種から引かない。** `default_rng(s).uniform(0.05, 0.95)` と
    `default_rng(s).random()` は同じ基底の一様乱数を使うため、
    `u < 0.05 + 0.9u ⇔ u < 0.5` に退化し、**ラベルが予測と完全に逆相関する**
    （最初の版で実際に起き、当てはめが `a = -62.9` を返した）。
    """
    return (rng.random(probs.size) < probs).astype(np.float64)


# --- 値域 ---

def test_output_stays_inside_the_open_interval() -> None:
    """**`(0,1)` を出ない。** logit 空間で動かすためクリップが要らない（2.4）。

    係数は実測の範囲（`a ≈ 0.97〜0.99` / `b ≈ -0.24〜-0.04`）を含む水準で試す。
    """
    probs = np.array([0.0, EPS / 2, 0.5, 1.0 - EPS / 2, 1.0])
    for a, b in ((1.0, 0.0), (0.98, -0.24), (2.0, 1.0), (-3.0, 2.0)):
        out = Platt(a=a, b=b).apply(probs)
        assert (out > 0).all() and (out < 1).all(), (a, b, out)
        assert np.isfinite(out).all()


def test_float64_rounds_to_the_endpoints_at_extreme_coefficients() -> None:
    """**数学的な保証は float64 では厳密に成り立たない。**

    `|a × logit(p) + b| > 37` で `sigmoid` が 0.0 / 1.0 に丸まる。実測の係数では
    `|z| < 10` に収まり届かないが、**成り立たないことを知らずにいない**ために
    固定する。出力はこの後 `clamp_avail_prob` が `[0.01, 0.99]` に収める。
    """
    out = Platt(a=10.0, b=5.0).apply(np.array([1.0]))
    assert out[0] == 1.0
    assert np.isfinite(out).all()
    assert (out >= 0).all() and (out <= 1).all()


def test_the_identity_changes_nothing() -> None:
    """**「入れない」を明示的に表せること。** 効かなければ入れない判断に使う。"""
    probs = np.array([0.02, 0.5, 0.97])
    assert np.allclose(IDENTITY.apply(probs), probs, atol=1e-9)


# --- 当てはめ ---

def test_it_corrects_an_overconfident_model() -> None:
    """過信したモデルの目盛りを直せること。

    真の確率を 0.6 倍の鋭さに潰した予測を作り、較正で ECE が下がることを見る。
    """
    rng = np.random.default_rng(3)
    truth = rng.uniform(0.05, 0.95, 20_000)
    # **確率を作った後の同じ Generator から引く**（別の種にしない。上記 `_draw`）
    actual = _draw(truth, rng)
    # logit を 1.8 倍に引き伸ばす = 過信
    logit = np.log(truth / (1 - truth))
    overconfident = 1 / (1 + np.exp(-1.8 * logit))

    model = fit_platt(overconfident, actual)
    before, after = ece(overconfident, actual), ece(model.apply(overconfident), actual)
    assert before is not None and after is not None
    assert after < before, (before, after)
    # 引き伸ばしを戻す向きに傾きが立つ
    assert model.a < 1.0


def test_it_leaves_a_calibrated_model_alone() -> None:
    """既に合っているものを壊さない（`a` が 1、`b` が 0 の近くに来る）。"""
    rng = np.random.default_rng(11)
    truth = rng.uniform(0.05, 0.95, 20_000)
    model = fit_platt(truth, _draw(truth, rng))
    assert abs(model.a - 1.0) < 0.15
    assert abs(model.b) < 0.15


def test_a_single_class_returns_the_identity() -> None:
    """**片方しか無い fold では傾きが決まらない。** 例外にせず恒等を返す。"""
    probs = np.array([0.2, 0.5, 0.8])
    assert fit_platt(probs, np.zeros(3)) == IDENTITY
    assert fit_platt(probs, np.ones(3)) == IDENTITY


def test_perfect_separation_does_not_diverge() -> None:
    """**完全に分離できる入力で `a` が発散しないこと**（Platt の平滑化）。

    平滑化を入れないと log loss が傾きを無限に大きくし続ける。
    """
    probs = np.concatenate([np.full(200, 0.1), np.full(200, 0.9)])
    actual = np.concatenate([np.zeros(200), np.ones(200)])
    model = fit_platt(probs, actual)
    assert np.isfinite([model.a, model.b]).all()
    assert abs(model.a) < 100.0
    out = model.apply(probs)
    assert (out > 0).all() and (out < 1).all()


# --- 入力の検査 ---

def test_regression_output_is_rejected() -> None:
    """**実測が 0/1 でなければ落とす。** 黙って意味のない値を返さない。"""
    with pytest.raises(CalibrationError, match="0/1"):
        fit_platt(np.array([0.3, 0.7]), np.array([12.5, 31.2]))


def test_probabilities_outside_the_unit_interval_are_rejected() -> None:
    with pytest.raises(CalibrationError, match=r"\[0,1\]"):
        fit_platt(np.array([0.3, 1.4]), np.array([0.0, 1.0]))


def test_mismatched_shapes_are_rejected() -> None:
    with pytest.raises(CalibrationError, match="形が合わない"):
        fit_platt(np.array([0.3, 0.7]), np.array([1.0]))


# --- 保存と読み出し ---

def test_a_round_trip_keeps_the_coefficients() -> None:
    model = Platt(a=0.9824, b=-0.0406)
    assert load(dump(model)) == model


def test_the_kind_and_version_are_checked() -> None:
    """**別の較正器として読まない。** 種類も版も照合する。"""
    with pytest.raises(CalibrationError, match="種類"):
        load('{"kind":"isotonic","version":1,"a":1,"b":0}')
    with pytest.raises(CalibrationError, match="版"):
        load('{"kind":"platt","version":2,"a":1,"b":0}')
    with pytest.raises(CalibrationError, match="JSON"):
        load("platt")
    with pytest.raises(CalibrationError, match="有限"):
        load('{"kind":"platt","version":1,"a":1e999,"b":0}')
