"""最終当てはめと `model_versions` の行の組み立て（詳細設計 4.5.1）。

**評価と登録を分ける。** 評価（`batch/jobs/train.py`）は何度でもやり直せるべきで、
登録は D1 の書き込み枠を使う。ここが受け取るのは**評価の結果**であり、自分では
walk-forward を回さない。

**登録するのは33本である**（WINNER / MARGIN / TOTAL / TEAM_RATE 14 / PLAYER_AVAIL /
PLAYER_MIN / PLAYER_RATE 14）。`prediction_model_bundle` が設計上の最大として
挙げている数と一致する（1.6）。

**v1.111 まで30本を登録しなかったのは、整合化まで到達できなかったからである。**
第1段（v1.107）と第3段（v1.110）が実装され4段が揃ったため、理由が消えた。
**規則そのものは残す** — 将来また段が増えたときに、整合化まで到達しないモデルを
先に登録してはならない（`prediction_model_bundle` に「使っていないモデル」が
並ぶと、どの値がどのモデルから出たのか辿れなくなる）。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np

from batch.model.baselines import fit_logistic
from batch.model.dataset import TrainingData, time_decay_weights
from batch.model.evaluate import Evaluation
from batch.model.params import SCORE_PARAMS, TIME_DECAY_LAMBDA_INITIAL, WINNER_PARAMS
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


def version_of(
    model_type: str, semver: str = DEFAULT_SEMVER, target: str = "",
) -> str:
    """`'winner-v1.0.0'` / `'team_rate-fg2a-v1.0.0'` の形（詳細設計 4.5.1）。

    **`target` を持つ28本は `target` を挟む。** `model_versions.version` は
    主キーであり、`TEAM_RATE` の14本は `model_type` が同じである。14本すべてが
    `team_rate-v1.0.0` になると、**1本目の登録で以後13本が主キー違反で落ちる。**
    """
    stem = model_type.lower()
    return f"{stem}-v{semver}" if not target else f"{stem}-{target}-v{semver}"


def _window(values: Sequence[str]) -> str:
    if not values:
        raise FinalFitError("期間が空である")
    return f"{values[0]}..{values[-1]}"


def rounds_of(evaluation: Evaluation) -> int:
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
        rounds = rounds_of(evaluation)
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


# ---------------------------------------------------------------------------
# 30本（TEAM_RATE 14 / PLAYER_AVAIL / PLAYER_MIN / PLAYER_RATE 14）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RateEvaluations:
    """30本の walk-forward 評価。`*_baseline` は**同一ウィンドウ**の
    「学習しない予測」である（詳細設計 4.6 は同一ウィンドウでの比較を求める）。

    **ベースラインは門ではない。** 回帰30本には要件 6.5 の条件1〜3 を課せない
    （Brier も ECE も確率予測の指標である）。それでも**比較は `notes` に残す** —
    門にはしないが、どのモデルが学習に値したのかは後から辿れるようにする。
    """

    team_rate: Mapping[str, Evaluation]
    team_rate_baseline: Mapping[str, Evaluation]
    avail: Evaluation
    #: 第1段のベースライン。**「直近10試合の出場率をそのまま出す」**（2.3.1）
    avail_baseline: Evaluation
    minutes: Evaluation
    minutes_baseline: Evaluation
    player_rate: Mapping[str, Evaluation]
    player_rate_baseline: Mapping[str, Evaluation]

    def missing(self) -> list[str]:
        """14項目が揃っていなければ名前を返す。**黙って少なく登録しない。**"""
        absent: list[str] = []
        for name, table in (
            ("TEAM_RATE", self.team_rate), ("PLAYER_RATE", self.player_rate),
        ):
            for target in team_rate_targets():
                if target not in table:
                    absent.append(f"{name}/{target}")
        return absent


def team_rate_targets() -> tuple[str, ...]:
    """14項目（カウント11 + 成功率3）。**`features/team_rate.py` が正。**"""
    from batch.features.team_rate import TARGETS

    return tuple(TARGETS)


def _mae_notes(evaluation: Evaluation, baseline: Evaluation, label: str) -> str:
    """回帰の `notes`。**`cv_brier` に MAE を入れない**（4.5.1）。"""
    gain = (
        (baseline.mae - evaluation.mae) / baseline.mae * 100.0
        if baseline.mae > 0 else float("nan")
    )
    return (
        f"MAE {evaluation.mae:.4f} / {label} {baseline.mae:.4f}"
        f"（上積み {gain:+.2f}%）"
    )


def build_rate_records(
    *,
    team_data: Any,
    avail_data: Any,
    minutes_data: Any,
    rate_data: Any,
    evaluations: RateEvaluations,
    shrink_k: Mapping[str, float],
    semver: str = DEFAULT_SEMVER,
    trained_at: str | None = None,
    league: str = "PREMIER",
) -> list[ModelRecord]:
    """30本の `ModelRecord` を作る。**D1 には触らない**（送るのは呼び出し側）。

    **最終当てはめはここで行う。** 評価と同じ絞り込み・重み・列を使うため、
    各 train モジュールの `fit_final*` を呼ぶ（別に組むと、登録したモデルが
    評価したモデルと違うものになる）。

    **第3段の `pred_minutes` は、ここで当てはめた第2段の出力で埋める。**
    本番と同じ1本である（4.5.1）。
    """
    from batch.model import train_player, train_team_rates

    absent = evaluations.missing()
    if absent:
        raise FinalFitError(f"評価が揃っていない: {len(absent)}本")

    stamp = trained_at or datetime.now(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z")
    records: list[ModelRecord] = []

    def regression(
        model_type: str, target: str, evaluation: Evaluation, baseline: Evaluation,
        artifact: str, rows: int, features: list[str], rounds: int,
        seasons: list[str], label: str, extra: Mapping[str, object] = {},
    ) -> ModelRecord:
        return ModelRecord(
            version=version_of(model_type, semver, target),
            model_type=model_type,
            target=target,
            algo="lightgbm",
            trained_at=stamp,
            train_rows=rows,
            train_range=_window(seasons),
            eval_window=_window([f.test_season for f in evaluation.folds]),
            params={**SCORE_PARAMS, "num_boost_round": rounds, **extra},
            feature_list=features,
            artifact_text=artifact,
            league=league,
            # **回帰に `cv_brier` を使わない**（4.5.1）。読む側が Brier だと思って比較する
            notes=_mae_notes(evaluation, baseline, label),
        )

    # --- TEAM_RATE 14本 ---
    for target in team_rate_targets():
        evaluation = evaluations.team_rate[target]
        rounds = rounds_of(evaluation)
        artifact, rows, features = train_team_rates.fit_final(
            team_data, target, rounds=rounds)
        records.append(regression(
            "TEAM_RATE", target, evaluation, evaluations.team_rate_baseline[target],
            artifact, rows, features, rounds, team_data.seasons, "平均",
        ))

    # --- PLAYER_AVAIL（二値分類。**ECE の条件は課さない**。4.5.1） ---
    avail_rounds = rounds_of(evaluations.avail)
    avail_artifact, avail_rows, avail_features = train_player.fit_final_avail(
        avail_data, rounds=avail_rounds)
    avail = evaluations.avail
    records.append(ModelRecord(
        version=version_of("PLAYER_AVAIL", semver),
        model_type="PLAYER_AVAIL",
        algo="lightgbm",
        trained_at=stamp,
        train_rows=avail_rows,
        train_range=_window(avail_data.seasons),
        eval_window=_window([f.test_season for f in avail.folds]),
        params={**WINNER_PARAMS, "num_boost_round": avail_rounds},
        feature_list=avail_features,
        artifact_text=avail_artifact,
        league=league,
        cv_accuracy=avail.accuracy,
        cv_brier=avail.brier,
        cv_logloss=avail.log_loss,
        cv_ece=avail.ece,
        # **`baseline_elo_brier` に入れない**（4.5.1）。列名が出どころを主張して
        # おり、「直近10試合の出場率」を入れるとモデル横断の比較で嘘になる
        notes=(
            f"Brier {avail.brier:.6f} / 直近10試合の出場率 "
            f"{evaluations.avail_baseline.brier:.6f}"
            f"（改善 {(evaluations.avail_baseline.brier - avail.brier) / evaluations.avail_baseline.brier * 100.0:+.1f}%）"
            f" / ECE {avail.ece:.6f}"
        ),
    ))

    # --- PLAYER_MIN（第3段の `pred_minutes` の出どころでもある） ---
    minutes_rounds = rounds_of(evaluations.minutes)
    minutes_artifact, minutes_rows, minutes_features = train_player.fit_final_minutes(
        minutes_data, rounds=minutes_rounds)
    records.append(regression(
        "PLAYER_MIN", "", evaluations.minutes, evaluations.minutes_baseline,
        minutes_artifact, minutes_rows, minutes_features, minutes_rounds,
        minutes_data.seasons, "直近5試合の平均",
    ))

    # --- PLAYER_RATE 14本 ---
    # **本番と同じ1本の第2段で `pred_minutes` を埋める**（4.5.1）
    minutes_for = minutes_predictor(minutes_artifact, minutes_features)

    for target in team_rate_targets():
        evaluation = evaluations.player_rate[target]
        rounds = rounds_of(evaluation)
        k = shrink_k.get(target)
        artifact, rows, features = train_player.fit_final_rate(
            rate_data, target, k=k or 1.0, rounds=rounds, minutes_for=minutes_for)
        records.append(regression(
            "PLAYER_RATE", target, evaluation, evaluations.player_rate_baseline[target],
            artifact, rows, features, rounds, rate_data.seasons, "直近10試合の水準",
            # **成功率は `shrink_k` も記録する** — 目的変数の定義そのものを決める値
            extra={} if k is None else {"shrink_k": k},
        ))
    return records


def minutes_predictor(
    artifact_text: str, columns: Sequence[str],
) -> Callable[[Any], Any]:
    """第2段の予測関数（4.5.1）。**採用判定と最終当てはめの両方がこれを通る。**

    `pred_minutes` は第3段の列であり、行列では NaN である（2.3.1）。採用判定は
    **本番が埋めるのと同じ値**を見なければならないため、評価の側もこの関数で埋める。

    **同じ入力・同じ params なら同じモデルになる**（`deterministic: True` と
    seed 固定。4.7）。したがって評価と登録が別々に当てはめても、埋まる値は一致する。
    """
    booster = _load_booster(artifact_text)

    def minutes_for(frame: Any) -> Any:
        return np.asarray(booster.predict(frame[list(columns)]), dtype=np.float64)

    return minutes_for


def _load_booster(artifact_text: str) -> Any:
    """LightGBM を遅延インポートして artifact から戻す（`predict.py` と同じ理由）。"""
    import lightgbm

    return lightgbm.Booster(model_str=artifact_text)
