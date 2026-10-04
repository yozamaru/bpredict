"""最終当てはめと `model_versions` の行の組み立て（詳細設計 4.5.1）。

**評価と登録を分ける。** 評価（`batch/jobs/train.py`）は何度でもやり直せるべきで、
登録は D1 の書き込み枠を使う。ここが受け取るのは**評価の結果**であり、自分では
walk-forward を回さない。

**登録するのは3本である**（WINNER / MARGIN / TOTAL）。`TEAM_RATE` と `PLAYER_MIN` は
個人スタッツの第1段・第3段が組めないため整合化まで到達せず、保存する値が作れない
（2.3.1）。**登録だけ先に済ませない** — `prediction_model_bundle` に「使っていない
モデル」が並ぶと、どの値がどのモデルから出たのか辿れなくなる。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np

from batch.model.baselines import fit_logistic
from batch.model.dataset import TrainingData, time_decay_weights
from batch.model.evaluate import Evaluation
from batch.model.params import SCORE_PARAMS, TIME_DECAY_LAMBDA_INITIAL
from batch.model.registry import ModelRecord, dump_logistic
from batch.model.train_winner import median_iteration, train_final

#: 既定の版（詳細設計 4.5.1）。**学習条件を変えたら呼び出し側が上げる。**
DEFAULT_SEMVER = "1.0.0"

#: ロジスティック回帰の当てはめの設定。`fit_logistic` の既定と一致させること。
#: **乱数を使わないモデルでも収束条件を残す**（詳細設計 4.7）。固定しないと
#: 当てはめが実行ごとに微妙に動き、採用判定が揺れる。
LOGISTIC_SETTINGS: dict[str, object] = {
    "l2": 1e-4,
    "max_iterations": 100,
    "tolerance": 1e-8,
}


class FinalFitError(RuntimeError):
    """最終当てはめを組めない。"""


@dataclass(frozen=True)
class Evaluations:
    """登録に必要な評価の結果（`Report` そのものを受け取らない）。

    `Report` を渡すと `batch.model` が `batch.jobs` に依存することになる。
    **依存の向きを逆にしない。**
    """

    winner: Evaluation
    home: Evaluation
    elo: Evaluation
    margin: Evaluation
    total: Evaluation
    ece_floor: float | None = None
    #: 列ごとの欠損率。**記録する**（詳細設計 4.5.1 の表 / 4.6 の条件5）
    null_rates: dict[str, float] | None = None


def version_of(model_type: str, semver: str = DEFAULT_SEMVER) -> str:
    """`'winner-v1.0.0'` の形（詳細設計 4.5.1 / 1.6 の DDL のコメント）。"""
    return f"{model_type.lower()}-v{semver}"


def _window(values: Sequence[str]) -> str:
    if not values:
        raise FinalFitError("期間が空である")
    return f"{values[0]}..{values[-1]}"


def _rounds(evaluation: Evaluation) -> int:
    """各 fold の `best_iteration` の中央値（基本設計 2.3）。

    **0 を黙って 1 に繰り上げない。** 木が1本も育たなかったモデルを登録すると、
    推論は通るのに予測が定数になる。
    """
    rounds = median_iteration([f.best_iteration for f in evaluation.folds])
    if rounds < 1:
        raise FinalFitError("best_iteration の中央値が1未満である")
    return rounds


def build_records(
    data: TrainingData, evaluations: Evaluations, *,
    semver: str = DEFAULT_SEMVER,
    decay: float = TIME_DECAY_LAMBDA_INITIAL,
    trained_at: str | None = None,
    league: str = "PREMIER",
) -> list[ModelRecord]:
    """3本の `ModelRecord` を作る。**D1 には触らない**（送るのは呼び出し側）。

    列の順序は `data.features.columns` をそのまま使う。評価のときの
    `logistic_learner()` も同じ順序で当てはめているため、**係数と列名の対応が
    評価と本番で一致する**（`registry.load_logistic` が照合する）。
    """
    stamp = trained_at or datetime.now(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z")
    features = list(data.features.columns)
    if not features:
        raise FinalFitError("特徴量が空である")
    weights = time_decay_weights(data.season_ids, data.seasons, lam=decay)
    train_range = _window(data.seasons)
    eval_window = _window([f.test_season for f in evaluations.winner.folds])
    sigma = evaluations.margin.residual_sigma
    if not math.isfinite(sigma) or sigma <= 0:
        raise FinalFitError("σ が正の有限値でない")

    # --- WINNER: 全特徴ロジスティック回帰（要件 6.1.1） ---
    matrix = data.features.to_numpy(dtype=np.float64)
    winner_model = fit_logistic(matrix, data.home_win, weights=weights)
    # **重みなしの列平均を artifact に入れる**（詳細設計 2.7.1）。根拠の寄与と
    # `base_value` がこれを要し、推論時には作り直せない。重み付きにすると
    # **時間減衰 λ を変えるたびに `base_value` の意味が動く**
    column_means = matrix.mean(axis=0)
    winner = ModelRecord(
        version=version_of("WINNER", semver),
        model_type="WINNER",
        algo="logistic",
        trained_at=stamp,
        train_rows=len(data),
        train_range=train_range,
        eval_window=eval_window,
        params={**LOGISTIC_SETTINGS, "time_decay_lambda": decay},
        feature_list=features,
        artifact_text=dump_logistic(winner_model, features, column_means),
        league=league,
        # **経路としては「Winner 直接」のまま**である（1.5）。実装が
        # ロジスティック回帰に変わったことは `algo` が持つ
        win_prob_source="WINNER",
        feature_null_rates=evaluations.null_rates,
        cv_accuracy=evaluations.winner.accuracy,
        cv_brier=evaluations.winner.brier,
        cv_logloss=evaluations.winner.log_loss,
        cv_ece=evaluations.winner.ece,
        baseline_home_accuracy=evaluations.home.accuracy,
        baseline_elo_brier=evaluations.elo.brier,
        notes=(
            f"ECE ノイズフロア95%点 "
            f"{'—' if evaluations.ece_floor is None else f'{evaluations.ece_floor:.4f}'}"
        ),
    )

    # --- MARGIN / TOTAL: LightGBM 回帰 ---
    scores = []
    for model_type, target, evaluation in (
        ("MARGIN", data.margin, evaluations.margin),
        ("TOTAL", data.total, evaluations.total),
    ):
        rounds = _rounds(evaluation)
        _, artifact = train_final(
            data.features, target, weights,
            num_boost_round=rounds, params=SCORE_PARAMS,
        )
        scores.append(ModelRecord(
            version=version_of(model_type, semver),
            model_type=model_type,
            algo="lightgbm",
            trained_at=stamp,
            train_rows=len(data),
            train_range=train_range,
            eval_window=eval_window,
            params={**SCORE_PARAMS, "num_boost_round": rounds,
                    "time_decay_lambda": decay},
            feature_list=features,
            artifact_text=artifact,
            league=league,
            # **σ は MARGIN の行だけに入れる**（詳細設計 4.5.1）。
            # Margin 回帰の残差であって勝率モデルの属性ではない
            margin_sigma=sigma if model_type == "MARGIN" else None,
            # **回帰に `cv_brier` を使わない。** 列の名前と中身が食い違うと、
            # `/internal/metrics/active` を読む側が Brier だと思って比較する
            notes=f"MAE {evaluation.mae:.4f}",
        ))
    return [winner, *scores]
