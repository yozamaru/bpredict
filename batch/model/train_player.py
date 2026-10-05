"""選手モデル（基本設計 2.3 / 詳細設計 2.3.1 / 4.5）。

**4段すべてが揃っている。** 本モジュールは第1〜3段を持つ。

| 段 | 状態 |
|---|---|
| 第1段 PlayerAvail | **本モジュール。** `P(出場)`。候補は過去の出場実績から作る（2.3.1） |
| 第2段 PlayerMinutes | **本モジュール。** `E[出場時間 | 出場]` |
| 第3段 PlayerRates | **本モジュール。** カウント11項目は per-minute、成功率3項目は率（2.3.1） |
| 第4段 Reconciliation | `batch/model/reconcile.py` |

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

from batch.features import player_rate, team_rate
from batch.model.calibrate import Platt, fit_platt
from batch.model.dataset import (
    PlayerAvailData,
    PlayerMinutesData,
    PlayerRateData,
)
from batch.model.evaluate import Evaluation, walk_forward
from batch.model.params import (
    NUM_BOOST_ROUND_MAX,
    PLAYER_MAX_FOLDS,
    SCORE_PARAMS,
    WINNER_PARAMS,
)
from batch.model.train_score import learn_score
from batch.model.train_winner import learn_winner, train_final

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
    data: PlayerMinutesData, *, max_folds: int = PLAYER_MAX_FOLDS,
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
    data: PlayerMinutesData, *, max_folds: int = PLAYER_MAX_FOLDS,
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
    data: PlayerAvailData, *, max_folds: int = PLAYER_MAX_FOLDS,
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


# ---------------------------------------------------------------------------
# 第3段（PlayerRates）
# ---------------------------------------------------------------------------

#: シュリンクの `k` のグリッドと初期値（詳細設計 2.3.1。運営者の判断で探索する）。
#: **項目ごとに決める** — 1試合の試投数が FT と 3P で桁が違う
SHRINK_K_GRID: tuple[float, ...] = (10.0, 20.0, 40.0, 80.0)
SHRINK_K_INITIAL = 20.0

#: 実データで探索して選んだ `k`（2026-10-05。詳細設計 2.3.1）。
#: **実現値（`made / att`）に対する MAE が最小のもの**を項目ごとに採った —
#: シュリンク済みの目的変数に対する MAE は `k` を上げるだけで下がるため使えない。
#: **3項目すべてグリッドの端が選ばれた**（最適は外側にある可能性がある）。
#: 差は MAE の 0.3〜1.4% であり、グリッドは広げない（運営者が決めた探索範囲）
SHRINK_K: dict[str, float] = {"fg2_pct": 10.0, "fg3_pct": 80.0, "ft_pct": 10.0}


def shrink_k_for(target: str) -> float:
    """その項目の `k`。**カウント11項目は `k` を使わない**（目的変数に入らない）。"""
    if target not in SHRINK_K:
        raise PlayerModelError(f"成功率3項目にない: {target}")
    return SHRINK_K[target]


def shrink_column(
    made: Floats, attempts: Floats, prior: Floats, k: float,
) -> Floats:
    """`(made + k × prior) / (attempts + k)` を列に当てる（要件 6.8.4）。

    **出力は `clip(0.01, 0.99)` に収める**（ロジット変換の定義域。2.4）。
    `made` / `attempts` / `prior` のいずれかが NaN の行は NaN のまま残す
    （既定値で埋めない。規約5）。
    """
    if k <= 0:
        raise PlayerModelError("シュリンクの k が正でない")
    value = (made + k * prior) / (attempts + k)
    return np.clip(value, 0.01, 0.99)


def rate_model_features(
    data: PlayerRateData, target: str, k: float,
) -> pd.DataFrame:
    """その項目のモデルに渡す列を作る（2.3.1）。

    **成功率はここで初めて `k` を当てる。** 行列は生の合計で持っており、
    `k` は探索の対象だからである。**`pred_minutes` は NaN のまま**で、
    fold ごとに第2段が埋める。
    """
    columns = player_rate.rate_model_keys(target)
    frame = data.features
    if target not in {name for name, _, _ in team_rate.PCT_TARGETS}:
        return frame[list(columns)]

    prior = frame[f"{target}_prior"].to_numpy(dtype=np.float64)
    built = frame[[c for c in columns if not c.endswith(("_shrunk_l10", "_shrunk_season"))]].copy()
    built[f"{target}_shrunk_l10"] = shrink_column(
        frame[f"{target}_made_l10"].to_numpy(dtype=np.float64),
        frame[f"{target}_att_l10"].to_numpy(dtype=np.float64), prior, k)
    built[f"{target}_shrunk_season"] = shrink_column(
        frame[f"{target}_made_season"].to_numpy(dtype=np.float64),
        frame[f"{target}_att_season"].to_numpy(dtype=np.float64), prior, k)
    return built[list(columns)]


def rate_target(data: PlayerRateData, target: str, k: float) -> Floats:
    """目的変数。**カウントは per-minute、成功率はシュリンク済みの率**（2.3.1）。"""
    if target in team_rate.COUNT_TARGETS:
        # カウントのまま学習すると出場時間の分散に支配される（要件 6.8.4）
        return np.asarray(
            data.counts[target].to_numpy(dtype=np.float64) / data.minutes,
            dtype=np.float64)
    if target not in {name for name, _, _ in team_rate.PCT_TARGETS}:
        raise PlayerModelError(f"目的変数が14項目にない: {target}")
    return shrink_column(
        data.shots[f"{target}_made"].to_numpy(dtype=np.float64),
        data.shots[f"{target}_att"].to_numpy(dtype=np.float64),
        data.features[f"{target}_prior"].to_numpy(dtype=np.float64), k)


def realized_pct(data: PlayerRateData, target: str) -> Floats:
    """実現した成功率（`made / att`）。**`k` に依らない固定の物差し**（2.3.1）。

    シュリンク済みの目的変数は `k` を上げると分散が潰れ、**MAE が必ず下がる**。
    `k` の選定はこちらに対して測る — **整合化が最終的に食わせる量そのもの**で
    あり（`made = pct × att`）、要件 6.8.6 が誤差を実績に対して定義しているのと
    同じ向きである。
    """
    if target not in {name for name, _, _ in team_rate.PCT_TARGETS}:
        raise PlayerModelError(f"成功率3項目にない: {target}")
    made = data.shots[f"{target}_made"].to_numpy(dtype=np.float64)
    attempts = data.shots[f"{target}_att"].to_numpy(dtype=np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.asarray(made / attempts, dtype=np.float64)


def rate_weights(data: PlayerRateData, target: str) -> Floats | None:
    """成功率3項目の学習重みは**その試合の試投数**（要件 6.8.4）。"""
    if target in team_rate.COUNT_TARGETS:
        return None
    return np.asarray(
        data.shots[f"{target}_att"].to_numpy(dtype=np.float64), dtype=np.float64)


def usable_rate_rows(data: PlayerRateData, target: str, k: float) -> Floats:
    """学習に使える行（目的変数と重みが有限で、成功率は `試投数 > 0`）。

    **4.5 の擬似コードが `X[df[att] > 0]` で絞っている**のに従う。成功率の
    `prior` が NaN（データ上の最初の季。2.3.1）の行もここで落ちる。
    """
    y = rate_target(data, target, k)
    keep = np.isfinite(y)
    weights = rate_weights(data, target)
    if weights is not None:
        keep &= np.isfinite(weights) & (weights > 0)
    return keep


def rate_training_features(
    data: PlayerRateData, target: str, *, k: float,
    minutes_for: Callable[[pd.DataFrame], Floats],
) -> pd.DataFrame:
    """第3段の1項目が**実際に学習する**列（2.3.1）。

    **`pred_minutes` を埋めるところまで含める。** 行列はこの列を NaN で持つため
    （`rate_model_features` の注記）、埋める前の表を採用判定に渡すと
    **欠損率100%・定数列**として必ず落ちる — 本番では第2段の出力が必ず入る。

    **最終当てはめと採用判定が同じ表を見るように、1か所に置く。** 別に組むと、
    判定した表と学習した表が食い違う（`fit_final_rate` が通る道である）。

    `data` は**絞り込み済み**の行列であること（`usable_rate_rows` を通した後）。
    """
    if target not in team_rate.TARGETS:
        raise PlayerModelError(f"目的変数が14項目にない: {target}")
    features = rate_model_features(data, target, k).copy()
    features["pred_minutes"] = minutes_for(data.minutes_features)
    # 列順は `rate_model_keys` が正（`pred_minutes` を末尾に足したままにしない）
    return features[list(player_rate.rate_model_keys(target))]


class MinutesProvider:
    """**fold ごとに1本の第2段を当てはめ、14本すべてで共有する**（2.3.1）。

    本番では登録済みの第2段が1本だけあり（4.5.1）、14本の第3段はその同じ出力を
    受け取る。**項目ごとに別の第2段を当てはめると、学習時だけ14本の第2段が
    存在することになり、本番と入力の作り方が食い違う。**

    当てはめには**目的変数ごとの絞り込みを掛ける前の行**を使う。絞り込みは
    第3段の目的変数の都合（`試投数 > 0` など）であって、出場時間の予測には
    関係がない。

    `data` は**絞り込み前の行列**（`build_player_rate_matrix` の戻り値）であること。
    """

    def __init__(
        self, data: PlayerRateData, *,
        num_boost_round: int = NUM_BOOST_ROUND_MAX,
    ) -> None:
        self._features = data.minutes_features
        self._minutes = pd.Series(data.minutes, index=data.minutes_features.index)
        self._season = pd.Series(data.season_ids, index=data.minutes_features.index)
        self._rounds = num_boost_round
        self._cache: dict[
            tuple[tuple[str, ...], str], Callable[[pd.DataFrame], Floats]] = {}

    def for_fold(
        self, train_index: pd.Index, valid_index: pd.Index,
    ) -> Callable[[pd.Index], Floats]:
        """その fold の第2段を返す。**季の組で記憶する**（14本で1本に揃う）。"""
        train_seasons = tuple(sorted(set(self._season.loc[train_index])))
        valid_seasons = sorted(set(self._season.loc[valid_index]))
        if len(valid_seasons) != 1:
            raise PlayerModelError(
                f"検証の季が1つでない: {valid_seasons}")
        key = (train_seasons, valid_seasons[0])
        if key not in self._cache:
            train = self._season.isin(train_seasons).to_numpy()
            valid = (self._season == valid_seasons[0]).to_numpy()
            predict, _ = learn_minutes(num_boost_round=self._rounds)(
                self._features[train],
                self._minutes[train].to_numpy(dtype=np.float64),
                np.ones(int(train.sum())),
                self._features[valid],
                self._minutes[valid].to_numpy(dtype=np.float64),
            )
            self._cache[key] = predict

        predict = self._cache[key]

        def minutes_for(index: pd.Index) -> Floats:
            return predict(self._features.loc[index])

        return minutes_for


def learn_rate(
    provider: MinutesProvider, target: str, *,
    num_boost_round: int = NUM_BOOST_ROUND_MAX,
) -> Callable[..., tuple[Callable[[pd.DataFrame], Floats], int]]:
    """第3段の学習関数。**fold ごとに第2段を当てはめて `pred_minutes` を埋める。**

    当該試合の実際の出場時間を入れることは規約4（対象試合自身を参照しない）に
    直接違反し、全データで当てはめた第2段を使うと fold を跨いで情報が入る
    （2.3.1）。**学習季の行だけで第2段を当てはめる。**

    索引の値が元の行の位置を指していること（`PlayerRateData.subset` が
    振り直さない）。
    """
    if target not in team_rate.TARGETS:
        raise PlayerModelError(f"目的変数が14項目にない: {target}")
    bounds = (0.01, 0.99) if target not in team_rate.COUNT_TARGETS else None

    def learn(
        train_x: pd.DataFrame, train_y: Floats, train_w: Floats,
        valid_x: pd.DataFrame, valid_y: Floats,
    ) -> tuple[Callable[[pd.DataFrame], Floats], int]:
        minutes_for = provider.for_fold(train_x.index, valid_x.index)

        def filled(features: pd.DataFrame) -> pd.DataFrame:
            out = features.copy()
            out["pred_minutes"] = minutes_for(features.index)
            return out

        predict, best = learn_score(
            filled(train_x), train_y, train_w, filled(valid_x), valid_y,
            num_boost_round=num_boost_round)

        def bounded(features: pd.DataFrame) -> Floats:
            values = predict(filled(features))
            if bounds is not None:
                # 成功率は `[0.01, 0.99]`（要件 6.8.4。整合化が logit を通る）
                return np.clip(values, *bounds)
            # **カウントのレートは負を出さない**（回帰は負を出しうる）
            return np.maximum(values, 0.0)

        return bounded, best

    return learn


def rate_recent_learner(target: str) -> Callable[
        ..., tuple[Callable[[pd.DataFrame], Floats], int]]:
    """**「直近10試合の水準をそのまま出す」ベースライン。**

    第2段の実測で「モデルの上積みは 0.60% しかない」と分かったのは、平均では
    なくこのベースラインと比べたからである（2.3.1）。**学習しない。**
    """
    column = (
        f"{target}_per_min_l10" if target in team_rate.COUNT_TARGETS
        else f"{target}_shrunk_l10"
    )

    def learn(
        train_x: pd.DataFrame, train_y: Floats, train_w: Floats,
        valid_x: pd.DataFrame, valid_y: Floats,
    ) -> tuple[Callable[[pd.DataFrame], Floats], int]:
        fallback = float(np.average(train_y, weights=train_w))

        def predict(features: pd.DataFrame) -> Floats:
            values = features[column].to_numpy(dtype=np.float64).copy()
            # **欠損は学習季の平均で埋める**（ベースラインを落とさないため）
            values[~np.isfinite(values)] = fallback
            return values

        return predict, 0

    return learn


def evaluate_rate(
    data: PlayerRateData, target: str, *, k: float = SHRINK_K_INITIAL,
    max_folds: int = PLAYER_MAX_FOLDS, num_boost_round: int = NUM_BOOST_ROUND_MAX,
    learner: Callable[..., tuple[Callable[[pd.DataFrame], Floats], int]] | None = None,
    provider: MinutesProvider | None = None,
    realized: bool = False,
) -> Evaluation:
    """第3段の1項目を walk-forward で評価する。**分割器を2つ作らない。**

    `provider` を渡すと第2段の当てはめを使い回す（14本 × `k` のグリッドで
    同じ fold を何度も当てはめ直さないため）。省略すると `data` から作る。

    `realized=True` のとき、成功率3項目を**実現値（`made / att`）に対して**
    評価する。**`k` を比べるときは必ずこれを使う** — シュリンク済みの目的変数に
    対する MAE は `k` を上げるだけで下がり、比較にならない（2.3.1）。
    """
    frame = data.subset(usable_rate_rows(data, target, k))
    features = rate_model_features(frame, target, k)
    if learner is None:
        learner = learn_rate(
            provider or MinutesProvider(data, num_boost_round=num_boost_round),
            target, num_boost_round=num_boost_round)
    # **`k` の比較は実現値に対して行う**（2.3.1）。学習の目的変数は変えない
    against_realized = (
        realized_pct(frame, target)
        if realized and target not in team_rate.COUNT_TARGETS else None
    )
    return walk_forward(
        frame.as_training_data(features), learner,
        target=rate_target(frame, target, k),
        eval_target=against_realized,
        weights=rate_weights(frame, target),
        max_folds=max_folds,
    )


# ---------------------------------------------------------------------------
# 最終当てはめ（詳細設計 4.5.1）
# ---------------------------------------------------------------------------
#
# **評価とまったく同じ絞り込み・重み・列を使う。** 別に組むと、登録したモデルが
# 評価したモデルと違うものになる。
#
# **丸め（`clamp_avail_prob` / `clip_minutes` / 成功率のクリップ）は artifact に
# 入らない。** LightGBM のテキストにそんな層はなく、推論側が同じ関数を通す
# （`batch/model/predict.py`）。


def fit_final_avail(
    data: PlayerAvailData, *, rounds: int,
) -> tuple[str, int, list[str]]:
    """第1段を全データで当てはめ、`(artifact, 行数, 列)` を返す。

    **二値分類は `WINNER_PARAMS` を使う**（`learn_avail` と同じ。4.7 は分類用の
    木の形を1つしか定めていない）。
    """
    if len(data) == 0:
        raise PlayerModelError("学習行が1件もない")
    _, artifact = train_final(
        data.features, data.played, np.ones(len(data), dtype=np.float64),
        num_boost_round=rounds, params=WINNER_PARAMS,
    )
    return artifact, len(data), list(data.features.columns)


def fit_final_minutes(
    data: PlayerMinutesData, *, rounds: int,
) -> tuple[str, int, list[str]]:
    """第2段を全データで当てはめ、`(artifact, 行数, 列)` を返す。"""
    if len(data) == 0:
        raise PlayerModelError("学習行が1件もない")
    _, artifact = train_final(
        data.features, data.minutes, np.ones(len(data), dtype=np.float64),
        num_boost_round=rounds, params=SCORE_PARAMS,
    )
    return artifact, len(data), list(data.features.columns)


def fit_final_rate(
    data: PlayerRateData, target: str, *, k: float, rounds: int,
    minutes_for: Callable[[pd.DataFrame], Floats],
) -> tuple[str, int, list[str]]:
    """第3段の1項目を全データで当てはめ、`(artifact, 行数, 列)` を返す。

    **`pred_minutes` は「登録する第2段」の出力で埋める。** 学習時の
    `MinutesProvider` は fold ごとに当てはめ直すが（fold を跨がないため）、
    最終当てはめでは**本番と同じ1本**を使う — そうでないと、登録した第3段が
    「本番では存在しない第2段」の出力を前提にしたモデルになる。

    `minutes_for` は第2段の最終モデルの予測関数で、`MINUTES_KEYS` の列を持つ
    表を受け取る。
    """
    frame = data.subset(usable_rate_rows(data, target, k))
    if len(frame) == 0:
        raise PlayerModelError(f"{target} に使える行が1件もない")
    features = rate_training_features(frame, target, k=k, minutes_for=minutes_for)
    weights = rate_weights(frame, target)
    if weights is None:
        weights = np.ones(len(frame), dtype=np.float64)
    _, artifact = train_final(
        features, rate_target(frame, target, k), weights,
        num_boost_round=rounds, params=SCORE_PARAMS,
    )
    return artifact, len(frame), list(features.columns)
