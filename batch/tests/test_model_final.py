"""最終当てはめと `model_versions` の行（`batch/model/final.py`。詳細設計 4.5.1）。

**D1 に触らない。** 合成した学習行列と評価結果だけで検証する。LightGBM は
`train_final` を差し替えて避ける（macOS では `libomp` が要る）。
"""
from __future__ import annotations

import json
from typing import Any, cast

import numpy as np
import pandas as pd
import pytest

from batch.model import final
from batch.model.dataset import TrainingData
from batch.model.evaluate import Evaluation, Fold
from batch.model.final import RateEvaluations, team_rate_targets, version_of
from batch.model.registry import load_logistic, load_logistic_means


def fake_data(per_season: int = 40,
              seasons: tuple[str, ...] = ("s1", "s2", "s3")) -> TrainingData:
    rng = np.random.default_rng(5)
    season_ids = [s for s in seasons for _ in range(per_season)]
    n = len(season_ids)
    elo = rng.normal(0, 120, n)
    home_win = (rng.random(n) < 1.0 / (1.0 + np.exp(-elo / 180.0))).astype(float)
    return TrainingData(
        features=pd.DataFrame({"elo_diff": elo, "rest_days_diff": np.zeros(n) + 1.0}),
        home_win=home_win,
        margin=elo / 10.0 + rng.normal(0, 8, n),
        total=rng.normal(160, 12, n),
        game_ids=[f"g{i}" for i in range(n)],
        season_ids=season_ids,
        game_dates=[f"2020-01-{i % 28 + 1:02d}" for i in range(n)],
        spectator_restricted=[None] * n,
    )


def fold(season: str, best_iteration: int, probs: np.ndarray,
         actual: np.ndarray) -> Fold:
    return Fold(
        test_season=season, train_seasons=(), valid_season="",
        n_train=0, n_valid=0, n_test=len(probs),
        best_iteration=best_iteration, probs=probs, actual=actual,
    )


def binary(seasons: tuple[str, ...] = ("s2", "s3")) -> Evaluation:
    actual = np.array([1.0, 0.0, 1.0, 0.0])
    probs = np.array([0.7, 0.3, 0.6, 0.4])
    return Evaluation(folds=tuple(fold(s, 80, probs, actual) for s in seasons))


def regression(scale: float = 10.0,
               seasons: tuple[str, ...] = ("s2", "s3")) -> Evaluation:
    actual = np.array([5.0, -5.0, 2.0, -2.0]) * scale
    probs = np.array([4.0, -6.0, 3.0, -1.0]) * scale
    return Evaluation(folds=tuple(fold(s, 80, probs, actual) for s in seasons))


def evaluations(**kwargs: Evaluation) -> final.Evaluations:
    base = {
        "winner": binary(), "home": binary(), "elo": binary(),
        "margin": regression(), "total": regression(),
    }
    base.update(kwargs)
    return final.Evaluations(**base, ece_floor=0.03,
                             null_rates={"elo_diff": 0.0})


@pytest.fixture(autouse=True)
def no_lightgbm(monkeypatch: pytest.MonkeyPatch) -> None:
    """LightGBM を呼ばない。**artifact の中身はここでの検証対象ではない。**"""
    def fake_train_final(features: pd.DataFrame, actual: np.ndarray,
                         weights: np.ndarray, *, num_boost_round: int,
                         params: dict[str, object] | None = None) -> tuple[object, str]:
        return object(), f"tree\nrounds={num_boost_round}\n"

    monkeypatch.setattr(final, "train_final", fake_train_final)


# --- 版 ---

def test_version_follows_the_ddl_example() -> None:
    """DDL のコメントが挙げる `'winner-v1.0.0'` の形に従う（詳細設計 1.6）。"""
    assert final.version_of("WINNER") == "winner-v1.0.0"
    assert final.version_of("MARGIN", "1.2.3") == "margin-v1.2.3"


# --- 3本（WINNER / MARGIN / TOTAL） ---

def test_exactly_three_models_are_registered() -> None:
    """**TEAM_RATE と PLAYER_MIN は登録しない**（詳細設計 4.5.1）。

    個人スタッツは第1段・第3段が組めず整合化まで到達しないため、保存する値が
    作れない。登録だけ先に済ませると `prediction_model_bundle` に「使っていない
    モデル」が並び、どの値がどのモデルから出たのか辿れなくなる。
    """
    records = final.build_records(fake_data(), evaluations())
    assert [r.model_type for r in records] == ["WINNER", "MARGIN", "TOTAL"]


def test_sigma_is_only_on_the_margin_row() -> None:
    """σ は Margin 回帰の残差であって勝率モデルの属性ではない（詳細設計 4.5.1）。"""
    by_type = {r.model_type: r for r in final.build_records(fake_data(), evaluations())}
    assert by_type["MARGIN"].margin_sigma is not None
    assert by_type["MARGIN"].margin_sigma > 0
    assert by_type["WINNER"].margin_sigma is None
    assert by_type["TOTAL"].margin_sigma is None


def test_win_prob_source_stays_winner() -> None:
    """経路は「Winner 直接」のまま。実装が変わったことは `algo` が持つ（1.5）。"""
    by_type = {r.model_type: r for r in final.build_records(fake_data(), evaluations())}
    assert by_type["WINNER"].win_prob_source == "WINNER"
    assert by_type["WINNER"].algo == "logistic"
    assert by_type["MARGIN"].win_prob_source is None


