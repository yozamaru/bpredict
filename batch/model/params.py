"""学習のパラメータ。**値の出どころを1か所にまとめる**（詳細設計 4.6 / 4.7）。

ここにある値は「設計が定めた値」と「探索の初期値」の2種類である。混ぜないため、
どちらであるかを必ず注記する。
"""
from __future__ import annotations

#: 採用ゲート（詳細設計 4.6）。**設計が定めた値であり、緩めてはならない。**
MIN_EVAL_N = 500        # これ未満では比較そのものを行わない
MIN_EFFECT = 0.003      # 最小実質差（Brier）
ECE_FLOOR_K = 1.5       # ノイズフロア95%点に対する倍率
MAX_FEATURE_NULL_RATE = 0.30

#: ECE の定義（要件 6.4）。等頻度10ビン・1ビン最低50件。**変えない。**
ECE_BINS = 10
ECE_MIN_PER_BIN = 50

#: walk-forward の fold 数の上限（基本設計 2.3）。設けないとシーズンが増えるたびに
#: 学習時間が単調増加する
MAX_FOLDS = 5

#: 時間減衰の λ。`weight = exp(-λ × 経過シーズン数)`（要件 6.6）。
#: **探索の初期値である。** 要件は「半減期3〜5シーズンが出発点」と定めており、
#: 4シーズンの半減期が ln(2)/4 ≒ 0.173 に当たる。工程8の walk-forward で探索する。
TIME_DECAY_LAMBDA_INITIAL = 0.173
TIME_DECAY_LAMBDA_GRID = (0.0, 0.139, 0.173, 0.231)   # 半減期 ∞ / 5 / 4 / 3 シーズン

#: LightGBM のパラメータ（詳細設計 4.7）。**seed 系を必ず含める** —
#: 固定しないと Brier が ±0.003 ぶれ、採用判定が乱数に支配される。
WINNER_PARAMS: dict[str, object] = {
    "objective": "binary",
    "metric": ["binary_logloss"],
    "learning_rate": 0.03,
    "num_leaves": 7,           # 8,000行に対し15は過大
    "max_depth": 3,
    "min_data_in_leaf": 100,
    "feature_fraction": 0.7,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    "verbosity": -1,
    "seed": 42,
    "bagging_seed": 42,
    "feature_fraction_seed": 42,
    "deterministic": True,
    "force_row_wise": True,
    "num_threads": 4,
}

#: 上限。artifact サイズが 1.5MB を超えないための制約でもある（詳細設計 4.7）
NUM_BOOST_ROUND_MAX = 1200
EARLY_STOPPING_ROUND = 100
