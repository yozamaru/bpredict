"""P0-12  LightGBM の実 artifact サイズを測る。

04 は lightgbm を導入できなかったため「テキスト書式からの再構成」による推定だった。
これは実際に学習して save_model() の出力バイト数を測る。

前提: pip install "lightgbm==4.5.0" "numpy==2.1.3"   （バージョンは固定する）
実行: python verification/06_lightgbm_artifact_size.py

見るところ:
  - 設計パラメータ（num_leaves=7）での実サイズが 1.5MB 以内か
  - early stopping が効かず num_boost_round=1200 まで回った場合でも 1.5MB 以内か
  - 超える場合に gzip+base64 でどこまで落ちるか（退避手段の実数値）

注意: 木の構造はデータの性質に依存する。合成データで測った値は「桁と傾向」であり、
      実データでの最終確認は工程8で行う。ただし 04 の推定よりは桁違いに信頼できる。
"""
import base64
import gzip
import sys

try:
    import lightgbm as lgb
    import numpy as np
except ImportError as e:
    sys.exit(f"依存が入っていません: {e.name}\n"
             '  pip install "lightgbm==4.5.0" "numpy==2.1.3"')

N_ROWS = 8_000          # 設計の想定試合数
SEED = 42
GATE = 1_572_864        # 1.5 MiB。登録時の上限
D1_ROW_LIMIT = 2_000_000

# docs/design-detail.md 4.7 の PARAMS
PARAMS = {
    "objective": "binary", "metric": ["binary_logloss"],
    "learning_rate": 0.03, "max_depth": 3, "min_data_in_leaf": 100,
    "feature_fraction": 0.7, "bagging_fraction": 0.8, "bagging_freq": 1,
    "lambda_l2": 1.0, "verbosity": -1,
    "seed": SEED, "bagging_seed": SEED, "feature_fraction_seed": SEED,
    "deterministic": True, "force_row_wise": True, "num_threads": 4,
}


def make_data(n_feat, rng):
    """勝敗予測に近い信号量（Brier 0.20 前後）の合成データを作る。

    信号を入れないと木が伸びず、サイズを過小評価する。
    """
    X = rng.standard_normal((N_ROWS, n_feat))
    w = rng.standard_normal(n_feat) * 0.25
    w[10:] *= 0.15                                   # 効く特徴は一部だけ
    logit = X @ w + 0.40                             # 切片 = ホームアドバンテージ
    p = 1 / (1 + np.exp(-logit))
    y = (rng.random(N_ROWS) < p).astype(int)
    return X, y


def measure(n_feat, num_leaves, num_boost_round, early_stopping, rng):
    X, y = make_data(n_feat, rng)
    cut = int(N_ROWS * 0.8)
    tr = lgb.Dataset(X[:cut], y[:cut])
    va = lgb.Dataset(X[cut:], y[cut:], reference=tr)
    params = {**PARAMS, "num_leaves": num_leaves}
    cbs = [lgb.early_stopping(100, verbose=False)] if early_stopping else []
    bst = lgb.train(params, tr, num_boost_round=num_boost_round,
                    valid_sets=[va], callbacks=cbs)
    text = bst.model_to_string()
    raw = len(text.encode("utf-8"))
    packed = len(base64.b64encode(gzip.compress(text.encode("utf-8"), 9)))
    return bst.num_trees(), raw, packed


def verdict(n):
    if n > D1_ROW_LIMIT:
        return "D1の1行上限を超過"
    if n > GATE:
        return "1.5MBゲートで登録拒否"
    if n > GATE * 0.75:
        return "危険域(>75%)"
    return "余裕"


def main():
    rng = np.random.default_rng(SEED)
    print("=" * 96)
    print("P0-12  LightGBM artifact の実サイズ（合成データ 8,000行）")
    print("=" * 96)
    print(f"lightgbm {lgb.__version__} / numpy {np.__version__}")
    print(f"ゲート: {GATE:,} バイト (1.5 MiB) / D1 の1行上限: {D1_ROW_LIMIT:,} バイト")
    print()
    print(f"{'特徴量':>6} {'leaves':>7} {'ES':>4} {'上限':>6} {'実本数':>7} "
          f"{'実サイズ':>12} {'ゲート比':>9} {'gzip+b64':>11} {'判定':>18}")
    print("-" * 96)

    rows = []
    for n_feat in (60, 80):
        for num_leaves in (7, 15):
            for cap, es in ((1200, True), (1200, False)):
                trees, raw, packed = measure(n_feat, num_leaves, cap, es, rng)
                rows.append((n_feat, num_leaves, es, trees, raw, packed))
                print(f"{n_feat:>6} {num_leaves:>7} {'有' if es else '無':>4} {cap:>6} "
                      f"{trees:>7} {raw:>11,}B {raw / GATE:>8.0%} "
                      f"{packed:>10,}B {verdict(raw):>18}")
        print()

    design = [r for r in rows if r[1] == 7]
    worst = max(design, key=lambda r: r[4])
    print("【判断材料】")
    print(f"  設計値 num_leaves=7 での最大: {worst[4]:,} バイト "
          f"({worst[4] / GATE:.0%} of 1.5MB, 木 {worst[3]} 本, 特徴量 {worst[0]})")
    if worst[4] <= GATE:
        print("  → 1.5MB ゲート内。設計どおり artifact_text を D1 に直接格納できる")
        print("  → gzip+base64 の退避手段は実装不要（v1 では入れない方針のまま）")
    else:
        print("  → ★ゲート超過。設計の修正が必要")
        print(f"     退避手段 gzip+base64 なら {worst[5]:,} バイト "
              f"({worst[5] / worst[4]:.0%} に縮む)")
        print("     もしくは num_boost_round の上限を下げる")
    big = [r for r in rows if r[1] == 15]
    if big and max(r[4] for r in big) > GATE:
        print(f"  num_leaves=15 は最大 {max(r[4] for r in big):,} バイトでゲートを超える")
        print("  → 探索範囲に 15 を含める場合、サイズ再測定とセットでのみ許可する（設計どおり）")
    print()
    print("verification/RESULTS.md の P0-12 の行に、上の『実サイズ』の最大値を記録すること。")


if __name__ == "__main__":
    main()