def test_regression_rows_do_not_carry_a_brier() -> None:
    """**列の名前と中身を食い違わせない。** MAE を `cv_brier` に入れない。

    入れると `/internal/metrics/active` を読む側が Brier だと思って比較する。
    """
    by_type = {r.model_type: r for r in final.build_records(fake_data(), evaluations())}
    for model_type in ("MARGIN", "TOTAL"):
        assert by_type[model_type].cv_brier is None
        assert "MAE" in (by_type[model_type].notes or "")
    assert by_type["WINNER"].cv_brier is not None


# --- artifact ---

def test_the_winner_artifact_round_trips_with_the_column_order() -> None:
    """**係数と列名の対応が評価と本番で一致すること**（`load_logistic` が照合する）。"""
    data = fake_data()
    winner = final.build_records(data, evaluations())[0]
    assert winner.artifact_text is not None
    model = load_logistic(winner.artifact_text, list(data.features.columns))
    assert model.coefficients.size == data.features.shape[1]
    # 係数の順序が列の順序と一致している（Elo が効く合成データなので符号も効く）
    assert model.coefficients[0] > 0


def test_the_winner_artifact_carries_the_unweighted_column_means() -> None:
    """**根拠の寄与と `base_value` が学習データの平均を要する**（詳細設計 2.7.1）。

    平均は推論時に作り直せない（学習行列は全期間の特徴量生成を要する）。
    **重み付き平均にしない** — 時間減衰 λ を変えるたびに `base_value` の意味が
    動くためである。
    """
    data = fake_data()
    winner = final.build_records(data, evaluations())[0]
    assert winner.artifact_text is not None
    means = load_logistic_means(winner.artifact_text, list(data.features.columns))
    expected = data.features.to_numpy(dtype=float).mean(axis=0)
    assert np.allclose(means, expected)


def test_the_params_record_the_convergence_settings() -> None:
    """乱数を使わないモデルでも収束条件を残す（詳細設計 4.7）。"""
    winner = final.build_records(fake_data(), evaluations())[0]
    assert winner.params["l2"] == pytest.approx(1e-4)
    assert winner.params["tolerance"] == pytest.approx(1e-8)
    assert "time_decay_lambda" in winner.params
    # JSON にできること（`payload_of` が `json.dumps` する）
    json.dumps(winner.params)


def test_the_windows_are_recorded() -> None:
    """`train_range` は学習の全期間、`eval_window` はテスト fold の範囲。"""
    records = final.build_records(fake_data(), evaluations())
    assert records[0].train_range == "s1..s3"
    assert records[0].eval_window == "s2..s3"
    assert records[0].train_rows == 120


# --- 落とすべきところで落ちる ---

def test_zero_rounds_is_not_rounded_up() -> None:
    """**`best_iteration` の中央値0を黙って1に繰り上げない**（詳細設計 4.5.1）。

    木が1本も育たなかったモデルを登録すると、推論は通るのに予測が定数になる。
    """
    actual = np.array([5.0, -5.0])
    zero = Evaluation(folds=(fold("s2", 0, actual, actual),))
    with pytest.raises(final.FinalFitError):
        final.build_records(fake_data(), evaluations(margin=zero))


def test_a_non_positive_sigma_is_rejected() -> None:
    """σ が正の有限値でなければ落とす（要件 6.1 / `train_score` と同じ扱い）。"""
    flat = np.array([3.0, 3.0, 3.0, 3.0])
    degenerate = Evaluation(folds=(fold("s2", 80, flat, flat),))
    with pytest.raises(final.FinalFitError):
        final.build_records(fake_data(), evaluations(margin=degenerate))


# ---------------------------------------------------------------------------
# 30本（詳細設計 4.5.1。v1.114）
# ---------------------------------------------------------------------------


def test_the_version_carries_the_target() -> None:
    """**`target` を持つ28本は版に `target` を挟む**（詳細設計 4.5.1）。

    `model_versions.version` は主キーであり、`TEAM_RATE` の14本は `model_type`
    が同じである。14本すべてが `team_rate-v1.0.0` になると、**1本目の登録で
    以後13本が主キー違反で落ちる。**
    """
    assert version_of("TEAM_RATE", "1.0.0", "fg2a") == "team_rate-fg2a-v1.0.0"
    assert version_of("PLAYER_RATE", "1.2.3", "fg3_pct") == (
        "player_rate-fg3_pct-v1.2.3")
    # `target` を持たないものは従来どおり
    assert version_of("WINNER", "1.0.0") == "winner-v1.0.0"
    assert version_of("PLAYER_AVAIL", "1.0.0", "") == "player_avail-v1.0.0"


def test_every_target_gets_its_own_version() -> None:
    """14本の版が重複しない（主キーが衝突しないことの直接の検査）。"""
    versions = {version_of("TEAM_RATE", "1.0.0", t) for t in team_rate_targets()}
    assert len(versions) == 14


def test_the_targets_come_from_the_feature_module() -> None:
    """**14項目の出どころは `features/team_rate.py` が正**（4.5.1）。

    `final.py` に写しを持つと、項目を増やしたときに片方だけが古くなる。
    """
    from batch.features.team_rate import TARGETS

    assert team_rate_targets() == tuple(TARGETS)


def test_missing_evaluations_are_named() -> None:
    """**14本が揃っていなければ登録しない**（4.5.1）。黙って少なく登録しない。"""
    evaluation = cast("Any", object())
    partial = RateEvaluations(
        team_rate={"fg2a": evaluation}, team_rate_baseline={"fg2a": evaluation},
        avail=evaluation, avail_baseline=evaluation,
        minutes=evaluation, minutes_baseline=evaluation,
        player_rate={}, player_rate_baseline={},
    )
    absent = partial.missing()
    # TEAM_RATE は13本、PLAYER_RATE は14本足りない
    assert len(absent) == 27
    assert "TEAM_RATE/fg2a" not in absent
    assert "PLAYER_RATE/fg2a" in absent
