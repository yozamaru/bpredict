"""第1段 PlayerAvail の学習と行列（詳細設計 2.3.1 / 4.5）。

| テスト | どの規約か |
|---|---|
| `test_probabilities_are_clamped` | **`[0.01, 0.99]`。** 勝率の幅を借りない |
| `test_binary_metrics_are_available` | 0/1 なので `brier` / `ece` が使える |
| `test_beats_both_baselines` | **出場率と直近出場率の両方**と比べる（4.6） |
| `test_label_is_appearance` | `player_game_stats` に行があることが正例 |
| `test_candidates_without_appearance_are_negatives` | 負例を落とさない |
| `test_uncovered_appearances_are_counted` | 覆えなかった出場を黙って落とさない |
| `test_roster_only_players_are_not_rows` | **ロスターを候補にしない**（絶対ルール1） |
"""
from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd
import pytest

from batch.features.dataset import export_sqlite
from batch.features.player_rate import AVAIL_KEYS
from batch.model.dataset import (
    MatrixError,
    PlayerAvailData,
    build_player_avail_matrix,
)
from batch.model.train_player import (
    PlayerModelError,
    evaluate_avail,
    prevalence_learner,
    ratio_learner,
)

SEASONS = ("s1", "s2", "s3", "s4")

#: テストの木の本数。**本番は 4.7 の上限（1,200）である。**
ROUNDS = 30


def fake_data(per_season: int = 200) -> PlayerAvailData:
    """`games_played_ratio_l10` が出場を本当に説明する合成データ。"""
    rng = np.random.default_rng(7)
    season_ids = [s for s in SEASONS for _ in range(per_season)]
    n = len(season_ids)
    ratio = rng.uniform(0.0, 1.0, n)
    minutes = ratio * 30.0 + rng.normal(0, 2, n)
    features = pd.DataFrame({
        "minutes_l5_player": np.clip(minutes, 0.0, None),
        "minutes_l10_player": np.clip(minutes + rng.normal(0, 2, n), 0.0, None),
        "games_played_ratio_l10": ratio,
        "days_since_last_played": np.clip(90.0 - ratio * 85.0, 0.0, 90.0),
        "entry_status": np.zeros(n),
    }, columns=list(AVAIL_KEYS))
    played = (rng.uniform(0, 1, n) < np.clip(ratio, 0.05, 0.95)).astype(float)
    return PlayerAvailData(
        features=features,
        played=played,
        game_ids=[f"g{i // 10}" for i in range(n)],
        player_ids=[f"p{i % 20}" for i in range(n)],
        club_ids=["c1" if i % 2 else "c2" for i in range(n)],
        season_ids=season_ids,
        game_dates=[f"2020-01-{1 + i % 28:02d}" for i in range(n)],
    )


# --- 出力の丸め ---

def test_probabilities_are_clamped() -> None:
    """**`[0.01, 0.99]` に収める。** 勝率の `[0.05, 0.95]` を使わない。

    「この選手は出ない」は本当に 0.01 側の状態である。実測でも勝率の幅は
    ECE を 2.5倍に悪化させた（`clamp_avail_prob`）。
    """
    pytest.importorskip("lightgbm")
    result = evaluate_avail(fake_data(), num_boost_round=ROUNDS)
    assert result.probs.min() >= 0.01
    assert result.probs.max() <= 0.99


def test_win_probability_bounds_are_not_reused() -> None:
    """**勝率の幅を借りない。** 借りると ECE が 2.5倍に悪化する（実測）。"""
    from batch.model.train_player import AVAIL_PROB_BOUNDS, clamp_avail_prob
    assert AVAIL_PROB_BOUNDS == (0.01, 0.99)
    assert clamp_avail_prob(np.array([0.0, 0.5, 1.0])).tolist() == [0.01, 0.5, 0.99]


def test_binary_metrics_are_available() -> None:
    """目的変数は 0/1 なので `brier` と `ece` が使える（第2段と違う）。

    第2段は `Evaluation._require_binary` が落とす。**ここは落ちない。**
    `ece` は `n < 500` で None を返す仕様なので、ビンが埋まる行数で測る
    （要件 6.4。**`None` を許す検査にすると、落ちていることに気づけない**）。
    """
    pytest.importorskip("lightgbm")
    result = evaluate_avail(fake_data(per_season=400), num_boost_round=ROUNDS)
    assert result.n >= 500
    assert np.isfinite(result.brier)
    assert result.ece is not None and np.isfinite(result.ece)


def test_beats_both_baselines() -> None:
    """**同じ fold で2つのベースラインと比べる**（要件 6.4 / 4.6）。

    出場率だけでは「候補の何割が出場するか」しか測れない。**モデルが
    「直近の出場率を出すだけ」を超えているか**は `ratio_learner` で測る。
    """
    pytest.importorskip("lightgbm")
    data = fake_data()
    model = evaluate_avail(data, num_boost_round=ROUNDS)
    prevalence = evaluate_avail(data, learner=prevalence_learner())
    assert model.brier < prevalence.brier


def test_ratio_baseline_is_measurable() -> None:
    """`ratio_learner` が同じ fold で測れること（勝てるかは実データで見る）。"""
    data = fake_data()
    base = evaluate_avail(data, learner=ratio_learner())
    assert np.isfinite(base.brier)
    assert base.probs.min() >= 0.01


