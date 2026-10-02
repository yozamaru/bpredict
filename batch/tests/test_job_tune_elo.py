"""Elo のパラメータ探索（工程8の段1。`batch/jobs/tune_elo.py`）。

**最重要の検査は「本番の参照と一致すること」である。** 探索は速さのために
二分探索で Elo を引くが、意味が `batch/features/team_strength.py` の `elo()` と
ずれると、**探索で選んだパラメータが本番で別の値を出す**。
"""
from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from batch.features.base import Context
from batch.features.dataset import Dataset
from batch.features.team_strength import elo as production_elo
from batch.jobs import tune_elo
from batch.ratings.elo import DEFAULT_PARAMS, EloParams
from batch.ratings.params import (
    HOME_ADVANTAGE_GRID,
    K_GRID,
    PROMOTED_ELO_GRID,
    SEASON_REGRESSION_GRID,
)


def ratings_frame() -> pd.DataFrame:
    """同じクラブに複数日、同じ日に複数クラブがある形。"""
    rows = [
        ("c1", "2020-10-01", 1500.0),
        ("c1", "2020-10-05", 1512.0),
        ("c1", "2020-10-09", 1498.0),
        ("c2", "2020-10-05", 1488.0),
        ("c2", "2020-10-09", 1502.0),
    ]
    return pd.DataFrame(rows, columns=["club_id", "as_of_date", "elo"])


# --- 本番の参照との一致（これが崩れたら探索の意味がない） ---

def production_context(ratings: pd.DataFrame, game_date: str) -> Context:
    """`elo()` が使うのは `dataset` と `game_date` だけ。他は空で足りる。"""
    empty = pd.DataFrame()
    return Context(
        game_id="g",
        as_of=datetime(2020, 10, 9, tzinfo=UTC),
        game=pd.Series(dtype="object"),
        home_club_id="c1",
        away_club_id="c2",
        season_id="s1",
        game_date=game_date,
        dataset=Dataset(tables={"team_ratings": ratings}),
        finished_team_games=empty,
        finished_player_stats=empty,
        entries=empty,
    )


@pytest.mark.parametrize("club_id", ["c1", "c2", "c3"])
@pytest.mark.parametrize(
    "game_date",
    ["2020-09-30", "2020-10-01", "2020-10-02", "2020-10-05", "2020-10-06",
     "2020-10-09", "2020-10-10"],
)
def test_index_matches_the_production_lookup(club_id: str, game_date: str) -> None:
    """`as_of_date < 対象試合日` の最新行。**境界（同日）も含めて一致すること。**"""
    ratings = ratings_frame()
    index = tune_elo.EloIndex(ratings)
    context = production_context(ratings, game_date)
    assert index.at(club_id, game_date) == production_elo(context, club_id)


def test_the_same_day_is_excluded() -> None:
    """**不等号は `<` である。** 1行はその試合日の終了時点の値（詳細設計 1.4）。"""
    index = tune_elo.EloIndex(ratings_frame())
    # 10-05 の行は 10-05 の試合からは見えない（10-01 の値が返る）
    assert index.at("c1", "2020-10-05") == 1500.0
    assert index.at("c1", "2020-10-06") == 1512.0


def test_unknown_club_and_empty_table_return_none() -> None:
    """**0埋めしない。** 既定値への変換は呼び出し側で行う（特徴量の規約5）。"""
    index = tune_elo.EloIndex(ratings_frame())
    assert index.at("unknown", "2020-10-09") is None
    assert tune_elo.EloIndex(pd.DataFrame(
        columns=["club_id", "as_of_date", "elo"]),
    ).at("c1", "2020-10-09") is None


# --- 学習行列 ---

def games_frame() -> pd.DataFrame:
    rows = [
        ("g1", "s1", "2020-10-02", "2020-10-02T10:00:00Z", "c1", "c2", 90, 80),
        ("g2", "s1", "2020-10-06", "2020-10-06T10:00:00Z", "c2", "c1", 70, 85),
        ("g3", "s2", "2020-10-10", "2020-10-10T10:00:00Z", "c1", "c2", 88, 88 - 5),
    ]
    return pd.DataFrame(rows, columns=[
        "id", "season_id", "game_date", "tipoff_at",
        "home_club_id", "away_club_id", "home_score", "away_score",
    ])


