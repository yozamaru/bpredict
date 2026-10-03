"""TeamRate の学習と評価（詳細設計 2.2.1 / 4.5、`batch/model/train_team_rates.py`）。

**実データを読まない。** 合成した学習行列だけで検証する。

| テスト | どの規約か |
|---|---|
| `test_pct_models_are_weighted_by_attempts` | 成功率は試投数を重みにする（4.5） |
| `test_pct_output_is_clipped` | 出力を `[0.01, 0.99]` に収める（整合化が `logit` を通る） |
| `test_counts_are_not_negative` | 回帰は負を出しうる。CHECK 制約を破らせない |
| `test_counts_are_not_per_minute` | チームは常に200分。per-minute にしない |
| `test_zero_attempt_rows_are_dropped` | 試投0の行は目的変数が NaN |
| `test_regression_metrics_only` | `brier` / `ece` を呼ばせない |
"""
from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd
import pytest

from batch.features.dataset import export_sqlite
from batch.features.team_rate import COUNT_TARGETS, PCT_TARGETS, TARGETS, feature_keys
from batch.model.dataset import MatrixError, TeamRateData, build_team_rate_matrix
from batch.model.evaluate import EvaluationError
from batch.model.train_team_rates import (
    PCT_CLIP,
    TeamRateError,
    clip_count,
    clip_pct,
    evaluate_target,
    is_pct,
)

SEASONS = ("s1", "s2", "s3", "s4")

#: テストの木の本数。**本番は 4.7 の上限（1,200）である。**
#: ここで下げるのは所要時間のためで、検査したい性質（丸め・重み・指標）は
#: 本数に依らない。本数そのものを検査するテストはこの値を使わない。
ROUNDS = 30


def fake_data(per_season: int = 40) -> TeamRateData:
    """`pace_own` が目的変数を本当に説明する合成データ。"""
    rng = np.random.default_rng(5)
    season_ids = [s for s in SEASONS for _ in range(per_season)]
    n = len(season_ids)
    pace_own = rng.uniform(60, 90, n)
    rows = pd.DataFrame({
        "pace_own": pace_own,
        "pace_opp": rng.uniform(60, 90, n),
        "rest_days_own": rng.integers(0, 4, n).astype(float),
    })
    actual: dict[str, np.ndarray] = {}
    attempts: dict[str, np.ndarray] = {}
    for target in COUNT_TARGETS:
        rows[f"own_{target}_l10"] = pace_own * 0.3 + rng.normal(0, 1, n)
        rows[f"opponent_{target}_allowed_l10"] = rng.normal(10, 2, n)
        actual[target] = np.maximum(pace_own * 0.3 + rng.normal(0, 2, n), 0.0)
    for pct, _, attempt in PCT_TARGETS:
        rows[f"own_{pct}_l10"] = rng.uniform(0.3, 0.6, n)
        rows[f"opponent_{pct}_allowed_l10"] = rng.uniform(0.3, 0.6, n)
        actual[pct] = np.clip(rng.uniform(0.2, 0.8, n), 0.01, 0.99)
        attempts[attempt] = actual[attempt]
    return TeamRateData(
        rows=rows,
        actual=pd.DataFrame(actual, columns=list(TARGETS)),
        attempts=pd.DataFrame(attempts, columns=[a for _, _, a in PCT_TARGETS]),
        game_ids=[f"g{i}" for i in range(n)],
        club_ids=["c1" if i % 2 else "c2" for i in range(n)],
        season_ids=season_ids,
        game_dates=[f"2020-01-{1 + i % 28:02d}" for i in range(n)],
        club_win=rng.integers(0, 2, n).astype(float),
        club_margin=rng.normal(0, 10, n),
        total=rng.normal(160, 10, n),
        spectator_restricted=[None] * n,
    )


# --- 区分の判定 ---

def test_is_pct_splits_fourteen_into_eleven_and_three() -> None:
    assert sum(is_pct(t) for t in TARGETS) == 3
    assert sum(not is_pct(t) for t in TARGETS) == 11


def test_unknown_target_is_rejected() -> None:
    with pytest.raises(TeamRateError, match="14項目にない"):
        is_pct("pts")


# --- 出力の丸め ---

def test_pct_output_is_clipped() -> None:
    """**`[0.01, 0.99]` に収める**（4.5）。整合化が `logit()` を通るため、
    0 や 1 が来ると発散する。"""
    assert PCT_CLIP == (0.01, 0.99)
    out = clip_pct(np.array([-0.5, 0.0, 0.004, 0.5, 0.999, 1.0, 1.4]))
    assert out.min() >= 0.01
    assert out.max() <= 0.99
    assert out[3] == pytest.approx(0.5)


def test_counts_are_not_negative() -> None:
    """**回帰は負を出しうる。** `tgt_fg2a >= 0` ほかの CHECK を破らせない（1.5）。"""
    out = clip_count(np.array([-3.0, 0.0, 12.5]))
    assert out.tolist() == [0.0, 0.0, 12.5]


# --- 学習と評価 ---

def test_evaluate_count_target_uses_mae() -> None:
    """カウント項目の評価は `mae`。**`brier` を呼ばない。**"""
    pytest.importorskip("lightgbm")
    data = fake_data()
    result = evaluate_target(data, "fg3a", num_boost_round=ROUNDS)
    assert result.n > 0
    assert np.isfinite(result.mae)


