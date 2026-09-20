import numpy as np
rng=np.random.default_rng(1)

print("="*76)
print("V-13  LightGBM artifact サイズの推定（lightgbm が導入できないため書式から構成）")
print("="*76)
print("注意: 実測ではなく、LightGBM のテキストモデル書式を忠実に再構成した推定値です。")
print("      実測は Phase 0 で行う必要があります。")
print()

def tree_text(idx, num_leaves, n_feat, lr):
    ni = num_leaves-1; nl = num_leaves
    f  = lambda n,fmt: " ".join(fmt.format(v) for v in rng.random(n))
    L=[]
    L.append(f"Tree={idx}")
    L.append(f"num_leaves={num_leaves}")
    L.append("num_cat=0")
    L.append("split_feature=" + " ".join(str(int(v)) for v in rng.integers(0,n_feat,ni)))
    L.append("split_gain=" + " ".join(f"{v*500:.9g}" for v in rng.random(ni)))
    L.append("threshold=" + " ".join(f"{v*100:.17g}" for v in rng.random(ni)))
    L.append("decision_type=" + " ".join("2" for _ in range(ni)))
    L.append("left_child=" + " ".join(str(int(v)) for v in rng.integers(-nl,nl,ni)))
    L.append("right_child=" + " ".join(str(int(v)) for v in rng.integers(-nl,nl,ni)))
    L.append("leaf_value=" + " ".join(f"{(v-0.5)*0.1:.17g}" for v in rng.random(nl)))
    L.append("leaf_weight=" + " ".join(f"{v*300:.9g}" for v in rng.random(nl)))
    L.append("leaf_count=" + " ".join(str(int(v)) for v in rng.integers(100,900,nl)))
    L.append("internal_value=" + " ".join(f"{(v-0.5)*4:.9g}" for v in rng.random(ni)))
    L.append("internal_weight=" + " ".join(f"{v*2000:.9g}" for v in rng.random(ni)))
    L.append("internal_count=" + " ".join(str(int(v)) for v in rng.integers(500,8000,ni)))
    L.append("is_linear=0")
    L.append(f"shrinkage={lr}")
    return "\n".join(L)+"\n\n\n"

def header(n_feat):
    names=" ".join(f"feature_{i}" for i in range(n_feat))
    infos=" ".join(f"{rng.random()*10:.17g}:{rng.random()*100:.17g}" for _ in range(n_feat))
    return ("tree\nversion=v4\nnum_class=1\nnum_tree_per_iteration=1\nlabel_index=0\n"
            f"max_feature_idx={n_feat-1}\nobjective=binary sigmoid:1\n"
            f"feature_names={names}\nfeature_infos={infos}\n\n")

LIMIT = 2_000_000
print(f"{'特徴量数':>8} {'num_leaves':>11} {'木の本数':>9} {'1本あたり':>11} {'合計サイズ':>13} {'2MB上限に対して':>16}")
print("-"*76)
for n_feat in (60, 80):
    for nl in (7, 15):
        t1 = len(tree_text(0, nl, n_feat, 0.03).encode())
        h  = len(header(n_feat).encode())
        for ntree in (300, 800, 1500, 2000):
            total = h + t1*ntree
            pct = total/LIMIT
            flag = "超過" if total>LIMIT else ("危険" if pct>0.75 else "余裕")
            print(f"{n_feat:>8} {nl:>11} {ntree:>9} {t1:>10,}B {total:>12,}B {pct:>14.0%} {flag}")
    print()

print("【判断材料】")
print("  ・設計の num_leaves=7 / early stopping ありなら通常 300〜800本 → 概ね 0.2〜0.6 MB")
print("  ・early stopping が効かず num_boost_round=2000 まで回ると 1.0〜1.6 MB")
print("  ・num_leaves=15 に上げた場合は 2000本で 2MB を超えうる")
print("  → D1 の 2MB 上限に対し、通常運用では収まるが余裕は大きくない")
print("  → 登録時の上限チェック（例 1.5MB）は必須。実測は Phase 0 で")
print()

print("="*76)
print("V-14  D1 のバッチ上限の算術（Free: 1呼び出し50クエリ / 1文100バインド）")
print("="*76)
MAXP, SAFE_FREE, SAFE_PAID = 100, 40, 800
tables = [("games", 20), ("team_games", 9), ("team_game_stats", 20),
          ("player_game_stats", 22), ("game_entries", 6), ("team_ratings", 7),
          ("predictions", 18), ("player_predictions", 26), ("prediction_reasons", 7)]
print(f"{'テーブル':<22} {'列数':>5} {'1文の行数':>10} {'Free上限行数':>13} {'現設計':>9} {'判定':>8}")
print("-"*76)
for name, cols in tables:
    rps = MAXP//cols
    free_rows = rps*SAFE_FREE
    cur = 200 if name=="player_game_stats" else 500
    need = -(-cur//rps)
    ok = "OK" if cur<=free_rows else "超過"
    print(f"{name:<22} {cols:>5} {rps:>10} {free_rows:>13} {cur:>9} {ok:>8}  ({need}文必要)")
print()
print("  ※ SAFE_MAX_STATEMENTS=40（Free上限50の8割）で算出")
print("  → player_game_stats(200件=50文) と predictions(500件=90文) が Free 上限を超える")
