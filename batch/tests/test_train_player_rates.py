"""第3段 PlayerRates の学習と行列（詳細設計 2.3.1 / 4.5）。

| テスト | どの規約か |
|---|---|
| `test_count_target_is_per_minute` | **カウントは per-minute**（要件 6.8.4） |
| `test_pct_target_is_shrunk` | 成功率はシュリンク済み |
| `test_pct_weight_is_the_attempts` | 学習重みは試投数 |
| `test_counts_have_no_weight` | カウントに重みを付けない |
| `test_usable_rows_drop_zero_attempts` | 4.5 の `X[df[att] > 0]` |
| `test_usable_rows_drop_a_missing_prior` | `prior` が無い季の行を落とす |
| `test_shrink_column_keeps_nan` | 欠損を既定値で埋めない（規約5） |
| `test_shrink_column_is_clipped` | `[0.01, 0.99]`（2.4） |
| `test_model_features_build_the_shrunk_columns` | 行列は生、モデルはシュリンク済み |
| `test_the_learner_fills_pred_minutes` | **第2段を fold ごとに当てはめる**（2.3.1） |
| `test_pred_minutes_is_not_the_actual_minutes` | **規約4に違反させない** |
| `test_counts_are_not_negative` | 回帰は負を出しうる |
| `test_pct_output_is_clipped` | 整合化が `logit` を通る |
| `test_subset_keeps_the_index` | 索引を振り直さない（第2段の引き当て） |
| `test_unknown_target_is_rejected` | 14項目にない名前を黙って受けない |
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from batch.features import player_rate, team_rate
from batch.model.dataset import MatrixError, PlayerRateData
from batch.model.train_player import (
    SHRINK_K_INITIAL,
    MinutesProvider,
    PlayerModelError,
    evaluate_rate,
    learn_rate,
    rate_model_features,
    rate_recent_learner,
    rate_target,
    rate_weights,
    shrink_column,
    usable_rate_rows,
)

SEASONS = ("s1", "s2", "s3", "s4")

#: テストの木の本数。**本番は 4.7 の上限（1,200）である。**
ROUNDS = 20


def fake_data(per_season: int = 160) -> PlayerRateData:
    """`pred_minutes` と直近の水準が実績を本当に説明する合成データ。"""
    rng = np.random.default_rng(13)
    season_ids = [s for s in SEASONS for _ in range(per_season)]
    n = len(season_ids)
    minutes = np.clip(rng.uniform(4.0, 36.0, n), 1.0, None)

    columns: dict[str, np.ndarray] = {}
    for key in player_rate.all_rate_matrix_keys():
        columns[key] = rng.uniform(0.05, 0.4, n)
    columns["pred_minutes"] = np.full(n, np.nan)
    columns["position"] = rng.integers(0, 5, n).astype(float)
    columns["team_minutes_lost"] = np.zeros(n)
    columns["opponent_drtg"] = rng.uniform(95.0, 120.0, n)
    columns["opponent_pace"] = rng.uniform(65.0, 85.0, n)
    columns["usage_l10"] = rng.uniform(0.05, 0.35, n)

    counts: dict[str, np.ndarray] = {}
    for target in team_rate.COUNT_TARGETS:
        rate = columns[f"{target}_per_min_l10"]
        counts[target] = np.maximum(
            rate * minutes + rng.normal(0, 0.5, n), 0.0)

    shots: dict[str, np.ndarray] = {}
    for name, _, _ in team_rate.PCT_TARGETS:
        attempts = np.maximum(rng.poisson(5, n).astype(float), 1.0)
        columns[f"{name}_att_l10"] = attempts * 8
        columns[f"{name}_att_season"] = attempts * 20
        columns[f"{name}_prior"] = np.full(n, 0.4)
        true = np.clip(columns[f"{name}_made_l10"] / columns[f"{name}_att_l10"]
                       * 8 + 0.3, 0.05, 0.95)
        columns[f"{name}_made_l10"] = true * columns[f"{name}_att_l10"]
        columns[f"{name}_made_season"] = true * columns[f"{name}_att_season"]
        shots[f"{name}_att"] = attempts
        shots[f"{name}_made"] = np.minimum(
            rng.binomial(attempts.astype(int), np.clip(true, 0, 1)).astype(float),
            attempts)

    return PlayerRateData(
        features=pd.DataFrame(
            columns, columns=list(player_rate.all_rate_matrix_keys())),
        minutes_features=pd.DataFrame({
            "minutes_l5_player": minutes + rng.normal(0, 1.5, n),
            "minutes_l10_player": minutes + rng.normal(0, 1.5, n),
            "is_starter_l5": (minutes > 24).astype(float),
            "team_minutes_lost": np.zeros(n),
        }, columns=list(player_rate.MINUTES_KEYS)),
        minutes=minutes,
        counts=pd.DataFrame(counts, columns=list(team_rate.COUNT_TARGETS)),
        shots=pd.DataFrame(
            shots,
            columns=[f"{name}_{part}" for name, _, _ in team_rate.PCT_TARGETS
                     for part in ("made", "att")]),
        game_ids=[f"g{i // 10}" for i in range(n)],
        player_ids=[f"p{i % 20}" for i in range(n)],
        club_ids=["c1" if i % 2 else "c2" for i in range(n)],
        season_ids=season_ids,
        game_dates=[f"2020-01-{1 + i % 28:02d}" for i in range(n)],
    )


# --- 目的変数 ---

def test_count_target_is_per_minute() -> None:
    """**カウントのまま学習すると出場時間の分散に支配される**（要件 6.8.4）。"""
    data = fake_data(per_season=40)
    found = rate_target(data, "ast", SHRINK_K_INITIAL)
    expected = data.counts["ast"].to_numpy() / data.minutes
    assert np.allclose(found, expected)


def test_pct_target_is_shrunk() -> None:
    data = fake_data(per_season=40)
    found = rate_target(data, "ft_pct", SHRINK_K_INITIAL)
    k = SHRINK_K_INITIAL
    made = data.shots["ft_pct_made"].to_numpy()
    att = data.shots["ft_pct_att"].to_numpy()
    prior = data.features["ft_pct_prior"].to_numpy()
    assert np.allclose(found, np.clip((made + k * prior) / (att + k), 0.01, 0.99))


def test_the_target_changes_with_k() -> None:
    """**`k` は探索の対象である**（2.3.1）。行列の段で当てていないことの確認。"""
    data = fake_data(per_season=40)
    assert not np.allclose(
        rate_target(data, "ft_pct", 10.0), rate_target(data, "ft_pct", 80.0))


def test_pct_weight_is_the_attempts() -> None:
    data = fake_data(per_season=40)
    found = rate_weights(data, "fg3_pct")
    assert found is not None
    assert np.allclose(found, data.shots["fg3_pct_att"].to_numpy())


def test_counts_have_no_weight() -> None:
    """カウントに重みを付けない（要件 6.8.4 は成功率にだけ定めている）。"""
    assert rate_weights(fake_data(per_season=20), "oreb") is None


def test_unknown_target_is_rejected() -> None:
    data = fake_data(per_season=20)
    with pytest.raises(PlayerModelError, match="14項目"):
        rate_target(data, "pts", SHRINK_K_INITIAL)


# --- 絞り込み ---

def test_usable_rows_drop_zero_attempts() -> None:
    """**4.5 の擬似コードが `X[df[att] > 0]` で絞っている。**"""
    data = fake_data(per_season=40)
    data.shots.loc[0, "fg2_pct_att"] = 0.0
    keep = usable_rate_rows(data, "fg2_pct", SHRINK_K_INITIAL)
    assert not keep[0]
    assert keep[1:].all()


def test_usable_rows_drop_a_missing_prior() -> None:
    """**`prior` が無い季の行は学習に使えない**（2.3.1）。"""
    data = fake_data(per_season=40)
    data.features.loc[0, "ft_pct_prior"] = np.nan
    keep = usable_rate_rows(data, "ft_pct", SHRINK_K_INITIAL)
    assert not keep[0]


def test_usable_rows_keep_everything_for_counts() -> None:
    data = fake_data(per_season=40)
    assert usable_rate_rows(data, "stl", SHRINK_K_INITIAL).all()


def test_subset_keeps_the_index() -> None:
    """**索引を振り直さない。** 第2段を fold ごとに引き当てるために要る（2.3.1）。"""
    data = fake_data(per_season=40)
    keep = np.zeros(len(data), dtype=bool)
    keep[[3, 7, 11]] = True
    picked = data.subset(keep)
    assert list(picked.features.index) == [3, 7, 11]
    assert list(picked.minutes_features.index) == [3, 7, 11]
    assert picked.season_ids == [data.season_ids[i] for i in (3, 7, 11)]


def test_subset_rejects_an_empty_result() -> None:
    data = fake_data(per_season=20)
    with pytest.raises(MatrixError, match="1件も"):
        data.subset(np.zeros(len(data), dtype=bool))


# --- シュリンク ---

def test_shrink_column_keeps_nan() -> None:
    """**欠損を既定値で埋めない**（規約5）。"""
    found = shrink_column(
        np.array([10.0, np.nan]), np.array([30.0, 30.0]),
        np.array([0.4, 0.4]), 20.0)
    assert found[0] == pytest.approx((10 + 20 * 0.4) / 50)
    assert np.isnan(found[1])


def test_shrink_column_is_clipped() -> None:
    found = shrink_column(
        np.array([0.0, 100.0]), np.array([0.0, 100.0]),
        np.array([0.0, 1.0]), 20.0)
    assert found[0] == 0.01
    assert found[1] == 0.99


def test_shrink_column_rejects_a_non_positive_k() -> None:
    with pytest.raises(PlayerModelError, match="k"):
        shrink_column(np.array([1.0]), np.array([2.0]), np.array([0.5]), 0.0)


# --- モデルに渡る列 ---

def test_model_features_build_the_shrunk_columns() -> None:
    data = fake_data(per_season=40)
    built = rate_model_features(data, "fg3_pct", SHRINK_K_INITIAL)
    assert tuple(built.columns) == player_rate.rate_model_keys("fg3_pct")
    k = SHRINK_K_INITIAL
    expected = np.clip(
        (data.features["fg3_pct_made_l10"].to_numpy()
         + k * data.features["fg3_pct_prior"].to_numpy())
        / (data.features["fg3_pct_att_l10"].to_numpy() + k), 0.01, 0.99)
    assert np.allclose(built["fg3_pct_shrunk_l10"].to_numpy(), expected)


def test_model_features_for_counts_are_the_matrix_columns() -> None:
    data = fake_data(per_season=40)
    built = rate_model_features(data, "tov", SHRINK_K_INITIAL)
    assert tuple(built.columns) == player_rate.rate_model_keys("tov")
    assert built["tov_per_min_l10"].equals(data.features["tov_per_min_l10"])


def test_model_features_leave_pred_minutes_empty() -> None:
    """**行列の段では NaN。** fold ごとに第2段が埋める（2.3.1）。"""
    built = rate_model_features(fake_data(per_season=20), "ast", SHRINK_K_INITIAL)
    assert built["pred_minutes"].isna().all()


# --- 学習 ---

def _fold_features(data: PlayerRateData, target: str) -> pd.DataFrame:
    """`learn_rate` に渡る形（絞り込み済み・索引を保った行列）。"""
    picked = data.subset(usable_rate_rows(data, target, SHRINK_K_INITIAL))
    return rate_model_features(picked, target, SHRINK_K_INITIAL)


def test_the_learner_fills_pred_minutes() -> None:
    """**第2段を学習季だけで当てはめ、`pred_minutes` を埋める**（2.3.1）。"""
    data = fake_data(per_season=60)
    picked = data.subset(usable_rate_rows(data, "ast", SHRINK_K_INITIAL))
    features = rate_model_features(picked, "ast", SHRINK_K_INITIAL)
    y = rate_target(picked, "ast", SHRINK_K_INITIAL)

    train = np.asarray([s in ("s1", "s2") for s in picked.season_ids])
    valid = np.asarray([s == "s3" for s in picked.season_ids])
    test = np.asarray([s == "s4" for s in picked.season_ids])
    learn = learn_rate(
        MinutesProvider(data, num_boost_round=ROUNDS), "ast",
        num_boost_round=ROUNDS)
    predict, _ = learn(
        features[train], y[train], np.ones(int(train.sum())),
        features[valid], y[valid])
    out = predict(features[test])
    assert np.isfinite(out).all(), "pred_minutes が NaN のまま予測した"


def test_pred_minutes_is_not_the_actual_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**当該試合の実際の出場時間を入れない**（規約4。2.3.1）。

    第3段に渡る行列を `learn_score` の入口で覗き、`pred_minutes` が
    (a) 埋まっていること (b) 実績と一致しないこと を見る。**一致したら
    実績を読んでいる** — それは目的変数の分母を知った状態である。
    """
    data = fake_data(per_season=60)
    picked = data.subset(usable_rate_rows(data, "ast", SHRINK_K_INITIAL))
    features = rate_model_features(picked, "ast", SHRINK_K_INITIAL)
    y = rate_target(picked, "ast", SHRINK_K_INITIAL)
    train = np.asarray([s in ("s1", "s2") for s in picked.season_ids])
    valid = np.asarray([s == "s3" for s in picked.season_ids])

    import batch.model.train_player as module
    original = module.learn_score
    seen: list[pd.DataFrame] = []

    def spy(train_x: pd.DataFrame, *args: object, **kwargs: object) -> object:
        seen.append(train_x)
        return original(train_x, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(module, "learn_score", spy)
    learn = learn_rate(
        MinutesProvider(data, num_boost_round=ROUNDS), "ast",
        num_boost_round=ROUNDS)
    learn(features[train], y[train], np.ones(int(train.sum())),
          features[valid], y[valid])

    # 第2段（`learn_minutes` 経由）と第3段の2回呼ばれる。第3段の方を見る
    assert len(seen) == 2
    stage_three = seen[-1]
    assert "pred_minutes" in stage_three
    assert stage_three["pred_minutes"].notna().all(), "NaN のまま学習した"
    actual = picked.minutes[train]
    assert not np.allclose(
        stage_three["pred_minutes"].to_numpy(), actual), (
        "pred_minutes が当該試合の実績と一致している（規約4に違反）")


def test_counts_are_not_negative() -> None:
    """**回帰は負を出しうる。** per-minute のレートは非負である。"""
    data = fake_data(per_season=60)
    found = evaluate_rate(data, "blk", num_boost_round=ROUNDS)
    assert (found.probs >= 0).all()


def test_pct_output_is_clipped() -> None:
    """**整合化が `logit` を通る**（要件 6.8.4）。"""
    data = fake_data(per_season=60)
    found = evaluate_rate(data, "ft_pct", num_boost_round=ROUNDS)
    assert found.probs.min() >= 0.01
    assert found.probs.max() <= 0.99


def test_regression_metrics_are_available() -> None:
    """**回帰の結果に分類の指標を呼べない**（`Evaluation._require_binary`）。"""
    data = fake_data(per_season=60)
    found = evaluate_rate(data, "ast", num_boost_round=ROUNDS)
    assert found.mae > 0
    assert found.residual_sigma > 0


def test_the_recent_baseline_does_not_learn() -> None:
    """**「直近10試合の水準をそのまま出す」と比べる**（第2段と同じ作法）。"""
    data = fake_data(per_season=60)
    found = evaluate_rate(data, "ast", learner=rate_recent_learner("ast"))
    picked = data.subset(usable_rate_rows(data, "ast", SHRINK_K_INITIAL))
    features = rate_model_features(picked, "ast", SHRINK_K_INITIAL)
    test = np.asarray([s == "s4" for s in picked.season_ids])
    assert np.allclose(
        found.folds[-1].probs, features["ast_per_min_l10"].to_numpy()[test])


def test_the_stage_two_model_is_shared_across_targets() -> None:
    """**fold ごとに1本の第2段を14本で共有する**（2.3.1）。

    本番では登録済みの第2段が1本だけある。項目ごとに当てはめ直すと、
    **学習時だけ14本の第2段が存在することになり、本番と入力の作り方が
    食い違う。**
    """
    data = fake_data(per_season=60)
    provider = MinutesProvider(data, num_boost_round=ROUNDS)
    train = data.minutes_features.index[
        np.asarray([s in ("s1", "s2") for s in data.season_ids], dtype=bool)]
    valid = data.minutes_features.index[
        np.asarray([s == "s3" for s in data.season_ids], dtype=bool)]
    first = provider.for_fold(train, valid)
    second = provider.for_fold(train[::2], valid)
    # 季の組が同じなら同じモデルを返す（行の部分集合でも当てはめ直さない）
    assert np.allclose(first(valid), second(valid))


def test_the_provider_requires_a_single_validation_season() -> None:
    """検証の季が1つでなければ落とす（walk-forward の契約）。"""
    data = fake_data(per_season=40)
    provider = MinutesProvider(data, num_boost_round=ROUNDS)
    index = data.minutes_features.index
    with pytest.raises(PlayerModelError, match="検証の季"):
        provider.for_fold(index[:10], index)