def test_matrix_has_one_column_and_counts_defaults() -> None:
    data, defaulted = tune_elo.elo_only_matrix(games_frame(), ratings_frame())
    assert list(data.features.columns) == ["elo_diff"]
    assert len(data) == 3
    # g1 は 10-02 で、c2 の行は 10-05 からしかない → 既定値で埋める
    assert defaulted == 1


def test_matrix_uses_the_league_mean_for_missing_ratings() -> None:
    """欠損はリーグ平均で埋める（詳細設計 2.2 の `elo_home` / `elo_away`）。"""
    data, _ = tune_elo.elo_only_matrix(games_frame(), ratings_frame(), league_mean=1500.0)
    # g1: home c1 は 10-01 の 1500、away c2 は欠損 → 1500。差は 0
    assert data.features["elo_diff"].iloc[0] == pytest.approx(0.0)
    # g2: home c2 は 10-05 の 1488、away c1 は 10-05 の 1512。差は -24
    assert data.features["elo_diff"].iloc[1] == pytest.approx(-24.0)


def test_matrix_labels_home_wins() -> None:
    data, _ = tune_elo.elo_only_matrix(games_frame(), ratings_frame())
    assert data.home_win == pytest.approx(np.array([1.0, 0.0, 1.0]))


def test_matrix_keeps_the_time_order() -> None:
    """**並べ替えない。** 時系列分割の順序がこれで決まる。"""
    data, _ = tune_elo.elo_only_matrix(games_frame(), ratings_frame())
    assert data.game_ids == ["g1", "g2", "g3"]
    assert data.seasons == ["s1", "s2"]


# --- グリッド ---

def test_grid_comes_from_the_design_not_the_job() -> None:
    """**実装で値を決めない。** 出どころは `batch/ratings/params.py` である。"""
    combos = tune_elo.grid()
    assert len(combos) == (
        len(K_GRID) * len(HOME_ADVANTAGE_GRID)
        * len(SEASON_REGRESSION_GRID) * len(PROMOTED_ELO_GRID)
    )
    assert len(combos) == 300
    assert {c.k for c in combos} == set(K_GRID)
    assert {c.home_advantage for c in combos} == set(HOME_ADVANTAGE_GRID)
    assert {c.season_regression for c in combos} == set(SEASON_REGRESSION_GRID)
    assert {c.promoted_elo for c in combos} == set(PROMOTED_ELO_GRID)


def test_the_grid_contains_the_initial_values() -> None:
    """初期値が探索に含まれること（最良と比べる相手になる）。"""
    assert DEFAULT_PARAMS in tune_elo.grid()


def test_the_home_advantage_grid_covers_the_measurement() -> None:
    """**実測 18.8点 を挟む値があること**（要件 1.13 / 詳細設計 2.5）。

    旧グリッド `{40,55,70,85}` は実測を1つも含んでおらず、探索しても
    「最も弱いホームアドバンテージ」が常に選ばれるだけだった。
    """
    assert min(HOME_ADVANTAGE_GRID) <= 18.8 <= max(HOME_ADVANTAGE_GRID)
    assert any(ha < 18.8 for ha in HOME_ADVANTAGE_GRID)
    assert any(ha > 18.8 for ha in HOME_ADVANTAGE_GRID)


# --- 失敗の扱い ---

def test_no_finished_game_is_an_error() -> None:
    empty = pd.DataFrame(columns=["status", "home_score", "away_score"])
    with pytest.raises(tune_elo.TuneError):
        tune_elo.finished_games(Dataset(tables={"games": empty}))


def test_games_without_a_score_are_dropped() -> None:
    """スコアが欠けている `FINISHED` は落とす（`build_matrix` と同じ）。"""
    games = pd.DataFrame({
        "id": ["a", "b"], "status": ["FINISHED", "FINISHED"],
        "home_score": [80, None], "away_score": [70, 75],
        "tipoff_at": ["2020-10-01T10:00:00Z", "2020-10-02T10:00:00Z"],
    })
    picked = tune_elo.finished_games(Dataset(tables={"games": games}))
    assert list(picked["id"]) == ["a"]


# --- 報告 ---

def test_render_shows_the_initial_values_for_comparison() -> None:
    trials = [
        tune_elo.Trial(EloParams(k=12.0), brier=0.19, accuracy=0.69, n=600, defaulted=1),
        tune_elo.Trial(DEFAULT_PARAMS, brier=0.21, accuracy=0.68, n=600, defaulted=1),
    ]
    text = tune_elo.render(trials)
    assert "初期値" in text
    assert "最良との差" in text
    assert "+0.0200" in text