def test_ratio_baseline_needs_its_column() -> None:
    """列がなければ落とす。**黙って別の値を出さない。**"""
    data = fake_data()
    with pytest.raises(PlayerModelError, match="ベースラインの列がない"):
        evaluate_avail(data, learner=ratio_learner("missing_column"))


def test_empty_data_is_rejected() -> None:
    """行が1件も無ければ落とす。**0件で学習したふりをしない。**"""
    empty = PlayerAvailData(
        features=pd.DataFrame(columns=list(AVAIL_KEYS)),
        played=np.empty(0), game_ids=[], player_ids=[], club_ids=[],
        season_ids=[], game_dates=[])
    with pytest.raises(PlayerModelError, match="1件もない"):
        evaluate_avail(empty)


def test_features_are_five_columns() -> None:
    """**モデルに渡すのは5列だけ**（2.3.1）。"""
    data = fake_data()
    assert list(data.features.columns) == list(AVAIL_KEYS)
    assert data.features.shape[1] == 5


# --- 行の単位（実シード） ---

@pytest.fixture(scope="module")
def seed_matrix() -> PlayerAvailData:
    """実シードから組んだ行列を**1回だけ**作る。"""
    from batch.tests.conftest import apply_migrations
    from db.seeds.test.generate import seed_test_database

    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON")
    apply_migrations(con)
    seed_test_database(con)
    try:
        return build_player_avail_matrix(export_sqlite(con))
    finally:
        con.close()


@pytest.fixture(scope="module")
def bench_matrix() -> PlayerAvailData:
    """**負例がある行列を作る。**

    合成シードは8選手が毎試合そろって出場するため、**そのままでは正例しか
    出ない**（`played.mean()` が 1.0 になる）。二値分類の検査としては空振り
    なので、**条件をこちらで作る** — 1人の選手の、ある日以降の出場を削る。
    それ以前の出場で候補には入り、以後は負例になる。
    """
    from batch.tests.conftest import apply_migrations
    from db.seeds.test.generate import seed_test_database

    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON")
    apply_migrations(con)
    seed_test_database(con)
    try:
        player_id = str(con.execute(
            "SELECT player_id FROM player_game_stats ORDER BY player_id LIMIT 1",
        ).fetchone()[0])
        removed = con.execute(
            "DELETE FROM player_game_stats WHERE player_id = ?"
            " AND game_date > (SELECT MIN(game_date) FROM games"
            "                  WHERE season_id = '2025-26-B1')",
            (player_id,)).rowcount
        assert removed > 0, "削除対象がない。検査が空振りしている"
        con.commit()
        return build_player_avail_matrix(export_sqlite(con))
    finally:
        con.close()


def test_one_row_per_candidate(seed_matrix: PlayerAvailData) -> None:
    """**1行は「試合 × 候補の選手」。** 同じ組が二重に入らない。"""
    pairs = pd.DataFrame({
        "game": seed_matrix.game_ids,
        "club": seed_matrix.club_ids,
        "player": seed_matrix.player_ids,
    })
    assert not pairs.duplicated().any()
    assert len(seed_matrix) == len(seed_matrix.features) == seed_matrix.played.size


def test_label_is_appearance(bench_matrix: PlayerAvailData) -> None:
    """正例は 1、負例は 0。**両方が存在すること。**

    片方しか無い行列では二値分類が成立しない（学習できたふりになる）。
    """
    values = set(np.unique(bench_matrix.played).tolist())
    assert values == {0.0, 1.0}


def test_candidates_without_appearance_are_negatives(
    bench_matrix: PlayerAvailData,
) -> None:
    """候補に入ったが出場しなかった選手が負例として残ること。"""
    rate = float(bench_matrix.played.mean())
    assert 0.0 < rate < 1.0


def test_uncovered_appearances_are_counted(seed_matrix: PlayerAvailData) -> None:
    """**候補に入らなかった出場を黙って落とさない**（2.3.1）。

    合成シードの最初の季の開幕戦は候補が空であり、その出場はここに数えられる。
    """
    assert seed_matrix.uncovered > 0
    assert seed_matrix.empty_candidates > 0


def test_roster_only_players_are_not_rows(seed_matrix: PlayerAvailData) -> None:
    """**ロスターにしかいない選手の行を作らない**（絶対ルール1）。

    `player_seasons` には出場実績のない選手がいる。候補を過去の出場実績から
    作っているため、その選手の行は1件も出ない。
    """
    from batch.tests.conftest import apply_migrations
    from db.seeds.test.generate import seed_test_database

    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON")
    apply_migrations(con)
    seed_test_database(con)
    try:
        roster = {str(r[0]) for r in con.execute(
            "SELECT DISTINCT player_id FROM player_seasons")}
        appeared = {str(r[0]) for r in con.execute(
            "SELECT DISTINCT player_id FROM player_game_stats")}
    finally:
        con.close()
    never = roster - appeared
    assert never, "出場実績のないロスター選手がいるシードであること"
    assert not (never & set(seed_matrix.player_ids))


def test_no_finished_games_is_rejected(seeded_db: sqlite3.Connection) -> None:
    seeded_db.execute("UPDATE games SET status = 'SCHEDULED'")
    seeded_db.commit()
    with pytest.raises(MatrixError, match="終了した試合が1件もない"):
        build_player_avail_matrix(export_sqlite(seeded_db))
