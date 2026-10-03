"""第2段 PlayerMinutes の学習（詳細設計 2.3.1 / 4.5）。

**実データを読まない。** 合成した学習行列だけで検証する。

| テスト | どの規約か |
|---|---|
| `test_minutes_are_not_negative` | 回帰は負を出しうる。`CHECK (pred_minutes >= 0)` を破らせない |
| `test_minutes_have_no_upper_bound` | 延長戦では40分を超える。上限を設けない |
| `test_regression_metrics_only` | `brier` / `ece` を呼ばせない |
| `test_one_row_per_appearance` | 1行は「試合 × 出場した選手」 |
| `test_rows_without_minutes_are_dropped` | 出場時間が欠けた行を0で埋めない |
"""
from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd
import pytest

from batch.features.dataset import export_sqlite
from batch.features.player_rate import MINUTES_KEYS
from batch.model.dataset import (
    MatrixError,
    PlayerMinutesData,
    build_player_minutes_matrix,
)
from batch.model.evaluate import EvaluationError
from batch.model.train_player import (
    PlayerModelError,
    clip_minutes,
    evaluate_baseline,
    evaluate_minutes,
)

SEASONS = ("s1", "s2", "s3", "s4")

#: テストの木の本数。**本番は 4.7 の上限（1,200）である。**
ROUNDS = 30


def fake_data(per_season: int = 50) -> PlayerMinutesData:
    """`minutes_l5_player` が出場時間を本当に説明する合成データ。"""
    rng = np.random.default_rng(3)
    season_ids = [s for s in SEASONS for _ in range(per_season)]
    n = len(season_ids)
    recent = rng.uniform(5, 35, n)
    features = pd.DataFrame({
        "minutes_l5_player": recent,
        "minutes_l10_player": recent + rng.normal(0, 2, n),
        "is_starter_l5": rng.integers(0, 2, n).astype(float),
        "team_minutes_lost": rng.uniform(0, 60, n),
    }, columns=list(MINUTES_KEYS))
    return PlayerMinutesData(
        features=features,
        minutes=np.clip(recent + rng.normal(0, 4, n), 0.0, None),
        game_ids=[f"g{i // 10}" for i in range(n)],
        player_ids=[f"p{i % 12}" for i in range(n)],
        club_ids=["c1" if i % 2 else "c2" for i in range(n)],
        season_ids=season_ids,
        game_dates=[f"2020-01-{1 + i % 28:02d}" for i in range(n)],
    )


# --- 出力の丸め ---

def test_minutes_are_not_negative() -> None:
    """**回帰は負を出しうる。** `pred_minutes >= 0` の CHECK を破らせない（1.5）。"""
    assert clip_minutes(np.array([-9.0, 0.0, 12.5])).tolist() == [0.0, 0.0, 12.5]


def test_minutes_have_no_upper_bound() -> None:
    """**上限を設けない。** 延長戦では40分を超える（要件 6.8.5）。

    値域検証の `minutes` 0–60 は**取り込みの検証**であって予測の上限ではない。
    """
    assert clip_minutes(np.array([48.0, 61.0])).tolist() == [48.0, 61.0]


# --- 学習と評価 ---

def test_evaluate_uses_mae() -> None:
    pytest.importorskip("lightgbm")
    result = evaluate_minutes(fake_data(), num_boost_round=ROUNDS)
    assert result.n > 0
    assert np.isfinite(result.mae)


def test_regression_metrics_only() -> None:
    """**回帰の結果に分類の指標を呼ばせない**（`Evaluation._require_binary`）。"""
    pytest.importorskip("lightgbm")
    result = evaluate_minutes(fake_data(), num_boost_round=ROUNDS)
    with pytest.raises(EvaluationError, match="0/1"):
        _ = result.brier


def test_predictions_are_not_negative() -> None:
    pytest.importorskip("lightgbm")
    result = evaluate_minutes(fake_data(), num_boost_round=ROUNDS)
    assert result.probs.min() >= 0.0


def test_beats_the_mean_baseline() -> None:
    """**同じ fold で平均ベースラインと比べる**（要件 6.4 / 4.6）。

    合成データは `minutes_l5_player` が説明力を持つように作ってあるため、
    ここで負けるなら学習そのものが効いていない。
    """
    pytest.importorskip("lightgbm")
    data = fake_data()
    model = evaluate_minutes(data, num_boost_round=ROUNDS)
    base = evaluate_baseline(data)
    assert model.mae < base.mae


def test_empty_data_is_rejected() -> None:
    """行が1件も無ければ落とす。**0件で学習したふりをしない。**"""
    empty = PlayerMinutesData(
        features=pd.DataFrame(columns=list(MINUTES_KEYS)),
        minutes=np.empty(0), game_ids=[], player_ids=[], club_ids=[],
        season_ids=[], game_dates=[])
    with pytest.raises(PlayerModelError, match="1件もない"):
        evaluate_minutes(empty)


def test_features_are_four_columns() -> None:
    """**モデルに渡すのは4列だけ**（2.3.1）。"""
    data = fake_data()
    assert list(data.features.columns) == list(MINUTES_KEYS)
    assert data.features.shape[1] == 4


# --- 行の単位（実シード） ---

@pytest.fixture(scope="module")
def seed_matrix() -> PlayerMinutesData:
    """実シードから組んだ行列を**1回だけ**作る。

    **行列を書き換える検査はここを使わない**（module スコープで共有される）。
    """
    from batch.tests.conftest import apply_migrations
    from db.seeds.test.generate import seed_test_database

    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON")
    apply_migrations(con)
    seed_test_database(con)
    try:
        return build_player_minutes_matrix(export_sqlite(con))
    finally:
        con.close()


def test_one_row_per_appearance(seed_matrix: PlayerMinutesData) -> None:
    """**1行は「試合 × 出場した選手」。** 同じ組が二重に入らない。"""
    pairs = pd.DataFrame({"game": seed_matrix.game_ids, "player": seed_matrix.player_ids})
    assert not pairs.duplicated().any()
    assert len(seed_matrix) == len(seed_matrix.features) == seed_matrix.minutes.size


def test_many_players_per_game(seed_matrix: PlayerMinutesData) -> None:
    """1試合から複数行できること（チーム視点の2行とは違う）。"""
    counts = pd.Series(seed_matrix.game_ids).value_counts()
    assert counts.max() > 2


def test_minutes_are_the_target(seed_matrix: PlayerMinutesData) -> None:
    """目的変数はその試合の出場時間そのもの。**per-minute に割らない。**"""
    assert seed_matrix.minutes.min() >= 0.0
    assert seed_matrix.minutes.max() > 1.0, (
        "per-minute に割っていたら 1 を超えない水準になる")


def test_rows_without_minutes_are_dropped(seeded_db: sqlite3.Connection) -> None:
    """**出場時間が欠けた行を0で埋めない。** 「出場したが0分」になる。"""
    seeded_db.execute(
        "UPDATE player_game_stats SET minutes = NULL"
        " WHERE player_id = (SELECT player_id FROM player_game_stats LIMIT 1)")
    seeded_db.commit()
    data = build_player_minutes_matrix(export_sqlite(seeded_db))
    assert np.isfinite(data.minutes).all()


def test_no_finished_games_is_rejected(seeded_db: sqlite3.Connection) -> None:
    seeded_db.execute("UPDATE games SET status = 'SCHEDULED'")
    seeded_db.commit()
    with pytest.raises(MatrixError, match="終了した試合が1件もない"):
        build_player_minutes_matrix(export_sqlite(seeded_db))
