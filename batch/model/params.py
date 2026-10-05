"""学習のパラメータ。**値の出どころを1か所にまとめる**（詳細設計 4.6 / 4.7）。

ここにある値は「設計が定めた値」と「探索の初期値」の2種類である。混ぜないため、
どちらであるかを必ず注記する。
"""
from __future__ import annotations

#: 採用ゲート（詳細設計 4.6）。**設計が定めた値であり、緩めてはならない。**
MIN_EVAL_N = 500        # これ未満では比較そのものを行わない
MIN_EFFECT = 0.003      # 最小実質差（Brier）
ECE_FLOOR_K = 1.5       # ノイズフロア95%点に対する倍率

#: ECE の閾値の**下限**（要件 6.4。v1.32 で運営者が置いた）。
#:
#: **ノイズフロアは標本が大きいほど小さくなるため、大標本では到達不能になる。**
#: 第1段（n=124,927）の実測では、**同じモデル・同じ予測なのに n だけで判定が
#: 反転した** — n=3,000 で通り（閾値 0.0187）、n=30,000 で落ちる（0.0059）。
#: ECE は n が増えるほど良くなっているのに、閾値がそれより速く縮む。
#:
#: 意味は「**確率の目盛りのずれが1ポイント以内なら良しとする**」。完全に較正された
#: モデルでも n=1,200 で ECE 95%点は 0.0479 であり（要件 6.4 の表）、0.01 は設計が
#: 想定した標本規模に対しては十分に厳しい。
#:
#: **固定閾値に戻したのではない。** 小標本では引き続きノイズフロアが閾値を決める
#: （n=500 で 0.0467）。下限が効くのは**フロアが1ポイントを下回ったときだけ**である。
ECE_THRESHOLD_FLOOR = 0.01
MAX_FEATURE_NULL_RATE = 0.30

#: ECE の定義（要件 6.4）。等頻度10ビン・1ビン最低50件。**変えない。**
ECE_BINS = 10
ECE_MIN_PER_BIN = 50

#: walk-forward の fold 数の上限（基本設計 2.3）。設けないとシーズンが増えるたびに
#: 学習時間が単調増加する
MAX_FOLDS = 5

#: **選手モデルの fold 数は3に制限する**（要件 6.8.7 / 基本設計 2.3）。
#: 選手モデルは16本あり（出場2 + 14項目）、チームモデルと同じ5 fold では
#: `monthly_train` の120分枠に収まらない。**行数は1 fold でも十分にある**
#: （146,463行あり、n >= 500 の下限に対して3桁の余裕がある）
PLAYER_MAX_FOLDS = 3

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

#: 得点差（Margin）と合計得点（Total）の回帰（基本設計 2.3）。
#: **`WINNER_PARAMS` を写して目的関数だけ変える。** 詳細設計 4.7 は回帰用の別の
#: グリッドを定めていないため、**こちらで別の木の形を作らない**。木を深くする前に
#: 特徴量を見直す（同 4.7）。seed 系も同じ値で固定する。
SCORE_PARAMS: dict[str, object] = {
    **WINNER_PARAMS,
    # L2（二乗誤差）。詳細設計 4.5 の擬似コードが回帰に `"regression"` を使っている
    "objective": "regression",
    # **MAE も出す。** 要件 6.4 が予想スコアの指標を MAE と定めている
    "metric": ["l2", "l1"],
}
