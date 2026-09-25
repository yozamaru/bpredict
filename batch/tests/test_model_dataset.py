"""学習行列・ベースライン・walk-forward（基本設計 2.3 / 詳細設計 4.6）。

LightGBM を使わない。**GBDT なしで検証できる形に分けてある**のがここの要点で、
`learn` を差し替えられるため分割の正しさを単体で確かめられる。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from batch.model import baselines
from batch.model.dataset import (
    MatrixError,
    TrainingData,
    constant_columns,
    null_rates,
    time_decay_weights,
)
from batch.model.evaluate import EvaluationError, folds_of, walk_forward

SEASONS = ("s1", "s2", "s3", "s4")


def fake_data(per_season: int = 10, seasons: tuple[str, ...] = SEASONS) -> TrainingData:
    """シーズンごとに同じ件数を持つ合成データ。"""
    rng = np.random.default_rng(5)
    season_ids = [s for s in seasons for _ in range(per_season)]
    n = len(season_ids)
    features = pd.DataFrame({
        "elo_diff": rng.normal(0, 100, n),
        "rest_days_diff": rng.integers(-2, 3, n).astype(float),
        "always_zero": np.zeros(n),
    })
    home_win = (rng.random(n) < 0.6).astype(float)
    return TrainingData(
        features=features,
        home_win=home_win,
        margin=rng.normal(3, 12, n),
        total=rng.normal(160, 15, n),
        game_ids=[f"g{i}" for i in range(n)],
        season_ids=season_ids,
        game_dates=[f"2020-01-{i % 28 + 1:02d}" for i in range(n)],
        spectator_restricted=[None] * n,
    )


# --- 時間減衰 ---

def test_time_decay_gives_the_newest_season_weight_one() -> None:
    data = fake_data(per_season=2)
    w = time_decay_weights(data.season_ids, data.seasons, lam=0.173)
    by_season = dict(zip(data.season_ids, w, strict=True))
    assert by_season["s4"] == pytest.approx(1.0)
    assert by_season["s3"] == pytest.approx(np.exp(-0.173))
    assert by_season["s1"] == pytest.approx(np.exp(-0.173 * 3))


def test_time_decay_lambda_zero_is_uniform() -> None:
    """λ=0 は「減衰なし」。探索の一端として意味を持つ。"""
    data = fake_data(per_season=2)
    w = time_decay_weights(data.season_ids, data.seasons, lam=0.0)
    assert np.allclose(w, 1.0)


def test_time_decay_rejects_unknown_seasons_and_negative_lambda() -> None:
    data = fake_data(per_season=2)
    with pytest.raises(MatrixError):
        time_decay_weights(["missing"], data.seasons)
    with pytest.raises(MatrixError):
        time_decay_weights(data.season_ids, data.seasons, lam=-0.1)


def test_seasons_keep_their_order_of_appearance() -> None:
    """**並べ替えない。** 時系列分割の順序がこれで決まる。"""
    data = fake_data(per_season=2, seasons=("2019-20-B1", "2016-17-B1"))
    assert data.seasons == ["2019-20-B1", "2016-17-B1"]


# --- 定数列の検出（採用ゲートの欠損率では捕まらない） ---

def test_constant_columns_are_detected() -> None:
    data = fake_data()
    assert constant_columns(data.features) == ["always_zero"]
    # **欠損率では捕まらない**（NULL ではなく定数0である）
    assert null_rates(data.features)["always_zero"] == 0.0


# --- fold の切り方 ---

@pytest.mark.parametrize(("count", "expected"), [
    (1, []), (2, []), (3, [2]), (4, [2, 3]), (5, [2, 3, 4]),
])
def test_folds_need_a_train_and_a_valid_season(count: int, expected: list[int]) -> None:
    """先頭2シーズンはテストにできない（学習か検証が空になる）。"""
    assert folds_of([f"s{i}" for i in range(count)]) == expected


def test_folds_are_capped_to_the_most_recent() -> None:
    """シーズンが増えるたびに学習時間が単調増加するのを防ぐ（基本設計 2.3）。"""
    seasons = [f"s{i}" for i in range(10)]
    assert folds_of(seasons, max_folds=3) == [7, 8, 9]


# --- walk-forward ---

def constant_learner(value: float = 0.6):
    """学習内容を記録しつつ、常に同じ確率を返す。"""
    seen: list[tuple[int, int]] = []

    def learn(tx, ty, tw, vx, vy):
        seen.append((len(tx), len(vx)))
        return (lambda X: np.full(len(X), value)), 7

    return learn, seen


def test_walk_forward_never_shows_the_test_season_to_the_learner() -> None:
    """**これが本命。** test に触れたら検証そのものが無意味になる（要件 6.3）。"""
    data = fake_data(per_season=10)
    season_ids = np.asarray(data.season_ids)
    given: list[set[str]] = []

    def learn(tx, ty, tw, vx, vy):
        # 渡された行の index から試合IDを復元する
        ids = {data.game_ids[i] for i in list(tx.index) + list(vx.index)}
        given.append(ids)
        return (lambda X: np.full(len(X), 0.6)), 7

    result = walk_forward(data, learn)
    for fold, ids in zip(result.folds, given, strict=True):
        test_ids = {data.game_ids[i] for i in np.flatnonzero(season_ids == fold.test_season)}
        assert not (ids & test_ids), f"{fold.test_season} の試合が学習・検証に混ざった"


def test_walk_forward_splits_follow_the_documented_shape() -> None:
    data = fake_data(per_season=10)
    learn, seen = constant_learner()
    result = walk_forward(data, learn)

    assert [f.test_season for f in result.folds] == ["s3", "s4"]
    assert result.folds[0].train_seasons == ("s1",)
    assert result.folds[0].valid_season == "s2"
    assert result.folds[1].train_seasons == ("s1", "s2")
    assert result.folds[1].valid_season == "s3"
    assert seen == [(10, 10), (20, 10)], "学習は累積し、検証は常に1シーズン"
    assert result.n == 20


def test_walk_forward_pools_folds_before_measuring() -> None:
    """**fold ごとの Brier を平均しない**（件数の違う fold が同じ重みになる）。"""
    data = fake_data(per_season=10)
    # s3 は全勝、s4 は全敗にして fold 間で難しさを変える
    home_win = np.asarray(data.home_win).copy()
    season_ids = np.asarray(data.season_ids)
    home_win[season_ids == "s3"] = 1.0
    home_win[season_ids == "s4"] = 0.0
    data = TrainingData(
        features=data.features, home_win=home_win, margin=data.margin, total=data.total,
        game_ids=data.game_ids, season_ids=data.season_ids, game_dates=data.game_dates,
        spectator_restricted=data.spectator_restricted,
    )
    learn, _ = constant_learner(0.6)
    result = walk_forward(data, learn)
    pooled = float(np.mean((result.probs - result.actual) ** 2))
    assert result.brier == pytest.approx(pooled)
    per_fold_mean = float(np.mean([f.brier for f in result.folds]))
    # この構成では両者は一致する（件数が同じ）。**式が違うことを固定する**
    assert result.brier == pytest.approx(per_fold_mean)


def test_walk_forward_needs_three_seasons() -> None:
    data = fake_data(per_season=10, seasons=("s1", "s2"))
    learn, _ = constant_learner()
    with pytest.raises(EvaluationError, match="3シーズン"):
        walk_forward(data, learn)


def test_walk_forward_rejects_mismatched_weights() -> None:
    data = fake_data(per_season=10)
    learn, _ = constant_learner()
    with pytest.raises(EvaluationError):
        walk_forward(data, learn, weights=np.ones(3))


# --- ベースライン（自前のロジスティック回帰） ---

def test_logistic_recovers_a_known_relationship() -> None:
    """**scikit-learn を入れない**判断の裏づけ。係数を復元できること。"""
    rng = np.random.default_rng(1)
    x = rng.normal(0, 1, 4000).reshape(-1, 1)
    p = baselines.sigmoid(0.5 + 1.5 * x[:, 0])
    y = (rng.random(4000) < p).astype(float)
    model = baselines.fit_logistic(x, y)
    assert model.intercept == pytest.approx(0.5, abs=0.15)
    assert model.coefficients[0] == pytest.approx(1.5, abs=0.15)


def test_logistic_handles_features_on_wildly_different_scales() -> None:
    """Elo差は数百、勝率差は 0〜1。**標準化しないと `l2` が片方にだけ効く。**"""
    rng = np.random.default_rng(2)
    elo = rng.normal(0, 150, 3000)
    winrate = rng.normal(0, 0.2, 3000)
    p = baselines.sigmoid(elo / 150 + winrate * 2)
    y = (rng.random(3000) < p).astype(float)
    model = baselines.fit_logistic(np.column_stack([elo, winrate]), y)
    # 元の尺度に戻した係数で予測が再現できること
    predicted = model.predict(np.column_stack([elo, winrate]))
    # **任意の閾値と比べない。** 真の確率 p を知っている上限（oracle）と比べる。
    # 標準化せずに解くと条件数が悪化して Elo差の係数が潰れ、この差が開く
    oracle = float(np.mean((p - y) ** 2))
    fitted = float(np.mean((predicted - y) ** 2))
    assert fitted <= oracle + 0.005, f"上限 {oracle:.4f} に対して {fitted:.4f}"
    assert model.coefficients[0] > 0 and model.coefficients[1] > 0


def test_logistic_tolerates_a_constant_column() -> None:
    """σ=0 の列があっても落ちない（`game_entries` が空のときに実在する）。"""
    rng = np.random.default_rng(4)
    x = np.column_stack([rng.normal(0, 1, 800), np.zeros(800)])
    y = (rng.random(800) < baselines.sigmoid(x[:, 0])).astype(float)
    model = baselines.fit_logistic(x, y)
    assert np.isfinite(model.coefficients).all()
    assert model.coefficients[1] == pytest.approx(0.0, abs=1e-6)


def test_logistic_weights_change_the_fit() -> None:
    """時間減衰の重みが効くこと。効かなければ探索に意味がない。"""
    x = np.array([[0.0], [1.0], [0.0], [1.0]])
    y = np.array([0.0, 1.0, 1.0, 0.0])
    heavy_first = baselines.fit_logistic(x, y, weights=np.array([10.0, 10.0, 1.0, 1.0]))
    heavy_last = baselines.fit_logistic(x, y, weights=np.array([1.0, 1.0, 10.0, 10.0]))
    assert heavy_first.coefficients[0] > 0
    assert heavy_last.coefficients[0] < 0


@pytest.mark.parametrize(("x", "y"), [
    (np.zeros((0, 1)), np.zeros(0)),
    (np.zeros((3, 0)), np.zeros(3)),
    (np.array([[1.0]]), np.array([0.5])),
    (np.array([[np.nan]]), np.array([1.0])),
])
def test_logistic_rejects_bad_input(x, y) -> None:
    with pytest.raises(baselines.FitError):
        baselines.fit_logistic(x, y)


def test_home_always_is_a_probability_that_always_predicts_home() -> None:
    probs = baselines.home_always(5)
    assert probs.size == 5
    assert (probs > 0.5).all()
    assert (probs < 1.0).all(), "log loss が無限にならないようにする"