def test_regression_metrics_only() -> None:
    """**回帰の結果に分類の指標を呼ばせない**（`Evaluation._require_binary`）。"""
    pytest.importorskip("lightgbm")
    result = evaluate_target(fake_data(), "fg3a", num_boost_round=ROUNDS)
    with pytest.raises(EvaluationError, match="0/1"):
        _ = result.brier


def test_pct_target_predictions_stay_inside_the_clip() -> None:
    """成功率の予測が `[0.01, 0.99]` の内側に収まること。"""
    pytest.importorskip("lightgbm")
    result = evaluate_target(fake_data(), "ft_pct", num_boost_round=ROUNDS)
    assert result.probs.min() >= PCT_CLIP[0]
    assert result.probs.max() <= PCT_CLIP[1]


def test_count_target_predictions_are_not_negative() -> None:
    pytest.importorskip("lightgbm")
    result = evaluate_target(fake_data(), "oreb", num_boost_round=ROUNDS)
    assert result.probs.min() >= 0.0


def test_features_are_five_columns() -> None:
    """**モデルに渡すのは5列だけ**（案C）。31列を渡していないこと。"""
    data = fake_data()
    for target in TARGETS:
        features = data.features(target)
        assert list(features.columns) == list(feature_keys(target))
        assert features.shape[1] == 5


def test_pct_models_are_weighted_by_attempts() -> None:
    """**成功率は試投数を重みにする**（4.5）。カウントは重みなし。"""
    data = fake_data()
    for pct, _, attempt in PCT_TARGETS:
        weights = data.weights(pct)
        assert weights is not None
        assert weights.tolist() == pytest.approx(
            np.asarray(data.attempts[attempt]).tolist())
    for count in COUNT_TARGETS:
        assert data.weights(count) is None


def test_zero_attempt_rows_are_dropped() -> None:
    """**試投0の行を学習に入れない。** 目的変数が NaN であり、重みも0になる。

    LightGBM は重み0の行を無視するが、目的変数の NaN は無視しない。
    """
    pytest.importorskip("lightgbm")
    data = fake_data()
    data.actual.loc[0, "ft_pct"] = float("nan")
    data.attempts.loc[0, "fta"] = 0.0
    result = evaluate_target(data, "ft_pct", num_boost_round=ROUNDS)
    assert np.isfinite(result.probs).all()
    assert np.isfinite(result.actual).all()


def test_target_without_usable_rows_is_rejected() -> None:
    """使える行が1件もなければ落とす。**0 件で学習したふりをしない。**"""
    data = fake_data()
    data.actual.loc[:, "ft_pct"] = float("nan")
    with pytest.raises(TeamRateError, match="使える行が1件もない"):
        evaluate_target(data, "ft_pct", num_boost_round=ROUNDS)


def test_unknown_target_in_data_is_rejected() -> None:
    with pytest.raises(MatrixError, match="14項目にない"):
        fake_data().target("pts")


@pytest.fixture(scope="module")
def seed_matrix() -> TeamRateData:
    """実シードから組んだ行列を**1回だけ**作る。

    448行で約17秒かかる（1行あたり約38ms。勝敗モデルの1試合あたりと同水準）。
    関数ごとに組み直すと検査の数だけ待つことになる。**行列を書き換える検査は
    ここを使わない**（module スコープで共有されるため）。
    """
    from batch.tests.conftest import apply_migrations
    from db.seeds.test.generate import seed_test_database

    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON")
    apply_migrations(con)
    seed_test_database(con)
    try:
        return build_team_rate_matrix(export_sqlite(con))
    finally:
        con.close()


# --- 行の単位 ---

def test_counts_are_not_per_minute() -> None:
    """**チームは常に200分。** 目的変数はその試合のカウントそのものである。

    個人モデル（2.3）は per-minute にするが、チームでは意味がない。テンポの違いは
    `pace_own` / `pace_opp` が持つ。
    """
    data = fake_data()
    assert data.target("fg3a").max() > 1.0, (
        "per-minute に割っていたら 1 を超えない水準になる")


def test_two_rows_per_game_from_real_seed(seed_matrix: TeamRateData) -> None:
    """**1試合から2行できる**（2.2.1）。実シードで確かめる。"""
    data = seed_matrix
    counts = pd.Series(data.game_ids).value_counts()
    assert set(counts.unique()) <= {1, 2}
    assert (counts == 2).any(), "2行できている試合が1件もない"
    # 同じ試合の2行は別のクラブであること
    pairs = pd.DataFrame({"game": data.game_ids, "club": data.club_ids})
    assert not pairs.duplicated().any()


def test_matrix_columns_and_labels_line_up(seed_matrix: TeamRateData) -> None:
    """列の集合・目的変数・重みの件数が揃っていること。"""
    data = seed_matrix
    assert len(data) == len(data.rows) == len(data.actual) == len(data.attempts)
    assert list(data.actual.columns) == list(TARGETS)
    for target in TARGETS:
        assert data.features(target).shape == (len(data), 5)


def test_club_view_outcomes_are_real(seed_matrix: TeamRateData) -> None:
    """`club_win` / `club_margin` が**クラブ視点の実績**であること（作り物でない）。

    同じ試合の2行は、勝敗が反転し得失点差の符号が逆になる。
    """
    data = seed_matrix
    frame = pd.DataFrame({
        "game": data.game_ids, "win": data.club_win, "margin": data.club_margin,
    })
    paired = frame.groupby("game").filter(lambda g: len(g) == 2)
    assert not paired.empty
    for _, group in paired.groupby("game"):
        margins = group["margin"].tolist()
        assert margins[0] == pytest.approx(-margins[1])
        assert sorted(group["win"].tolist()) == [0.0, 1.0]
