"""選手モデル（基本設計 2.3 / 詳細設計 2.3.1 / 4.5）。

**いま実装してあるのは第1段と第2段である。**

| 段 | 状態 |
|---|---|
| 第1段 PlayerAvail | **本モジュール。** `P(出場)`。候補は過去の出場実績から作る（2.3.1） |
| 第2段 PlayerMinutes | **本モジュール。** `E[出場時間 | 出場]` |
| 第3段 PlayerRates | **未実装。** `k` / `prior` / `usage_l10` が未定義（2.3.1） |
| 第4段 Reconciliation | `batch/model/reconcile.py`（実装済み） |

**出場時間は非負に収める。** 回帰は負を出しうるが、`player_predictions.pred_minutes`
には `CHECK (pred_minutes >= 0)` があり（詳細設計 1.5）、整合化は出場時間を
200分へ正規化するため符号が反転すると配分が壊れる（2.4 の手順2）。

**上限は設けない。** 延長戦では40分を超える（要件 6.8.5 は `200 + 25 × 延長回数`）。
値域検証は `minutes` 0–60 を課すが（基本設計 2.1）、それは**取り込みの検証**で
あって予測の上限ではない。
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd
from numpy.typing import NDArray

from batch.model.calibrate import Platt, fit_platt
from batch.model.dataset import PlayerAvailData, PlayerMinutesData
from batch.model.evaluate import MAX_FOLDS, Evaluation, walk_forward
from batch.model.params import NUM_BOOST_ROUND_MAX
from batch.model.train_score import learn_score
from batch.model.train_winner import learn_winner

type Floats = NDArray[np.float64]


class PlayerModelError(ValueError):
    """選手モデルを学習・推論できない入力。"""


def clip_minutes(values: Floats) -> Floats:
    """出場時間を非負に収める（上限は設けない。上記）。"""
    return np.maximum(np.asarray(values, dtype=np.float64), 0.0)


def learn_minutes(
    *, num_boost_round: int = NUM_BOOST_ROUND_MAX,
) -> Callable[..., tuple[Callable[[pd.DataFrame], Floats], int]]:
    """第2段の学習関数。**木の形は Margin / Total と同じ**（4.7 は回帰用の
    別のグリッドを定めていない）。出力の丸めだけが違う。

    `num_boost_round` の既定は 4.7 の上限（1,200）である。**本番でこれを下げない。**
    """
    def learn(
        train_x: pd.DataFrame, train_y: Floats, train_w: Floats,
        valid_x: pd.DataFrame, valid_y: Floats,
    ) -> tuple[Callable[[pd.DataFrame], Floats], int]:
        predict, best = learn_score(
            train_x, train_y, train_w, valid_x, valid_y,
            num_boost_round=num_boost_round)

        def bounded(features: pd.DataFrame) -> Floats:
            return clip_minutes(predict(features))

        return bounded, best

    return learn


def mean_learner() -> Callable[..., tuple[Callable[[pd.DataFrame], Floats], int]]:
    """**ベースライン。学習データの平均をそのまま返す。**

    要件 6.4 はベースラインとの比較を求めている。**`MAE / 実績SD` で代用しない**
    （その比が 0.798 で平均と同等になるのは目的変数が正規分布のときだけで、
    出場時間は 0〜40分に収まる有界な分布である）。
    """
    def learn(
        train_x: pd.DataFrame, train_y: Floats, train_w: Floats,
        valid_x: pd.DataFrame, valid_y: Floats,
    ) -> tuple[Callable[[pd.DataFrame], Floats], int]:
        value = float(np.asarray(train_y, dtype=np.float64).mean())

        def predict(features: pd.DataFrame) -> Floats:
            return np.full(len(features), value, dtype=np.float64)

        return predict, 0

    return learn


def recent_learner(column: str = "minutes_l5_player") -> Callable[
        ..., tuple[Callable[[pd.DataFrame], Floats], int]]:
    """**もう1つのベースライン。その選手の直近N試合の平均をそのまま返す。**

    学習しない（列をそのまま出す）。**平均ベースラインだけでは足りない** —
    出場時間は選手ごとに大きく違うため、リーグ全体の平均（約19分）に対する
    改善は「選手ごとの水準を知っているか」をほとんど測っていない。
    **モデルが「直近の平均を出すだけ」を超えているかは、こちらで測る。**
    """
    def learn(
        train_x: pd.DataFrame, train_y: Floats, train_w: Floats,
        valid_x: pd.DataFrame, valid_y: Floats,
    ) -> tuple[Callable[[pd.DataFrame], Floats], int]:
        if column not in train_x.columns:
            raise PlayerModelError(f"ベースラインの列がない: {column}")

        def predict(features: pd.DataFrame) -> Floats:
            return clip_minutes(features[column].to_numpy(dtype=np.float64))

        return predict, 0

    return learn


def evaluate_minutes(
    data: PlayerMinutesData, *, max_folds: int = MAX_FOLDS,
    num_boost_round: int = NUM_BOOST_ROUND_MAX,
) -> Evaluation:
    """第2段の walk-forward 評価。**分割器を2つ作らない**（`evaluate.py`）。

    指標は `mae` である。**`brier` や `ece` を呼ばない** — 0/1 の目的変数では
    なく、`Evaluation._require_binary` が落とす。
    """
    if len(data) == 0:
        raise PlayerModelError("学習行が1件もない")
    return walk_forward(
        data.as_training_data(),
        learn_minutes(num_boost_round=num_boost_round),
        target=data.minutes,
        max_folds=max_folds,
    )


def evaluate_baseline(
    data: PlayerMinutesData, *, max_folds: int = MAX_FOLDS,
    learner: Callable[..., tuple[Callable[[pd.DataFrame], Floats], int]] | None = None,
) -> Evaluation:
    """同じ fold でベースラインを測る。**同一の分割で比べる**（4.6）。

    既定は平均（`mean_learner`）。`recent_learner()` を渡すと「直近N試合の平均を
    そのまま出す」ベースラインになる。**両方を測る** — 前者だけでは
    「選手ごとの水準を知っているか」しか見えない。
    """
    if len(data) == 0:
        raise PlayerModelError("学習行が1件もない")
    return walk_forward(
        data.as_training_data(),
        mean_learner() if learner is None else learner,
        target=data.minutes,
        max_folds=max_folds,
    )


# ---------------------------------------------------------------------------
# 第1段（PlayerAvail）
# ---------------------------------------------------------------------------

#: 表示する／しないの境目（要件 6.8.4）。**学習には使わない** — 学習は 0/1 を
#: そのまま当て、閾値は推論と表示の側の規則である
AVAIL_DISPLAY_THRESHOLD = 0.5

#: 出場確率の値域。**勝率の `[0.05, 0.95]` を使わない**（下記）。要件 6.8.4 が
#: 第3段の成功率に定めている値をそのまま使う（**新しい定数を増やさない**）
AVAIL_PROB_BOUNDS = (0.01, 0.99)


def clamp_avail_prob(prob: Floats) -> Floats:
    """出場確率を `[0.01, 0.99]` に収める。

    **勝率の `clamp_win_prob`（`[0.05, 0.95]`）を使わない。** あの幅は
    「どのチームも 95% を超えて勝つことはない」という勝敗の性質に合わせた値で、
    出場確率には合わない — **「この選手は出ない」は本当に 0.01 側の状態である**。

    実測（2026-10-05。245,392行 / テスト n=124,927）。

    | クランプ | Brier | ECE |
    |---|---:|---:|
    | `[0.05, 0.95]`（勝率の幅） | 0.042965 | 0.027177 |
    | **`[0.01, 0.99]`（採用）** | **0.042352** | **0.010875** |
    | なし | 0.042354 | 0.011187 |

    **クランプなしより良い。** 予測の分布が両端に寄るため、僅かな外挿を
    切り落とす効果がある。Accuracy は3つとも 0.9466 で変わらない
    （0.5 のどちら側かは動かない）。
    """
    lo, hi = AVAIL_PROB_BOUNDS
    return np.clip(np.asarray(prob, dtype=np.float64), lo, hi)


def learn_avail(
    *, num_boost_round: int = NUM_BOOST_ROUND_MAX,
    calibrated: bool = False,
    on_calibrator: Callable[[Platt], None] | None = None,
) -> Callable[..., tuple[Callable[[pd.DataFrame], Floats], int]]:
    """第1段の学習関数。**二値分類は `learn_winner` を使い回す**。

    基本設計 2.3 が `PlayerAvail` を「LightGBM 二値分類」と定めており、4.7 は
    分類用の木の形を1つしか定めていない（`WINNER_PARAMS`）。**ここで別の
    グリッドを作らない** — 木を深くする前に特徴量を見直す（同 4.7）。

    出力は `clamp_avail_prob` で `[0.01, 0.99]` に収める。**勝率の幅を使わない** —
    理由と実測は同関数にある。

    `calibrated=True` で **Platt scaling を fold の検証セットに当てはめる**
    （要件 6.7 / 詳細設計 2.3.1）。**学習データでは当てはめない** — 学習データ上の
    過剰な分離を「正しい」と学習し、本番では過信が増幅される。

    **クランプは較正の後に当てる。** 先にクランプすると `[0.01, 0.99]` の端に
    固まった値を較正器が引き伸ばし、端のぶんだけ目盛りが狂う。
    """
    def learn(
        train_x: pd.DataFrame, train_y: Floats, train_w: Floats,
        valid_x: pd.DataFrame, valid_y: Floats,
    ) -> tuple[Callable[[pd.DataFrame], Floats], int]:
        predict, best = learn_winner(
            train_x, train_y, train_w, valid_x, valid_y,
            num_boost_round=num_boost_round)

        if not calibrated:
            def bounded(features: pd.DataFrame) -> Floats:
                return clamp_avail_prob(predict(features))

            return bounded, best

        # **検証セットの out-of-fold 予測で当てはめる。** `valid` は early stopping と
        # 共用するが、どちらも `test` に触れないため評価は out-of-sample のまま
        platt = fit_platt(predict(valid_x), valid_y)
        if on_calibrator is not None:
            on_calibrator(platt)

        def calibrated_predict(features: pd.DataFrame) -> Floats:
            return clamp_avail_prob(platt.apply(predict(features)))

        return calibrated_predict, best

    return learn


def prevalence_learner() -> Callable[..., tuple[Callable[[pd.DataFrame], Floats], int]]:
    """**ベースライン。学習データの出場率をそのまま返す。**

    要件 6.4 はベースラインとの比較を求めている。これは「候補の何割が出場するか」
    だけを知っているモデルで、要件 6.8.4 の「登録選手の3〜4割が0分」に対応する。
    """
    def learn(
        train_x: pd.DataFrame, train_y: Floats, train_w: Floats,
        valid_x: pd.DataFrame, valid_y: Floats,
    ) -> tuple[Callable[[pd.DataFrame], Floats], int]:
        value = float(np.asarray(train_y, dtype=np.float64).mean())

        def predict(features: pd.DataFrame) -> Floats:
            return clamp_avail_prob(
                np.full(len(features), value, dtype=np.float64))

        return predict, 0

    return learn


def ratio_learner(column: str = "games_played_ratio_l10") -> Callable[
        ..., tuple[Callable[[pd.DataFrame], Floats], int]]:
    """**もう1つのベースライン。直近10試合の出場率をそのまま確率として返す。**

    学習しない（列をそのまま出す）。**出場率ベースラインだけでは足りない** —
    候補の出場率はリーグ全体でほぼ一定で、その比較は「選手ごとの水準を知って
    いるか」をほとんど測っていない。**モデルが「直近の出場率を出すだけ」を
    超えているかは、こちらで測る**（第2段の `recent_learner` と同じ役割）。
    """
    def learn(
        train_x: pd.DataFrame, train_y: Floats, train_w: Floats,
        valid_x: pd.DataFrame, valid_y: Floats,
    ) -> tuple[Callable[[pd.DataFrame], Floats], int]:
        if column not in train_x.columns:
            raise PlayerModelError(f"ベースラインの列がない: {column}")

        def predict(features: pd.DataFrame) -> Floats:
            return clamp_avail_prob(features[column].to_numpy(dtype=np.float64))

        return predict, 0

    return learn


def evaluate_avail(
    data: PlayerAvailData, *, max_folds: int = MAX_FOLDS,
    num_boost_round: int = NUM_BOOST_ROUND_MAX,
    calibrated: bool = False,
    on_calibrator: Callable[[Platt], None] | None = None,
    learner: Callable[..., tuple[Callable[[pd.DataFrame], Floats], int]] | None = None,
) -> Evaluation:
    """第1段の walk-forward 評価。**分割器を2つ作らない**（`evaluate.py`）。

    目的変数は 0/1 なので `brier` と `ece` が使える。`learner` を渡すと
    ベースラインを同じ fold で測れる（4.6 は同一ウィンドウでの比較を求める）。
    """
    if len(data) == 0:
        raise PlayerModelError("学習行が1件もない")
    return walk_forward(
        data.as_training_data(),
        learn_avail(
            num_boost_round=num_boost_round,
            calibrated=calibrated,
            on_calibrator=on_calibrator,
        ) if learner is None else learner,
        target=data.played,
        max_folds=max_folds,
    )
