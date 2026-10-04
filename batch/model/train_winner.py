"""勝敗モデル（経路A）の学習（詳細設計 4.5 / 4.7）。

**経路Aは Winner の出力をそのまま勝率とする。** 経路B（Margin から
`Φ(margin/σ)` で導く）は `train_score.py` にある。P0-11 で比較した結果、
**経路A を採用した**（要件 6.1.1）。

**ただし Winner モデルの実装は LightGBM ではない（要件 6.1.1）。** 本番の勝敗
モデルは**全特徴ロジスティック回帰**（`batch/model/baselines.fit_logistic`）で
あり、A-09（Elo単体より Brier が有意かつ 0.003 以上良い）を満たすのはこの構成
だけだった。**この関数の LightGBM はベースライン3段目に降りた**（要件 6.4）。

| 役 | 実装 | Brier | A-09 |
|---|---|---|---|
| 本番（経路A） | `baselines.fit_logistic` | **0.198699** | **満たす** |
| ベースライン3 | **この関数**（LightGBM） | 0.201241 | 未達 |

**それでも残す。** 要件 6.4 のベースラインは「超えられなければ使う理由がない」
相手であり、LightGBM を落とすと**木が線形より良くなったときに気づけない**。

**LightGBM をここだけに閉じ込める。** 指標・ベースライン・walk-forward は
LightGBM を知らない（テストが GBDT なしで書けるようにするため）。
"""
from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd
from numpy.typing import NDArray

from batch.model.params import (
    EARLY_STOPPING_ROUND,
    NUM_BOOST_ROUND_MAX,
    WINNER_PARAMS,
)

type Floats = NDArray[np.float64]


def _booster():  # type: ignore[no-untyped-def]
    """LightGBM を遅延インポートする。

    指標とベースラインのテストは LightGBM 抜きで走らせたい（macOS では
    `libomp` が必要で、環境の都合でインポートできないことがある）。
    """
    # 遅延インポートが目的（下記）
    import lightgbm

    return lightgbm


def learn_winner(
    train_x: pd.DataFrame, train_y: Floats, train_w: Floats,
    valid_x: pd.DataFrame, valid_y: Floats,
    *, params: dict[str, object] | None = None,
    num_boost_round: int = NUM_BOOST_ROUND_MAX,
    on_gain: Callable[[dict[str, float]], None] | None = None,
) -> tuple[Callable[[pd.DataFrame], Floats], int]:
    """1 fold を学習し、(予測関数, best_iteration) を返す。

    **検証セットを必ず渡す。** early stopping は検証セットがあって初めて成立する。
    `num_boost_round` の上限は 1,200 で、artifact サイズが 1.5MB を超えないための
    制約でもある（詳細設計 4.7）。

    `on_gain` を渡すと、その fold の**列ごとの gain**（分割がもたらした損失の減少の
    合計）を通知する。要件 6.2 は「寄与度（SHAP / gain）と欠損率を測定する」と定めて
    おり、**特徴量の採否は全体の Brier だけでは判断できない**（1項目の効果は
    walk-forward の CI の幅より小さい。付録B）。`Learner` の契約（予測関数と
    `best_iteration` を返す）は変えないため、通知で外へ出す。
    """
    lgb = _booster()
    settings = dict(WINNER_PARAMS if params is None else params)
    train_set = lgb.Dataset(train_x, label=train_y, weight=train_w, free_raw_data=False)
    valid_set = lgb.Dataset(valid_x, label=valid_y, reference=train_set, free_raw_data=False)
    booster = lgb.train(
        settings,
        train_set,
        num_boost_round=num_boost_round,
        valid_sets=[valid_set],
        callbacks=[lgb.early_stopping(EARLY_STOPPING_ROUND, verbose=False)],
    )
    best = int(booster.best_iteration or num_boost_round)

    if on_gain is not None:
        names = [str(v) for v in booster.feature_name()]
        gains = [float(v) for v in booster.feature_importance(importance_type="gain")]
        on_gain(dict(zip(names, gains, strict=True)))

    def predict(features: pd.DataFrame) -> Floats:
        return np.asarray(
            booster.predict(features, num_iteration=best), dtype=np.float64,
        )

    return predict, best


def train_final(
    features: pd.DataFrame, actual: Floats, weights: Floats, *,
    num_boost_round: int, params: dict[str, object] | None = None,
) -> tuple[object, str]:
    """最終モデル。**各 fold の `best_iteration` の中央値で固定学習する**（基本設計 2.3）。

    early stopping を使わない（検証セットを学習に含めるため）。返すのは
    booster と `save_model()` のテキストで、後者を `model_versions.artifact_text`
    として登録する（詳細設計 1.6）。
    """
    lgb = _booster()
    settings = dict(WINNER_PARAMS if params is None else params)
    if num_boost_round < 1 or num_boost_round > NUM_BOOST_ROUND_MAX:
        raise ValueError("num_boost_round が範囲外である")
    train_set = lgb.Dataset(features, label=actual, weight=weights, free_raw_data=False)
    booster = lgb.train(settings, train_set, num_boost_round=num_boost_round)
    return booster, str(booster.model_to_string())


def median_iteration(values: list[int]) -> int:
    """`best_iteration` の中央値。**平均にしない** — 1 fold の外れ値に引かれる。"""
    if not values:
        raise ValueError("best_iteration が空である")
    return int(np.median(np.asarray(values, dtype=np.float64)))


#: 本番の勝率にかけるクランプの下限・上限（詳細設計 5020行目付近 / 6.4）。
#: **学習や評価ではかけない。** 外挿域の保険であって、較正の一部ではない。
WIN_PROB_FLOOR = 0.05
WIN_PROB_CEILING = 0.95


def clamp_win_prob(prob: Floats) -> Floats:
    """推論の後処理。確率を `[0.05, 0.95]` に収める（詳細設計 6.4）。

    **モデル側ではなく後処理で行う。** 極端な欠場（`minutes_lost` が学習域外）に
    対しては較正も効かず、「ホーム 100% – アウェイ 0%」と表示して外すと信頼が
    一度で失われる。

    **評価では通さない。** A-09 を満たした測定はクランプなしの値であり、ここを
    評価経路に入れると**測った数字と違うものを採用判定に使う**ことになる。

    **区間の外に出る試合は実在する**（2026-10-05 の実測。21列 / n=3,031）。

    | 項目 | 実測 |
    |---|---|
    | 区間の外 | **27件（0.9%）** |
    | 最小 / 最大 | 0.0287 / 0.9681 |
    | Brier（そのまま） | 0.199133393 |
    | Brier（クランプ後） | 0.199136464（**+3.1e-06**） |

    クランプは Brier をわずかに悪化させる（確信して当てた試合を中央へ寄せるため）。
    **それでも入れる** — 差は最小実質差 0.003 の1,000分の1であり、「97%と出して
    外す」ことの代償の方が大きい（詳細設計 5020行目付近）。

    予想スコアの導出（`margin_from_win_prob`）は `Φ⁻¹(p)` を通るため、
    **クランプが有限性の保証でもある**（`Φ⁻¹(0)` は −∞）。
    """
    return np.clip(np.asarray(prob, dtype=np.float64),
                   WIN_PROB_FLOOR, WIN_PROB_CEILING)
