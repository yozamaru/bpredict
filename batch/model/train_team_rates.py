"""チーム目標（TeamRate）の回帰14本（基本設計 2.3 / 詳細設計 2.2.1 / 4.5）。

**これは整合化の目標値を作るモデルである**（要件 6.8.5）。選手予測の合計を
ここへ寄せる。各項目を独立に生成すると約2%の確率で「FT成功数 > FT試投数」という
**実行不能な目標**が生まれ、整合化が原理的に成立しない。それを防ぐために
「試投数 ＋ 成功率」の構造で予測する。

| 区分 | 目的変数 | 学習重み | 出力 |
|---|---|---|---|
| カウント11項目 | その試合のカウント | なし | そのまま |
| 成功率3項目 | `成功数 ÷ 試投数` | **その試合の試投数** | `clip(0.01, 0.99)` |

**カウントを per-minute にしない。** 個人は出場時間の分散に支配されるため
per-minute にするが（2.3）、チームは常に200分である。テンポの違いは
`pace_own` / `pace_opp` が持つ（2.2.1）。

**この出力をそのままチーム目標にしない。** TeamRate と Margin / Total は別モデル
であり、導出した得点が予想スコアと一致する保証がない。
`reconcile_team_targets()` を通してから保存する（2.4 の前段）。
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd
from numpy.typing import NDArray

from batch.features.team_rate import COUNT_TARGETS, PCT_TARGETS, TARGETS
from batch.model.dataset import TeamRateData
from batch.model.evaluate import MAX_FOLDS, Evaluation, walk_forward
from batch.model.params import NUM_BOOST_ROUND_MAX, SCORE_PARAMS
from batch.model.train_score import learn_score
from batch.model.train_winner import train_final

type Floats = NDArray[np.float64]

#: 成功率の出力の下限と上限（詳細設計 4.5）。**ロジット変換の定義域を確保する。**
#: 整合化（2.4）が `logit()` を通すため、0 や 1 が来ると発散する。
PCT_CLIP = (0.01, 0.99)

#: 成功率3項目の名前。
PCT_NAMES: tuple[str, ...] = tuple(name for name, _, _ in PCT_TARGETS)

#: カウント11項目の名前（再掲。`features/team_rate.py` が正）。
COUNT_NAMES: tuple[str, ...] = COUNT_TARGETS


class TeamRateError(ValueError):
    """TeamRate を学習・推論できない入力。"""


def is_pct(target: str) -> bool:
    if target not in TARGETS:
        raise TeamRateError(f"目的変数が14項目にない: {target}")
    return target in PCT_NAMES


def clip_pct(values: Floats) -> Floats:
    """成功率を `[0.01, 0.99]` に収める。"""
    return np.clip(np.asarray(values, dtype=np.float64), *PCT_CLIP)


def clip_count(values: Floats) -> Floats:
    """カウントを非負に収める。

    **回帰は負を出しうる。** 試投数や被ファウル数が負の目標値は
    `prediction_team_targets` の CHECK 制約（`tgt_fg2a >= 0` ほか。詳細設計 1.5）を
    破り、整合化の比例スケールでも符号が反転する。**0 で止める。**
    """
    return np.maximum(np.asarray(values, dtype=np.float64), 0.0)


def learner_for(
    target: str, *, num_boost_round: int = NUM_BOOST_ROUND_MAX,
) -> Callable[..., tuple[Callable[[pd.DataFrame], Floats], int]]:
    """その目的変数の学習関数。**木の形は Margin / Total と同じ**（4.7 は回帰用の
    別のグリッドを定めていない）。出力の丸めだけが区分で変わる。

    `num_boost_round` の既定は 4.7 の上限（1,200）である。**本番でこれを下げない** —
    下げてよいのは「14本 × fold の総時間を測るための試し」のときだけで、
    採用判定に使う評価は必ず上限のままで行う。
    """
    pct = is_pct(target)

    def learn(
        train_x: pd.DataFrame, train_y: Floats, train_w: Floats,
        valid_x: pd.DataFrame, valid_y: Floats,
    ) -> tuple[Callable[[pd.DataFrame], Floats], int]:
        predict, best = learn_score(
            train_x, train_y, train_w, valid_x, valid_y,
            num_boost_round=num_boost_round)

        def bounded(features: pd.DataFrame) -> Floats:
            raw = predict(features)
            return clip_pct(raw) if pct else clip_count(raw)

        return bounded, best

    return learn


def mean_learner() -> Callable[..., tuple[Callable[[pd.DataFrame], Floats], int]]:
    """**ベースライン。学習データの（重み付き）平均をそのまま返す。**

    要件 6.4 はベースラインとの比較を求めている。回帰では「ホーム必勝」に当たるのが
    これである。**`MAE / 実績SD` で代用しない** — その比が 0.798 のとき平均と同等に
    なるのは目的変数が正規分布のときだけで、カウントは右に裾を持つ。

    成功率は試投数の重み付き平均を取る（学習と同じ重みを使う）。
    """
    def learn(
        train_x: pd.DataFrame, train_y: Floats, train_w: Floats,
        valid_x: pd.DataFrame, valid_y: Floats,
    ) -> tuple[Callable[[pd.DataFrame], Floats], int]:
        weights = np.asarray(train_w, dtype=np.float64)
        y = np.asarray(train_y, dtype=np.float64)
        total = float(weights.sum())
        value = float((y * weights).sum() / total) if total > 0 else float(y.mean())

        def predict(features: pd.DataFrame) -> Floats:
            return np.full(len(features), value, dtype=np.float64)

        return predict, 0

    return learn


def _usable(data: TeamRateData, target: str) -> Floats:
    """学習に使える行の真偽。

    **成功率は試投0の行を落とす。** 目的変数が NaN であり、重みも0になる。
    LightGBM は重み0の行を無視するが、目的変数の NaN は無視しない。
    """
    y = data.target(target)
    keep = ~np.isnan(y)
    weights = data.weights(target)
    if weights is not None:
        keep = keep & (weights > 0)
    return keep


def evaluate_target(
    data: TeamRateData, target: str, *, max_folds: int = MAX_FOLDS,
    num_boost_round: int = NUM_BOOST_ROUND_MAX,
) -> Evaluation:
    """1項目の walk-forward 評価。**分割器を2つ作らない**（`evaluate.py`）。

    指標は `mae` である。**`brier` や `ece` を呼ばない** — 0/1 の目的変数ではなく、
    `Evaluation._require_binary` が落とす。
    """
    keep = _usable(data, target)
    if not keep.any():
        raise TeamRateError(f"{target} に使える行が1件もない")
    subset = _subset(data, keep)
    weights = subset.weights(target)
    return walk_forward(
        subset.as_training_data(target),
        learner_for(target, num_boost_round=num_boost_round),
        target=subset.target(target),
        weights=weights,
        max_folds=max_folds,
    )


def _subset(data: TeamRateData, keep: Floats) -> TeamRateData:
    """行を絞った同じ形。**シーズンの出現順を保つ**（時系列分割がこれで決まる）。"""
    mask = np.asarray(keep, dtype=bool)
    picked = [i for i, flag in enumerate(mask) if flag]
    return TeamRateData(
        rows=data.rows.iloc[picked].reset_index(drop=True),
        actual=data.actual.iloc[picked].reset_index(drop=True),
        attempts=data.attempts.iloc[picked].reset_index(drop=True),
        game_ids=[data.game_ids[i] for i in picked],
        club_ids=[data.club_ids[i] for i in picked],
        season_ids=[data.season_ids[i] for i in picked],
        game_dates=[data.game_dates[i] for i in picked],
        club_win=data.club_win[mask],
        club_margin=data.club_margin[mask],
        total=data.total[mask],
        spectator_restricted=[data.spectator_restricted[i] for i in picked],
    )


def evaluate_baseline(
    data: TeamRateData, target: str, *, max_folds: int = MAX_FOLDS,
) -> Evaluation:
    """同じ fold で平均ベースラインを測る。**同一の分割で比べる**（4.6）。"""
    keep = _usable(data, target)
    if not keep.any():
        raise TeamRateError(f"{target} に使える行が1件もない")
    subset = _subset(data, keep)
    return walk_forward(
        subset.as_training_data(target),
        mean_learner(),
        target=subset.target(target),
        weights=subset.weights(target),
        max_folds=max_folds,
    )


def evaluate_all(
    data: TeamRateData, *, max_folds: int = MAX_FOLDS,
    num_boost_round: int = NUM_BOOST_ROUND_MAX,
) -> dict[str, Evaluation]:
    """14項目すべてを評価する。**順序は `TARGETS` に固定する。**"""
    return {
        target: evaluate_target(
            data, target, max_folds=max_folds, num_boost_round=num_boost_round)
        for target in TARGETS
    }


def fit_final(
    data: TeamRateData, target: str, *, rounds: int,
) -> tuple[str, int, list[str]]:
    """全データで最終当てはめし、`(artifact, 行数, 列)` を返す（詳細設計 4.5.1）。

    **評価とまったく同じ絞り込み・重み・列を使う。** 別に組むと、登録した
    モデルが評価したモデルと違うものになる（`_usable` を外すと成功率が NaN の
    行を学習に入れる）。

    **丸め（`clip_pct` / `clip_count`）は artifact に入らない。** LightGBM の
    テキストにそんな層はなく、推論側が同じ関数を通す（`batch/model/predict.py`）。
    """
    keep = _usable(data, target)
    if not keep.any():
        raise TeamRateError(f"{target} に使える行が1件もない")
    subset = _subset(data, keep)
    features = subset.features(target)
    weights = subset.weights(target)
    if weights is None:
        weights = np.ones(len(features), dtype=np.float64)
    _, artifact = train_final(
        features, subset.target(target), weights,
        num_boost_round=rounds, params=SCORE_PARAMS,
    )
    return artifact, len(features), list(features.columns)
